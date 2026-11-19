"""P3 isolated lifecycle acceptance. External HTTP is forbidden by conftest."""
import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.models import Account, Attempt, Credential, Job, Resource
from app.store import Store, DomainError, day_at, uid
from app.native_lifecycle import control, record_native_login
from app.login_material import requirements
from app.registration import RegistrationService
from app.registration_queries import ensure_registration_indexes, field, page, runnable
from app.worker import MaintenanceWorker
from app.workbuddy_benefits import execute as benefits
from app.workbuddy_credits import packages
from app.imports import Imports, PreviewInput, mount_imports
from app.task_receipts import mount_task_receipts
from app.upstream import UpstreamError
from app.upstream import Transport
from app.sms_channels import ChannelInput, ProviderLogin, SmsChannels


@pytest.fixture
def env(tmp_path):
    now = [time.time()]
    store = Store(f"sqlite:///{tmp_path/'isolated.db'}", Fernet.generate_key(), clock=lambda: now[0])
    yield store, now
    store.engine.dispose()


def account(store, platform="Loomy", credential=None):
    credential = credential or {"session": "isolated-session", "phone": "13800000001", "password": "isolated-password"}
    aid = uid()
    with store.tx() as (s, _):
        a = Account(id=aid, platform=platform, external_id=aid, label="isolated", sandbox=False,
            enabled=True, credential_version=1, credential_status="invalid", created_at=store.clock(), updated_at=store.clock())
        s.add(a)
        s.add(Credential(id=uid(), account_id=aid, version=1, ciphertext=store.cipher.encrypt(json.dumps(credential).encode()).decode(), created_at=store.clock()))
    return aid


def running(store, aid, kind):
    jid = uid()
    with store.tx() as (s, _):
        a = s.get(Account, aid)
        day = day_at(store.clock())
        s.add(Job(id=jid, dedupe_key=jid, account_id=aid, kind=kind, business_day=day, credential_version=a.credential_version,
            status="running", priority=10, available_at=store.clock(), created_at=store.clock(), owner="isolated-worker", lease_until=store.clock()+120, attempt=1))
        s.add(Attempt(id=uid(), job_id=jid, number=1, owner="isolated-worker", started_at=store.clock()))
        result = {"id":jid,"account_id":aid,"kind":kind,"day":day,"version":a.credential_version,
            "owner":"isolated-worker","attempt":1,"platform":a.platform,"sandbox":False}
    return result


class Adapter:
    def __init__(self, credential=None):
        self.c = credential or {}
        self.base = "https://isolated.invalid"
        self.t = SimpleNamespace(close=self.close)
        self.renew_calls = 0
        self.posts = []
        self.redeemed = False
        self.ambiguous = False
        self.saved = {}

    async def close(self):
        pass

    def headers(self):
        return {}

    async def renew(self):
        self.renew_calls += 1
        raise UpstreamError("network")

    async def validate_renewal(self, candidate):
        return "verified-identity"

    async def checkin(self, day):
        return {"day": day}

    async def balance(self):
        return 0

    async def ledger(self, key, value=None):
        if value is not None:
            self.saved[key] = value
        return self.saved.get(key)

    async def request(self, method, url, headers, body=None):
        if method == "POST":
            self.posts.append((url, body))
            if self.ambiguous:
                raise UpstreamError("network")
            self.redeemed = True
            return {}
        if url.endswith("/streak"):
            return {"makeup_cards":{"balance":0},"redemption_status":{
                "tier_7d_status":"claimed" if self.redeemed else "available", "tier_14d_status":"locked", "tier_28d_status":"locked"}}
        if url.endswith("/buddy/info"):
            return {"buddy": None}
        raise AssertionError("unexpected isolated request")


def worker(store, adapter):
    registry = SimpleNamespace(make=lambda *args: adapter)
    return MaintenanceWorker(store, registry=registry)


