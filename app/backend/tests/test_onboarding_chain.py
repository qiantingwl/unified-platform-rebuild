import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Account,Job,Resource
from app.onboarding import loomy_claim,raccoon_claim
from app.store import DomainError,Store
from app.upstream import UpstreamError
from app.vendor.mirai.loomy.endpoints import ONBOARDING_TASKS
from app.worker import MaintenanceWorker


class FixtureAdapter:
    base="https://upstream.example.test"
    def __init__(self,request):
        self.request=request
        self.saved=None
    def headers(self):
        return {"Authorization":"synthetic"}
    async def ledger(self,key,value=None):
        assert key=="onboarding"
        if value is not None:
            self.saved=copy.deepcopy(value)
        return copy.deepcopy(self.saved)


def test_loomy_checks_before_claim_and_settled_memory_is_zero_network():
    calls=[]
    key=next(iter(ONBOARDING_TASKS))
    async def request(method,url,headers,body=None):
        calls.append((method,body))
        if method=="GET":
            return {"tasks":{k:k!=key for k in ONBOARDING_TASKS}}
        assert body=={"key":key}
        assert next(t for t in adapter.saved["tasks"] if t["key"]==key)["status"]=="claiming"
        return {"alreadyCompleted":False,"balance":100}
    adapter=FixtureAdapter(request)
    result=asyncio.run(loomy_claim(adapter))
    assert result["settled"] and result["claimed"]==1
    assert asyncio.run(loomy_claim(adapter))["claimed"]==0
    assert len(calls)==2


def test_loomy_missing_progress_is_not_permission_to_claim():
    calls=[]
    async def request(method,*args):
        calls.append(method)
        return {"tasks":{}}
    result=asyncio.run(loomy_claim(FixtureAdapter(request)))
    assert not result["settled"] and result["failed"]==len(ONBOARDING_TASKS)
    assert calls==["GET"]


def test_loomy_lost_claim_can_be_resolved_by_read_only_progress():
    key=next(iter(ONBOARDING_TASKS))
    posts=[]
    async def request(method,url,headers,body=None):
        if method=="GET":
            return {"tasks":{k:bool(posts) or k!=key for k in ONBOARDING_TASKS}}
        posts.append(body)
        raise UpstreamError("network")
    adapter=FixtureAdapter(request)
    assert not asyncio.run(loomy_claim(adapter))["settled"]
    assert asyncio.run(loomy_claim(adapter))["settled"]
    assert len(posts)==1


@pytest.mark.parametrize("reply",[{"granted":False},{"granted":True},{"granted":True,"popup":{"points":123}}])
def test_raccoon_receipts_are_durable_and_never_invent_points(reply):
    calls=[]
    async def request(method,url,headers,body=None):
        assert method=="POST" and any(t.get("status")=="claiming" for t in adapter.saved["tasks"])
        calls.append((url,headers))
        return reply
    adapter=FixtureAdapter(request)
    result=asyncio.run(raccoon_claim(adapter))
    assert result["settled"] and len(calls)==2
    expected=123 if reply.get("popup") else None if reply["granted"] else 0
    assert all(t["points"]==expected for t in result["tasks"])
    assert calls[0][1]["X-Client-Platform"]=="desktop-linux"
    assert calls[1][1]["X-Client-Platform"]=="app-android"
    assert asyncio.run(raccoon_claim(adapter))["settled"] and len(calls)==2


@pytest.mark.parametrize("failure",["network","upstream_timeout","response_read_failed","unconfirmed_onboarding"])
def test_raccoon_uncertain_write_is_not_replayed(failure):
    calls=[]
    async def request(method,url,*args):
        calls.append(url)
        if "/desktop/" in url:
            if failure=="unconfirmed_onboarding":
                return {}  # HTTP success with missing granted is not a receipt
            raise UpstreamError(failure)
        return {"granted":False}
    adapter=FixtureAdapter(request)
    result=asyncio.run(raccoon_claim(adapter))
    assert not result["settled"] and result["failed"]==1
    assert result["tasks"][0]["status"]=="uncertain"
    asyncio.run(raccoon_claim(adapter))
    assert len(calls)==2


