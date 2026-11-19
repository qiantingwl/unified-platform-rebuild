import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import Account, Resource
from app.registration import RegistrationService
from app.registration_automation import AutomationInput, RegistrationAutomation, mount_registration_automation
from app.registration_runner import BatchInput, RegistrationRunner
from app.sms_channels import ChannelInput
from app.store import DomainError, Store


@pytest.fixture
def env(tmp_path):
    now=[1794900000.]
    store=Store(f"sqlite:///{tmp_path/'auto.db'}",Fernet.generate_key(),clock=lambda:now[0])
    service=RegistrationService(store,SimpleNamespace(enabled=True))
    runner=RegistrationRunner(service,None)
    channel=service.sms.configure(ChannelInput(name="fixture",provider="eomsg",base_url="https://sms.example.test/api",token="synthetic-token",keyword="fixture"))
    auto=RegistrationAutomation(runner)
    body=AutomationInput(name="scheduled",platform="Loomy",count=2,parallel=2,sms_channel_id=channel,interval_minutes=1)
    yield store,service,runner,auto,body,now
    store.engine.dispose()


def change(store,rid,**changes):
    with store.tx() as (s,_):
        row=s.get(Resource,rid)
        row.data=json.dumps({**json.loads(row.data),**changes})


def rowdata(store,rid):
    with store.read() as (s,_):
        return json.loads(s.get(Resource,rid).data)


def enable(auto,body):
    return auto.save(body.model_copy(update={"enabled":True}))


def test_default_disabled_and_preview_do_not_write_or_call_upstream(env):
    store,service,runner,auto,body,now=env
    before=store.revision()
    assert auto.get()["default_enabled"] is False
    assert auto.preview(body)["reason"]=="disabled"
    assert auto.preview(body.model_copy(update={"enabled":True}))["would_create"] is True
    assert auto.tick()==0 and store.revision()==before
    assert not store.resources("registration") and not store.resources("registration_batch")
    auto.save(body)
    now[0]+=86400
    assert auto.tick()==0


def test_due_and_creation_atomic_across_workers_and_restart(env):
    store,service,runner,auto,body,now=env
    saved=enable(auto,body)
    assert auto.tick()==0
    now[0]+=61
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes=list(pool.map(lambda _:RegistrationAutomation(RegistrationRunner(service,None)).tick(),range(6)))
    assert sum(outcomes)==1
    batches=store.resources("registration_batch")
    assert len(batches)==1 and len(store.resources("registration"))==2
    assert batches[0]["data"]["automation_id"]==saved["id"]
    assert auto.get()["policies"][0]["version"]==saved["version"]
    now[0]+=3600
    assert RegistrationAutomation(RegistrationRunner(service,None)).tick()==0
    assert len(store.resources("registration_batch"))==1


def test_no_catchup_storm_and_existing_batch_budgets_reused(env):
    store,service,runner,auto,body,now=env
    enable(auto,body.model_copy(update={"number_budget":2,"max_consecutive_failures":2,"max_consecutive_skips":2}))
    now[0]+=10000
    assert auto.tick()==1
    batch=store.resources("registration_batch")[0]
    assert batch["data"]["number_budget"]==2
    for sid in batch["data"]["sessions"]:
        change(store,sid,status="failed",error="no_sms",finished_at=now[0])
    runner.refill_batches()
    assert rowdata(store,batch["id"])["status"]=="paused"
    now[0]+=10000
    assert auto.tick()==0
    assert auto.get()["policies"][0]["preview"]["reason"]=="existing_batch_paused"


@pytest.mark.parametrize("status",["failed","cancelled","uncertain"])
def test_unresolved_results_reserve_batch_slots_even_if_terminal_label(env,status):
    store,service,runner,auto,body,now=env
    bid=runner.make_batch(BatchInput(**{k:getattr(body,k) for k in BatchInput.model_fields}))
    original=rowdata(store,bid)["sessions"]
    for sid in original:
        change(store,sid,status=status,result_unresolved=True,finished_at=now[0])
    runner.refill_batches()
    assert rowdata(store,bid)["sessions"]==original
    progress=runner.batches()[0]["progress"]
    assert progress["inflight"]==2 and progress["failure_streak"]==0


def test_missing_session_is_not_permission_to_buy_replacement():
    p=RegistrationRunner.progress({"sessions":["missing"]},{})
    assert p["attempts"]==1 and p["inflight"]==1 and p["succeeded"]==0


@pytest.mark.parametrize("status",["active","paused"])
def test_manual_batch_prevents_automatic_overlap(env,status):
    store,service,runner,auto,body,now=env
    bid=runner.make_batch(BatchInput(**{k:getattr(body,k) for k in BatchInput.model_fields}))
    change(store,bid,status=status)
    enable(auto,body)
    now[0]+=61
    assert auto.tick()==0 and len(store.resources("registration_batch"))==1