@pytest.mark.parametrize("platform,mode,credential", [
    ("Loomy","sms",{"session":"new","phone":"13800000001"}),
    ("小浣熊","official",{"access_token":"new","refresh_token":"refresh-new"}),
    ("WPS 灵犀","browser",{"cookie":"wps_sid=new"}),
    ("CodeArts","official",{"refresh_token":"new","oauth_context":{"pkce_pair":{"code_verifier":"v"},"dpop_key_pair":{"private_key_jwk":{"d":"private"}}}}),
    ("WorkBuddy 国内","sms",{"account":{"uid":"u","enterpriseId":"e"},"auth":{"accessToken":"new","refreshToken":"r"}}),
    ("WorkBuddy 国际","official",{"account":{"uid":"u"},"auth":{"accessToken":"new","refreshToken":"r"}}),
    ("Marvis","oauth_qq",{"access_token":"new"}),
])
def test_seven_platform_official_modes_and_import_is_not_independence(env, platform, mode, credential):
    store, _ = env
    aid = account(store, platform, credential)
    with store.tx() as (s, _):
        a = s.get(Account, aid)
        assert record_native_login(store,s,a,mode,credential)["state"] == "independent"
        assert control(s,a)["state"] == "independent"
        assert record_native_login(store,s,a,"verified_import",credential)["state"] == "verification_required"


def test_import_and_reused_refresh_require_independent_session_evidence(env):
    store, _ = env
    c = {"access_token":"old","refresh_token":"shared"}
    aid = account(store,"小浣熊",c)
    with store.tx() as (s,_):
        a=s.get(Account,aid)
        assert control(s,a)["state"]=="verification_required"
        assert control(s,a)["reason"]=="session_evidence_required"
        a.credential_version=2
        value=record_native_login(store,s,a,"official",{"access_token":"new","refresh_token":"shared"},1)
        assert value["state"]=="verification_required" and value["reason"]=="official_login_reused_session_material"


def test_materials_require_real_pkce_dpop_and_wps_not_renewal():
    assert set(requirements("CodeArts",{"refresh_token":"r","oauth_context":{}},{})["missing_material"])=={"pkce_code_verifier","dpop_private_key"}
    assert not requirements("WPS 灵犀",{"refresh_token":"r"},{})["can_refresh"]
    import base64
    claims=base64.urlsafe_b64encode(b'{"iss":"https://identity.invalid/iam"}').decode().rstrip("=")
    value=requirements("小浣熊",{"access_token":"x."+claims+".x","refresh_token":"r"},{})
    assert not value["can_refresh"] and value["material_guidance"]=="iam_requires_official_authorization"


@pytest.mark.parametrize("change", ["lease", "attempt", "credential"])
def test_late_ledger_write_is_rejected_before_mutation(env, change):
    store, now = env
    aid=account(store);job=running(store,aid,"renew")
    with store.tx() as (s,_):
        if change=="lease": s.get(Job,job["id"]).lease_until=now[0]-1
        elif change=="attempt": s.get(Job,job["id"]).attempt=2
        else: s.get(Account,aid).credential_version=2
    with pytest.raises(DomainError):
        store.operation_ledger(aid,"must-not-write",{"value":1},fence=job)
    assert store.operation_ledger(aid,"must-not-write") is None


def test_ambiguous_refresh_is_never_replayed_after_worker_restart(env):
    store, _ = env
    aid=account(store);adapter=Adapter()
    first=running(store,aid,"renew")
    asyncio.run(worker(store,adapter).execute_real(first))
    assert adapter.renew_calls==1
    second=running(store,aid,"renew")
    asyncio.run(worker(store,adapter).execute_real(second))
    assert adapter.renew_calls==1
    with store.read() as (s,_):
        assert s.get(Job,second["id"]).error_code=="previous_operation_requires_review"