def test_raccoon_crash_after_intent_keeps_unknown_instead_of_replaying():
    calls=[]
    async def request(method,url,*args):
        calls.append(url)
        if "/desktop/" in url:
            raise asyncio.CancelledError()
        return {"granted":False}
    adapter=FixtureAdapter(request)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(raccoon_claim(adapter))
    result=asyncio.run(raccoon_claim(adapter))
    assert result["tasks"][0]["status"]=="uncertain" and len(calls)==2


@pytest.fixture
def env(tmp_path):
    now=[1794900000.]
    store=Store(f"sqlite:///{tmp_path/'onboarding.db'}",Fernet.generate_key(),clock=lambda:now[0])
    store.real_capabilities["Loomy"]={"checkin","balance","onboarding","chat"}
    aid=store.create_account("Loomy","synthetic","synthetic")
    store.put_credential(aid,json.dumps({"session":"synthetic"}),0)
    yield store,aid,now
    store.engine.dispose()


def mark_new(store,aid):
    store.resource_put("onboarding_pending",aid,{"status":"pending","source":"registration","session_id":"synthetic"})


@pytest.mark.parametrize("outcome",["succeeded","failed","uncertain"])
def test_signup_checkin_balance_onboarding_balance_chain(env,outcome):
    store,aid,_=env
    mark_new(store,aid)
    seen=[]
    class Adapter:
        t=SimpleNamespace(close=lambda:asyncio.sleep(0))
        async def checkin(self,day):
            return {"day":day}
        async def balance(self):
            return 100 if seen.count("balance")==1 else 101
        async def onboarding(self):
            with store.read() as (s,_):
                assert s.get(Account,aid).balance==100
            tasks=[{"status":"settled" if outcome=="succeeded" else outcome}]
            return {"settled":outcome=="succeeded","failed":int(outcome!="succeeded"),"tasks":tasks}
    worker=MaintenanceWorker(store,registry=SimpleNamespace(make=lambda *args:Adapter()))
    async def run():
        while job:=store.claim("worker",64):
            seen.append(job["kind"])
            await worker.execute_real(job)
    asyncio.run(run())
    assert seen==["checkin","balance","onboarding","balance"]
    with store.read() as (s,_):
        account=s.get(Account,aid)
        assert account.balance==101 and account.credential_status=="valid"
        job=s.scalar(select(Job).where(Job.kind=="onboarding"))
        assert job.status==outcome
    assert store.resources("onboarding_pending")[0]["data"]["status"]==outcome


def test_imported_account_has_no_automatic_onboarding(env):
    store,aid,_=env
    checkin=store.claim("w",64)
    store.finish(checkin["id"],"w",checkin["attempt"],True)
    balance=store.claim("w",64)
    store.finish(balance["id"],"w",balance["attempt"],True,balance=100)
    assert store.claim("w",64) is None


def test_failed_checkin_still_verifies_balance_before_onboarding(env):
    store,aid,_=env
    mark_new(store,aid)
    checkin=store.claim("w",64)
    store.finish(checkin["id"],"w",checkin["attempt"],False,error="network")
    store.reconcile_maintenance()
    balance=store.claim("w",64)
    assert balance["kind"]=="balance"
    store.finish(balance["id"],"w",balance["attempt"],True,balance=100)
    reward=store.claim("w",64)
    assert reward["kind"]=="onboarding"


def test_lost_onboarding_worker_keeps_uncertain_and_still_queries_balance(env):
    store,aid,now=env
    mark_new(store,aid)
    checkin=store.claim("w",64)
    store.finish(checkin["id"],"w",checkin["attempt"],True)
    balance=store.claim("w",64)
    store.finish(balance["id"],"w",balance["attempt"],True,balance=100)
    reward=store.claim("w",64)
    assert reward["kind"]=="onboarding"
    now[0]+=31
    follow=store.claim("next",64)
    assert follow["kind"]=="balance"
    store.finish(follow["id"],"next",follow["attempt"],True,balance=120)
    assert store.claim("next",64) is None
    with pytest.raises(DomainError,match="previous_operation_requires_review"):
        store.plan(aid,"onboarding")
