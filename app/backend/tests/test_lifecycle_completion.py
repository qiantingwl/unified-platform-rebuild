"""Exercise SMS and recovery paths through the durable state machine, without network."""
import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Account, Resource
from app.registration import RegistrationService
from app.sms_channels import ChannelInput, ChannelUpdate, SmsChannels
from app.store import Store, DomainError, uid
from app.upstream import Transport, UpstreamError
from app.workbuddy_sms import WorkBuddySMS


@pytest.fixture
def env(tmp_path):
    now = [1794801600.0]
    store = Store(f"sqlite:///{tmp_path/'db'}", Fernet.generate_key(), clock=lambda:now[0])
    class Registry:
        enabled = True
        transport_factory = staticmethod(lambda **kwargs: Transport(httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(500)))))
    service = RegistrationService(store, Registry())
    yield store, service, now
    store.engine.dispose()


def channel(service, **kwargs):
    return service.sms.configure(ChannelInput(name="synthetic", provider="haozhuma", base_url="https://sms.example.test/api", tokens=["first-private", "second-private"], project="p1", **kwargs))


def session(store, platform="Loomy"):
    sid = uid()
    with store.tx() as (s, _):
        s.add(Resource(id=sid,kind="registration",name="synthetic",data=json.dumps({"platform":platform,"status":"failed"}),updated_at=store.clock()))
    return sid


def test_sms_all_token_balances_partial_failure_and_pairing(env):
    store,service,_=env
    cid=service.sms.configure(ChannelInput(name="paired",provider="haozhuma",base_url="https://sms.example.test/api",pairs=[{"token":"one-private","project":"sid1"},{"token":"two-private","project":"sid2"},{"token":"one-private","project":"sid3"}]))
    config=service.sms.read(cid)
    assert [(v["token"],v["project"]) for v in service.sms.candidates(cid,config)]==[("one-private","sid1"),("two-private","sid2"),("one-private","sid3")]
    calls=[]
    async def call(c,action,**params):
        calls.append(c["token"])
        if c["token"]=="two-private":raise UpstreamError("sms_channel_credentials_invalid")
        return {"money":"8.5"}
    service.sms.call=call
    result=asyncio.run(service.sms.balances(cid))
    assert calls==["one-private","two-private"]
    assert result["balance"] is None and result["verified_subtotal"]==8.5 and result["status"]=="partial"
    public=json.dumps(store.resources("sms_channel")+store.resources("sms_balance"))
    assert "one-private" not in public and "two-private" not in public
    async def valid(c,action,**params):return {"money":"0"}
    service.sms.call=valid
    result=asyncio.run(service.sms.balances(cid))
    assert result["balance"]==0 and result["status"]=="verified"


def test_eomsg_purchase_and_poll_use_separate_keywords(env):
    store,service,_=env
    cid=service.sms.configure(ChannelInput(name="separate",provider="eomsg",base_url="https://sms.example.test/api",token="private",keyword_get="Huawei cloud",keyword_msg="Huawei"))
    calls=[]
    async def call(c,action,**params):
        calls.append((action,params.get("keyWord")))
        return "13800000001" if action=="getPhone" else "验证码123456"
    service.sms.call=call
    lease,_=asyncio.run(service.sms.acquire(cid,session(store)))
    assert asyncio.run(service.sms.poll(lease))=="123456"
    assert calls==[("getPhone","Huawei cloud"),("getMsg","Huawei")]


def test_batch_defaults_do_not_pause_after_arbitrary_failure_count(env):
    from app.registration_runner import BatchInput,RegistrationRunner
    store,service,now=env
    runner=RegistrationRunner(service,None)
    bid=runner.make_batch(BatchInput(name="retry",platform="Loomy",count=1,parallel=1,sms_channel_id=channel(service)))
    for _ in range(21):
        with store.tx() as (s,state):
            batch=s.get(Resource,bid);data=json.loads(batch.data)
            assert data["status"]=="active" and data["number_budget"] is None
            row=s.get(Resource,data["sessions"][-1]);item=json.loads(row.data)
            item.update(status="failed",error="phone_previously_used",finished_at=now[0]);row.data=json.dumps(item)
            now[0]+=1
            runner.refill(s,state,batch)
    assert runner.batches()[0]["data"]["status"]=="active"


def test_renew_wait_prepare_returns_plan_without_mutation(env):
    _,service,_=env
    plan={"action":"renew_wait","reason":"renew_backoff","job_id":"synthetic"}
    service.recovery.plan=lambda _:plan
    assert service.recovery.prepare("synthetic")==plan


def test_received_sms_completes_poll_and_is_encrypted(env):
    store, service, now = env
    cid=channel(service)
    lease=service.sms.existing_number(cid, session(store), "13800000001")
    async def call(c, action, **params):
        return {"sms":"【腾讯】517204为您的登录验证码"}
    service.sms.call=call
    assert asyncio.run(service.sms.poll(lease)) == "517204"
    assert asyncio.run(service.sms.poll(lease)) == "517204"
    public=json.dumps(store.resources("sms_channel")+store.resources("sms_lease"))
    for value in ("517204", "13800000001", "first-private", "second-private"):
        assert value not in public