def test_issued_candidate_survives_restart_without_second_refresh(env):
    store, _ = env
    aid=account(store);job=running(store,aid,"renew")
    store.renewal_candidate(job,{"session":"already-issued"})
    adapter=Adapter()
    asyncio.run(worker(store,adapter).execute_real(job))
    assert adapter.renew_calls==0
    with store.read() as (s,_):
        assert s.get(Account,aid).credential_version==2
        assert s.get(Job,job["id"]).status=="succeeded"
        candidate=s.scalar(select(Resource).where(Resource.kind=="renewal_candidate",Resource.name==job["id"]))
        assert candidate.secret is None and json.loads(candidate.data)["status"]=="activated"


def test_cancelled_refresh_keeps_intent_and_cannot_rotate_again(env):
    store,now=env
    aid=account(store);job=running(store,aid,"renew")
    class Cancelled(Adapter):
        async def renew(self):
            self.renew_calls+=1
            raise asyncio.CancelledError()
    adapter=Cancelled()
    with pytest.raises(asyncio.CancelledError): asyncio.run(worker(store,adapter).execute_real(job))
    now[0]+=121
    with store.tx() as (s,state): store.recover_expired(s,state)
    second=running(store,aid,"renew")
    asyncio.run(worker(store,adapter).execute_real(second))
    assert adapter.renew_calls==1


def test_already_issued_registration_result_commits_without_transport(env):
    store,_=env
    aid=account(store)
    def forbidden(**kwargs): raise AssertionError("issued receipt must not access upstream")
    service=RegistrationService(store,SimpleNamespace(enabled=True,transport_factory=forbidden))
    sid=service.create("Loomy","isolated","sms",{"phone":"13800000001"},aid)
    with store.tx() as (s,_):
        row=s.get(Resource,sid);data=json.loads(row.data)
        data.update(status="uncertain",result_unresolved=True)
        row.data=json.dumps(data)
        material=json.loads(store.cipher.decrypt(row.secret.encode()))
        material["issued_result"]={"identity":aid,"credential":{"session":"already-issued"}}
        row.secret=store.cipher.encrypt(json.dumps(material).encode()).decode()
    result=asyncio.run(service.run(sid,continuation=True))
    assert result["status"]=="succeeded"
    with store.read() as (s,_): assert s.get(Account,aid).credential_version==2


def test_three_confirmed_no_sms_days_stop_original_identity(env):
    store,now=env
    aid=account(store)
    service=RegistrationService(store,SimpleNamespace(enabled=True))
    for index in range(3):
        sid=service.create("Loomy","isolated","sms",{"phone":"13800000001"},aid)
        token=store.acquire_execution(sid,8)
        data,_=service.transition(sid,"prepared","sending",token)
        service.finish(sid,data["operation"],result={"challenge":"isolated"})
        store.release_execution(sid,token)
        now[0]+=301
        service.expire_sms(sid)
        with store.read() as (s,_): assert s.get(Account,aid).relogin_failures==index+1
        now[0]+=86400
    plan=service.recovery.plan(aid)
    assert plan["action"]=="stopped" and plan["reason"]=="relogin_attempts_exhausted"


def test_provider_credentials_swap_and_expired_token_refresh_are_private(env):
    store,_=env
    sms=SmsChannels(store,SimpleNamespace(enabled=True))
    cid=sms.configure(ChannelInput(name="isolated",provider="haozhuma",base_url="https://sms.invalid/api",token="old",project="project"))
    calls=[]
    async def upstream(channel,action,**params):
        calls.append(action)
        if action=="login": return {"token":"exchanged-"+str(len(calls))}
        if channel["token"]=="exchanged-1": raise UpstreamError("sms_channel_credentials_invalid")
        return {"money":"8"}
    sms._call=upstream
    asyncio.run(sms.credential_login(cid,ProviderLogin(user="user",password="private-password",expected_version=1,remember=True)))
    value=asyncio.run(sms.call(sms.read(cid),"getSummary"))
    assert value["money"]=="8" and calls==["login","getSummary","login","getSummary"]
    public=json.dumps(store.resources("sms_channel"))
    assert "private-password" not in public and "exchanged-" not in public
    before=len(calls)
    with pytest.raises(DomainError):
        asyncio.run(sms.credential_login(cid,ProviderLogin(user="user",password="private-password",expected_version=1)))
    assert len(calls)==before


