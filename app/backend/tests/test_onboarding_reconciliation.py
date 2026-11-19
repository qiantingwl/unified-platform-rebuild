import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.adapters import Loomy
from app.models import Job
from app.onboarding import loomy_receipt
from app.store import DomainError, Store
from app.upstream import UpstreamError
from app.vendor.mirai.loomy.endpoints import ONBOARDING_TASKS
from app.worker import MaintenanceWorker


@pytest.fixture
def env(tmp_path):
    store=Store(f"sqlite:///{tmp_path/'receipt.db'}",Fernet.generate_key())
    store.real_capabilities["Loomy"]={"balance","onboarding"}
    aid=store.create_account("Loomy","synthetic","synthetic")
    store.put_credential(aid,json.dumps({"session":"synthetic"}),0)
    while job:=store.claim("setup",64):
        assert job["kind"]=="balance"
        store.finish(job["id"],"setup",job["attempt"],True,balance=100)
    job_id=store.plan(aid,"onboarding")
    job=store.claim("setup",64)
    store.finish(job_id,"setup",job["attempt"],False,error="onboarding_uncertain")
    followup=store.claim("setup",64)
    store.finish(followup["id"],"setup",followup["attempt"],True,balance=100)
    yield store,aid,job_id
    store.engine.dispose()


@pytest.mark.parametrize("progress,expected",[
    ({key:True for key in ONBOARDING_TASKS},"succeeded"),
    ({key:False for key in ONBOARDING_TASKS},"failed"),
    ({},"uncertain"),
    ({next(iter(ONBOARDING_TASKS)):False},"uncertain"),
])
def test_reconciliation_get_only_then_explicit_claim_if_unpaid(env,progress,expected):
    store,aid,job_id=env
    calls=[]
    class Adapter:
        base="https://upstream.example.test"
        t=SimpleNamespace(close=lambda:asyncio.sleep(0))
        def headers(self):
            return {}
        async def request(self,method,url,*args):
            calls.append(method)
            assert method=="GET"
            return {"tasks":copy.deepcopy(progress)}
        onboarding_receipt=Loomy.onboarding_receipt
        async def onboarding(self):
            raise AssertionError("reconciliation must not claim rewards")
    with pytest.raises(DomainError,match="previous_operation_requires_review"):
        store.plan(aid,"onboarding")
    assert store.reconcile_onboarding(job_id)==job_id
    job=store.claim("readback",64)
    assert job["reconcile_onboarding"] is True and job["attempt"]==2
    worker=MaintenanceWorker(store,registry=SimpleNamespace(make=lambda *args:Adapter()))
    asyncio.run(worker.execute_real(job))
    with store.read() as (s,_):
        assert s.get(Job,job_id).status==expected
    assert calls==["GET"]
    # A new readback creates a fresh balance observation instead of reusing the
    # completed balance job from the ambiguous first attempt.
    followup=store.claim("after",64)
    assert followup["kind"]=="balance"
    store.finish(followup["id"],"after",followup["attempt"],True,balance=101)
    assert store.claim("after",64) is None
    if expected=="uncertain":
        with pytest.raises(DomainError,match="previous_operation_requires_review"):
            store.plan(aid,"onboarding")
    elif expected=="failed":
        new_job=store.plan(aid,"onboarding")
        assert new_job!=job_id
        assert store.claim("explicit",64)["reconcile_onboarding"] is False


@pytest.mark.parametrize("error",["network","invalid_credentials","unconfirmed_onboarding"])
def test_failed_read_keeps_uncertain_and_cannot_enable_reward_replay(env,error):
    store,aid,job_id=env
    class Adapter:
        t=SimpleNamespace(close=lambda:asyncio.sleep(0))
        async def onboarding_receipt(self):
            raise UpstreamError(error)
    store.reconcile_onboarding(job_id)
    job=store.claim("readback",64)
    asyncio.run(MaintenanceWorker(store,registry=SimpleNamespace(make=lambda *args:Adapter())).execute_real(job))
    with store.read() as (s,_):
        saved=s.get(Job,job_id)
        assert saved.status=="uncertain" and saved.error_code==error
    with pytest.raises(DomainError,match="previous_operation_requires_review"):
        store.plan(aid,"onboarding")


def test_reconciliation_rejects_missing_wrong_platform_and_duplicate_running(env):
    store,aid,job_id=env
    with pytest.raises(DomainError,match="job_not_safely_retryable"):
        store.reconcile_onboarding("missing")
    with store.tx() as (s,_):
        store.account(s,aid).platform="小浣熊"
    with pytest.raises(DomainError,match="job_not_safely_retryable"):
        store.reconcile_onboarding(job_id)
    with store.tx() as (s,_):
        store.account(s,aid).platform="Loomy"
    store.reconcile_onboarding(job_id)
    with pytest.raises(DomainError,match="job_not_safely_retryable"):
        store.reconcile_onboarding(job_id)
    store.claim("readback",64)
    with pytest.raises(DomainError,match="job_not_safely_retryable"):
        store.reconcile_onboarding(job_id)


def test_receipt_never_trusts_old_settled_ledger():
    calls=[]
    class Adapter:
        base="https://upstream.example.test"
        def headers(self):
            return {}
        async def ledger(self,key,value=None):
            return {"settled":True}
        async def request(self,method,url,*args):
            calls.append(method)
            return {"tasks":{key:False for key in ONBOARDING_TASKS}}
    result=asyncio.run(loomy_receipt(Adapter()))
    assert result["settled"] is False and result["claimed"]==0 and calls==["GET"]