def test_cancelled_batch_requires_explicit_reenable_and_waits_for_started_sessions(env):
    store,service,runner,auto,body,now=env
    saved=enable(auto,body)
    now[0]+=61
    assert auto.tick()==1
    batch=store.resources("registration_batch")[0]
    change(store,batch["id"],status="cancelled")
    now[0]+=61
    assert auto.tick()==0
    assert auto.get()["policies"][0]["preview"]["reason"]=="automatic_batch_cancelled"
    disabled=auto.save(body.model_copy(update={"expected_version":saved["version"]}))
    auto.save(body.model_copy(update={"enabled":True,"expected_version":disabled["version"]}))
    now[0]+=61
    assert auto.tick()==0  # the cancelled batch's started/unsettled sessions still exist
    for sid in batch["data"]["sessions"]:
        change(store,sid,status="cancelled")
    now[0]+=61
    assert auto.tick()==1


def test_inventory_counts_accounts_not_zero_balance_or_temporary_model_availability(env):
    store,service,runner,auto,body,now=env
    aid=store.create_account("Loomy","synthetic","fixture")
    with store.tx() as (s,_):
        account=s.get(Account,aid)
        account.credential_version,account.credential_status,account.balance=1,"valid",0
    inventory=body.model_copy(update={"enabled":True,"mode":"inventory","inventory_target":2,"count":10})
    preview=auto.preview(inventory)
    assert preview["inventory"]==1 and preview["count"]==1
    auto.save(inventory)
    now[0]+=61
    assert auto.tick()==1
    assert store.resources("registration_batch")[0]["data"]["target"]==1


def test_sufficient_inventory_and_pending_independent_registration_no_new_batch(env):
    store,service,runner,auto,body,now=env
    sid=service.create("Loomy","existing","register",{"sms_channel_id":body.sms_channel_id})
    inventory=body.model_copy(update={"enabled":True,"mode":"inventory","inventory_target":1})
    assert auto.preview(inventory)["pending"]==1
    auto.save(inventory)
    now[0]+=61
    assert auto.tick()==0
    change(store,sid,status="failed",result_unresolved=True)
    assert auto.preview(inventory)["reason"]=="registration_result_unresolved"


def test_disabled_external_execution_does_not_advance_due_or_create(env):
    store,service,runner,auto,body,now=env
    enable(auto,body)
    now[0]+=61
    before=auto.get()["policies"][0]["runtime"]["next_run_at"]
    service.registry.enabled=False
    assert auto.tick()==0
    assert auto.get()["policies"][0]["runtime"]["next_run_at"]==before
    assert auto.get()["policies"][0]["preview"]["reason"]=="upstream_disabled"


def test_save_version_conflict_and_disable_preserves_existing_batch(env):
    store,service,runner,auto,body,now=env
    saved=enable(auto,body)
    with pytest.raises(DomainError,match="version_conflict"):
        enable(auto,body)
    now[0]+=61
    auto.tick()
    auto.save(body.model_copy(update={"expected_version":saved["version"]}))
    now[0]+=10000
    assert auto.tick()==0
    assert store.resources("registration_batch")[0]["data"]["status"]=="active"


def test_disabled_channel_after_save_is_visible_and_does_not_create(env):
    store,service,runner,auto,body,now=env
    enable(auto,body)
    change(store,body.sms_channel_id,enabled=False)
    now[0]+=61
    assert auto.tick()==0
    assert auto.get()["policies"][0]["runtime"]["last_reason"]=="sms_channel_disabled"


def test_partial_batch_creation_rolls_back_and_retains_failure(env,monkeypatch):
    store,service,runner,auto,body,now=env
    enable(auto,body)
    original=service.create
    calls=[]
    def broken(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:
            raise DomainError("missing_proxy")
        return original(*args,**kwargs)
    monkeypatch.setattr(service,"create",broken)
    now[0]+=61
    assert auto.tick()==0
    assert not store.resources("registration_batch") and not store.resources("registration")
    assert auto.get()["policies"][0]["runtime"]["last_error"]=="missing_proxy"


def test_module_api_preview_is_read_only_and_put_does_not_start_batch(env):
    store,service,runner,auto,body,now=env
    app=FastAPI()
    mount_registration_automation(app,runner)
    with TestClient(app) as client:
        assert client.get("/api/registration-automation").json()["policies"]==[]
        assert client.post("/api/registration-automation/preview",json=body.model_dump()).status_code==200
        assert not store.resources("registration_automation")
        result=client.put("/api/registration-automation",json=body.model_copy(update={"enabled":True}).model_dump())
        assert result.status_code==200 and result.json()["version"]==1
        assert not store.resources("registration_batch")