def test_checkin_immediately_queues_balance_and_unknown_is_not_zero(env):
    store, _ = env
    aid=account(store);store.real_capabilities["Loomy"]={"checkin","balance"}
    job=running(store,aid,"checkin")
    asyncio.run(worker(store,Adapter()).execute_real(job))
    with store.read() as (s,_):
        a=s.get(Account,aid)
        assert a.checkin_day==job["day"] and a.balance is None
        balance=s.scalar(select(Job).where(Job.account_id==aid,Job.kind=="balance"))
        assert balance.priority==5 and balance.status=="queued"
    with pytest.raises(DomainError) as failure: store.plan(aid,"checkin")
    assert failure.value.code=="already_checked_in"


def test_benefits_partial_success_still_queues_balance(env):
    store,_=env
    credential={"account":{"uid":"u"},"auth":{"accessToken":"new"}}
    aid=account(store,"WorkBuddy 国内",credential)
    store.real_capabilities["WorkBuddy 国内"]={"benefits","balance"}
    job=running(store,aid,"benefits");adapter=Adapter(credential)
    asyncio.run(worker(store,adapter).execute_real(job))
    with store.read() as (s,_):
        result=s.get(Job,job["id"])
        assert result.status=="failed" and result.error_code=="benefits_material_required"
        followup=s.scalar(select(Job).where(Job.account_id==aid,Job.kind=="balance"))
        assert followup is not None and followup.priority==5


def test_uncertain_benefit_resume_only_reads_and_gates_enterprise():
    adapter=Adapter();adapter.ambiguous=True
    first=asyncio.run(benefits(adapter,"WorkBuddy 国内"))
    assert not first["settled"] and first["error_code"]=="benefits_uncertain"
    posts=len(adapter.posts)
    second=asyncio.run(benefits(adapter,"WorkBuddy 国内",reconcile=True))
    assert len(adapter.posts)==posts and not second["settled"]
    for platform,credential in [("WorkBuddy 国际",{}),("WorkBuddy 国内",{"account":{"enterpriseId":"e"}})]:
        with pytest.raises(UpstreamError): asyncio.run(benefits(Adapter(credential),platform))


def test_package_zero_missing_and_official_expiry_priority():
    result=packages({"Response":{"Data":{"Accounts":[
        {"PackageName":"zero","CycleCapacitySize":10,"CycleCapacityRemain":0,"DeductionEndTime":1796000000000,"CycleEndTime":"2027-01-01 00:00:00"},
        {"PackageName":"unknown"}]}}})
    assert result[0]["remaining"]==0 and result[0]["expiry_source"]=="DeductionEndTime"
    assert result[0]["expires_at"]==1796000000
    assert result[1]["remaining"] is None and result[1]["expires_at"] is None


def test_registration_status_group_uses_identical_json_expressions(env):
    store, _ = env
    with store.tx() as (s, _):
        for number, status in enumerate(("succeeded", "succeeded", "prepared")):
            s.add(Resource(id=f"group-{number}", kind="registration", name="group",
                data=json.dumps({"status": status}), updated_at=number))
    with store.read() as (s, _):
        # Separate helper calls reproduce PostgreSQL's bound-key grouping failure.
        rows = s.execute(select(field(s, "status"), func.count())
            .where(Resource.kind == "registration").group_by(field(s, "status"))).all()
        assert dict(rows) == {"succeeded": 2, "prepared": 1}
        with pytest.raises(ValueError):
            field(s, "status' OR 1=1 --")