@pytest.mark.parametrize("status", ["code_received", "retired"])
def test_late_code_rejection_preserves_new_code_or_terminal_lease(env, status):
    store,service,_=env
    lease=service.sms.existing_number(channel(service),session(store),"13800000001")
    service.sms.update(lease,{"status":status},{"code":"654321"})
    service.sms.reject_code(lease,"123456")
    data,private=service.sms.lease(lease)
    assert data["status"]==status
    assert private["code"]=="654321" and not private.get("rejected_code")


def test_poll_recovery_clears_failure_flags(env):
    store,service,_=env
    lease=service.sms.existing_number(channel(service),session(store),"13800000001")
    service.sms.update(lease,{"poll_error":"network","poll_all_failed":True,"poll_error_at":store.clock()})
    async def call(*args,**kwargs):return {"yzm":"654321"}
    service.sms.call=call
    assert asyncio.run(service.sms.poll(lease))=="654321"
    data,_=service.sms.lease(lease)
    assert data["poll_error"] is None and not data["poll_all_failed"] and data["poll_error_at"] is None


def test_existing_number_cross_token_project_poll_and_throttle(env):
    store, service, now=env
    cid=channel(service,projects=["p2"])
    lease=service.sms.existing_number(cid,session(store),"13800000001")
    calls=[]
    async def call(c, action, **params):
        calls.append((c["token"],params["sid"]))
        if c["token"]=="second-private" and params["sid"]=="p2":
            return {"yzm":"123456"}
        return None
    service.sms.call=call
    assert asyncio.run(service.sms.poll(lease))=="123456"
    assert calls==[("first-private","p1"),("first-private","p2"),("second-private","p1"),("second-private","p2")]
    service.sms.reject_code(lease,"123456")
    assert asyncio.run(service.sms.poll(lease)) is None and len(calls)==4
    now[0]+=5
    assert asyncio.run(service.sms.poll(lease)) is None
    assert calls[4]==("second-private","p2")


def test_acquisition_moves_only_on_definitive_refusal_and_pins_release(env):
    store, service, now=env
    cid=channel(service)
    calls=[]
    async def call(c, action, **params):
        calls.append((c["token"],action))
        if action=="getPhone" and c["token"]=="first-private":
            raise UpstreamError("sms_no_numbers")
        return {"phone":"13800000001"}
    service.sms.call=call
    lease,_=asyncio.run(service.sms.acquire(cid,session(store)))
    row=next(r for r in store.resources("sms_channel") if r["id"]==cid)
    service.sms.configure(ChannelUpdate(name="synthetic",provider="haozhuma",base_url="https://sms.example.test/api",token="replacement",project="p1",expected_version=row["version"]),cid)
    asyncio.run(service.sms.retire(lease,"failed"))
    assert calls[-1:]==[("second-private","cancelRecv")]
    assert not any(action=="addBlacklist" for _,action in calls)
    calls.clear()
    async def ambiguous(c,action,**params):
        calls.append(action)
        raise UpstreamError("upstream_timeout")
    service.sms.call=ambiguous
    with pytest.raises(UpstreamError):
        asyncio.run(service.sms.acquire(cid,session(store)))
    assert calls==["getPhone"]


@pytest.mark.parametrize("reason,blacklist", [("sms_send_rejected",False),("failed",False),("succeeded",False),("cancelled",False),("duplicate_number",True)])
def test_phone_release_never_blacklists_on_platform_or_generic_failure(env,reason,blacklist):
    store,service,_=env
    cid=channel(service);calls=[]
    async def call(config,action,**params):
        calls.append(action)
        return {"phone":"13800000001"}
    service.sms.call=call
    lease,_=asyncio.run(service.sms.acquire(cid,session(store)))
    calls.clear()
    asyncio.run(service.sms.retire(lease,reason))
    assert calls==(["addBlacklist","cancelRecv"] if blacklist else ["cancelRecv"])
    data,_=service.sms.lease(lease)
    assert data["status"]=="retired" and data["release_confirmed"]
    assert bool(data["blacklist_confirmed"]) is blacklist


def test_pool_update_omitting_tokens_preserves_secret(env):
    store, service, now=env
    cid=channel(service,projects=["p2"])
    row=next(r for r in store.resources("sms_channel") if r["id"]==cid)
    service.sms.configure(ChannelUpdate(name="renamed",provider="haozhuma",base_url="https://sms.example.test/api",project="p1",expected_version=row["version"]),cid)
    saved=service.sms.read(cid)
    assert saved["tokens"]==["first-private","second-private"] and saved["projects"]==["p2"]


@pytest.mark.parametrize("message,code",[("等待短信","sms_waiting"),("您的余额不足,请释放拉黑后再取号","sms_number_limit"),("余额不足请充值","sms_balance_insufficient"),("取号频繁","sms_rate_limit"),("无号可取","sms_no_numbers"),("token失效","sms_channel_credentials_invalid")])
def test_sms_errors_are_distinct(message,code):
    assert SmsChannels.provider_error(message)==code