def test_registration_indexes_upgrade_and_do_not_parse_other_resources(env):
    store,_=env
    old_names=["ix_resources_registration_"+suffix for suffix in ("account","status","expiry","batch")]
    with store.engine.begin() as connection:
        old_expression = "json_extract(data,'$.status')" if connection.dialect.name=='sqlite' else "(CAST(data AS JSON)->>'status')"
        for name in old_names:
            connection.exec_driver_sql(f"CREATE INDEX {name} ON resources (kind, {old_expression})")
        ensure_registration_indexes(connection)
        ensure_registration_indexes(connection)
    with store.tx() as (session,_):
        session.add(Resource(id='broken-unrelated',kind='key_policy',name='damaged',data='broken',updated_at=1))
        session.add(Resource(id='healthy-registration',kind='registration',name='healthy',data='{"status":"succeeded"}',updated_at=2))
        if store.engine.dialect.name=='sqlite':
            session.add(Resource(id='broken-registration',kind='registration',name='damaged',data='broken',updated_at=3))
    assert page(store,status='succeeded')['total']==1
    with store.engine.begin() as connection:
        ensure_registration_indexes(connection)
        if connection.dialect.name=='sqlite':
            indexes={row[1]:row[4] for row in connection.exec_driver_sql('PRAGMA index_list(resources)')}
        else:
            indexes=dict(connection.exec_driver_sql("SELECT indexname,position('WHERE' in indexdef)>0 FROM pg_indexes WHERE tablename='resources'").all())
        assert all(name not in indexes for name in old_names)
        assert all(indexes[name+'_v2'] for name in old_names)


def test_long_registration_and_import_history_are_sql_paged(env):
    store,_=env
    with store.tx() as (s,_):
        for number in range(1205):
            s.add(Resource(id=f"history-{number:05}",kind="registration",name="same",data=json.dumps({"status":"succeeded","account_id":"target"}),updated_at=number))
        s.add(Resource(id="open",kind="registration",name="same",data=json.dumps({"status":"prepared"}),updated_at=1))
        s.add(Resource(id="import-page",kind="account_import",name="same",version=1,data=json.dumps({"status":"preview","entries":[{"line":n} for n in range(1205)]}),updated_at=1))
    statements=[]
    def recorded(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
    event.listen(store.engine,"before_cursor_execute",recorded)
    try:
        result=page(store,5,25,account_id="target")
        assert result["total"]==1205 and len(result["sessions"])==25
        assert [r["id"] for r in runnable(store)]==["open"]
        app=FastAPI();mount_imports(app,store,SimpleNamespace(enabled=False))
        client=TestClient(app)
        response=client.get('/api/imports/import-page?page=5&page_size=25').json()
        assert response["total"]==1205 and [v["line"] for v in response["data"]["entries"]]==list(range(100,125))
        json_array_function = "json_each" if store.engine.dialect.name == "sqlite" else "json_array_elements"
        assert any(json_array_function in sql and "LIMIT" in sql for sql in statements)
    finally:
        event.remove(store.engine,"before_cursor_execute",recorded)


def test_import_activation_captures_official_name_but_not_independence(env):
    store,_=env
    requests=[]
    def response(request):
        requests.append(request.url.path)
        return httpx.Response(200,json={"code":0,"data":{"id":"wps-identity","nickname":"Official name"}})
    registry=SimpleNamespace(enabled=True,transport_factory=lambda **kwargs:Transport(httpx.AsyncClient(transport=httpx.MockTransport(response))))
    service=Imports(store,registry)
    preview=service.preview(PreviewInput(content=json.dumps([{"platform":"WPS 灵犀","credential":{"userid":"wps-identity","cookie":"wps_sid=isolated"}}])))
    result=service.commit(preview["id"],preview["version"])
    aid=result["entries"][0]["account_id"]
    with store.read() as (s,_):
        candidate=s.scalar(select(Resource).where(Resource.kind=="import_candidate",Resource.name==aid))
        cid=candidate.id
    activated=asyncio.run(service.verify(cid))
    assert activated["status"]=="activated" and len(requests)==1
    with store.read() as (s,_):
        a=s.get(Account,aid)
        assert a.original_label=="Official name" and control(s,a)["state"]=="verification_required"


def test_workbuddy_enterprise_selection_resumes_issued_token_without_exchange():
    from app.login_connectors import WorkBuddyLogin
    calls=[]
    def response(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={"code":0,"data":{"accessToken":"isolated","refreshToken":"refresh","domain":"workbuddy.cn"}})
        return httpx.Response(200,json={"code":0,"data":{"accounts":[{"uid":"same","enterpriseId":"e1"},{"uid":"same","enterpriseId":"e2"}]}})
    async def scenario():
        transport=Transport(httpx.AsyncClient(transport=httpx.MockTransport(response)))
        login=WorkBuddyLogin(transport,"WorkBuddy 国内")
        material={"state":"isolated-state"}
        async def checkpoint(patch): material.update(patch)
        login.checkpoint=checkpoint
        try:
            first=await login.verify(material,"official",None)
            assert first["waiting"]=="waiting_identity" and len(first["identities"])==2
            material["selected_identity"]=first["identities"][1]["selection_key"]
            second=await login.verify(material,"official",None)
            assert second["identity"]=="same:e2" and second["credential"]["account"]["enterpriseId"]=="e2"
            assert sum(path.endswith('/token') for path in calls)==1
        finally:
            await transport.close()
    asyncio.run(scenario())


def test_codearts_generates_private_material_and_rejects_wrong_ticket_without_network():
    from app.codearts_login import CodeArtsLogin
    class NoNetwork:
        async def json(self,*args,**kwargs): raise AssertionError("wrong state must fail before exchange")
    async def scenario():
        login=CodeArtsLogin(NoNetwork())
        started=await login.start({},"official")
        context=started["oauth_context"]
        assert context["pkce_pair"]["code_verifier"] and context["dpop_key_pair"]["private_key_jwk"]["d"]
        with pytest.raises(UpstreamError) as failure:
            await login.verify(started,"official",started["callback_url"]+'?code=isolated&ticket_id=wrong')
        assert failure.value.code=="oauth_state_mismatch"
    asyncio.run(scenario())


def test_credit_receipt_pagination_and_stale_version_are_explicit(env):
    store,_=env
    aid=account(store)
    store.operation_ledger(aid,"credit_packages",{"status":"verified","credential_version":1,"observed_at":1,
        "packages":[{"name":"first","remaining":0},{"name":"second","remaining":None}],"transactions_available":False})
    app=FastAPI();mount_task_receipts(app,store)
    client=TestClient(app)
    result=client.get(f'/api/accounts/{aid}/credit-packages?page=2&page_size=1').json()
    assert result["total"]==2 and result["packages"]==[{"name":"second","remaining":None}] and result["credential_current"]
    with store.tx() as (s,_): s.get(Account,aid).credential_version=2
    stale=client.get(f'/api/accounts/{aid}/credit-packages').json()
    assert stale["status"]=="stale" and not stale["credential_current"] and stale["credential_version"]==1


@pytest.mark.parametrize("platform,mode,old,new", [
    ("小浣熊", "official", {"token": "Bearer shared"}, {"access_token": "shared"}),
    ("WPS 灵犀", "browser", {"cookie": "wps_sid=shared; other=old"}, {"cookie": "other=new; wps_sid=shared"}),
])
def test_official_receipt_cannot_promote_reused_session_with_different_serialization(env, platform, mode, old, new):
    store, _ = env
    aid = account(store, platform, old)
    with store.tx() as (s, _):
        a = s.get(Account, aid)
        a.credential_version = 2
        state = record_native_login(store, s, a, mode, new, previous_version=1)
        assert state["state"] == "verification_required"
        assert state["reason"] == "official_login_reused_session_material"
        assert record_native_login(store, s, a, mode, new, previous_version=9)["reason"] == "previous_session_evidence_unavailable"