def prepare_account(store,identity,phone):
    aid=store.create_account("Loomy",identity,identity)
    store.put_credential(aid,json.dumps({"session":"synthetic", "phone":phone}),0)
    with store.tx() as (s,_):
        s.get(Account,aid).credential_status="invalid"
    return aid


def transition(service,sid):
    token=service.store.acquire_execution(sid,8)
    try:
        return service.transition(sid,"prepared","sending",token),token
    except BaseException:
        service.store.release_execution(sid,token)
        raise


def test_same_identity_daily_limit_is_atomic_and_no_sms_count_shared(env):
    store,service,now=env
    a=prepare_account(store,"a","13800000001")
    b=prepare_account(store,"b","+8613800000001")
    sa=service.create("Loomy","a","sms",{"phone":"13800000001"},a)
    sb=service.create("Loomy","b","sms",{"phone":"+8613800000001"},b)
    (data,_),token=transition(service,sa)
    service.finish(sa,data["operation"],result={"challenge":"c"})
    store.release_execution(sa,token)
    with pytest.raises(DomainError) as failure:
        transition(service,sb)
    assert failure.value.code=="relogin_already_scheduled_today"
    assert service.recovery.plan(b)["action"]=="pending"  # its prepared attempt still visible
    now[0]+=301
    service.expire_sms(sa)
    with store.read() as (s,_):
        assert s.get(Account,a).relogin_failures==1
    # Next day the other record cannot forget this identity's failed SMS attempt.
    now[0]+=86400
    (data,_),token=transition(service,sb)
    service.finish(sb,data["operation"],result={"challenge":"c"})
    store.release_execution(sb,token)
    now[0]+=301
    service.expire_sms(sb)
    with store.read() as (s,_):
        assert s.get(Account,a).relogin_failures==s.get(Account,b).relogin_failures==2


def test_sms_channel_outage_is_not_no_sms_account_failure(env):
    store,service,now=env
    aid=prepare_account(store,"a","13800000001")
    cid=channel(service)
    sid=service.create("Loomy","a","sms",{"phone":"13800000001"},aid)
    (data,_),token=transition(service,sid)
    lease=service.sms.existing_number(cid,sid,"13800000001")
    service.checkpoint(sid,data["operation"],{"sms_lease_id":lease})
    service.finish(sid,data["operation"],result={"challenge":"c"})
    store.release_execution(sid,token)
    async def call(*args,**kwargs):
        raise UpstreamError("sms_channel_credentials_invalid")
    service.sms.call=call
    with pytest.raises(UpstreamError):
        asyncio.run(service.sms.poll(lease))
    now[0]+=301
    service.recover()
    assert service.details(sid)[0]["error"]=="sms_channel_credentials_invalid"
    with store.read() as (s,_):
        assert s.get(Account,aid).relogin_failures==0


def test_received_but_not_consumed_sms_does_not_count_as_no_sms(env):
    store,service,now=env
    aid=prepare_account(store,"a","13800000001")
    cid=channel(service)
    sid=service.create("Loomy","a","sms",{"phone":"13800000001"},aid)
    (data,_),token=transition(service,sid)
    lease=service.sms.existing_number(cid,sid,"13800000001")
    service.checkpoint(sid,data["operation"],{"sms_lease_id":lease})
    service.finish(sid,data["operation"],result={"challenge":"c"})
    store.release_execution(sid,token)
    service.sms.update(lease,{"status":"code_received"},{"code":"123456"})
    now[0]+=301
    service.recover()
    assert service.details(sid)[0]["error"]=="verification_expired"
    with store.read() as (s,_):
        assert s.get(Account,aid).relogin_failures==0


def test_workbuddy_ticket_rewrite_once_without_sms_resend():
    async def run():
        transport=Transport(httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(500))))
        adapter=WorkBuddySMS(transport)
        material={"sms_verified":True,"broker_complete":True,"ticket_written":True,"cli_state":"state","phone":"+8613800000001"}
        writes=[]
        async def checkpoint(patch):material.update(patch)
        async def navigation(*args):return 200,b""
        async def fake_json(method,url,*args,**kwargs):
            assert "sms" not in url
            writes.append(url)
            return {}
        async def raw(method,url,*args,**kwargs):
            if "/console/auth/login" in url:return 200,{},b'{"code":0}'
            return 200,{},b'{"code":11217}'
        adapter.checkpoint,adapter.navigation,adapter.json,adapter.raw=checkpoint,navigation,fake_json,raw
        try:
            for _ in range(8):
                assert await adapter.verify(material,"sms",None)=={"waiting":"waiting_ticket"}
            assert material["ticket_rewrites"]==1
            assert len(writes)==1 and "/console/login/enterprise" in writes[0]
        finally:await transport.close()
    asyncio.run(run())
