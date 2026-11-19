"""Port recovery/receipt semantics from the existing 3067 task implementation."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Account, Job, Resource
from app.registration import RegistrationService
from app.registration_runner import RegistrationRunner
from app.store import Store


@pytest.fixture
def env(tmp_path):
    store=Store(f"sqlite:///{tmp_path/'receipt-dispatch.db'}",Fernet.generate_key(),clock=lambda:1794900000.)
    store.real_capabilities["Loomy"]={"checkin","balance","chat"}
    def unexpected_transport(**kwargs):
        raise AssertionError("local receipt recovery must not contact an upstream")
    service=RegistrationService(store,SimpleNamespace(enabled=True,transport_factory=unexpected_transport))
    store.resource_put("settings","runtime",{"maintenance_capacity":1,"auto_relogin":False})
    yield store,service,RegistrationRunner(service,None)
    store.engine.dispose()


def session(service,status,**material):
    sid=service.create("Loomy","synthetic","sms",{"phone":"13800000001","auto_run":True,**material})
    with service.store.tx() as (s,_):
        row=s.get(Resource,sid)
        data=json.loads(row.data)
        data.update(status=status,expires_at=service.store.clock()+300)
        row.data=json.dumps(data)
    return sid


async def tick(runner):
    await runner.tick()
    await asyncio.gather(*list(runner.tasks.values()))
    await asyncio.sleep(0)


def test_complete_issued_receipt_recovers_locally_and_enqueues_maintenance(env):
    store,service,runner=env
    sid=session(service,"uncertain",issued_result={"identity":"issued-id","credential":{"session":"synthetic","userid":"issued-id"}})
    asyncio.run(tick(runner))
    data,_=service.details(sid)
    assert data["status"]=="succeeded"
    with store.read() as (s,_):
        account=s.get(Account,data["account_id"])
        assert account.credential_status=="pending" and account.credential_version==1
        assert list(s.scalars(select(Job).where(Job.account_id==account.id)))
    asyncio.run(tick(runner))
    assert len(store.resources("registration"))==1


@pytest.mark.parametrize("material",[{}, {"issued_token":"synthetic"}, {"issued_result":{"identity":"issued-id"}}, {"issued_result":{"identity":"issued-id","credential":{}}}])
def test_ambiguous_or_incomplete_result_never_replays_login(env,material):
    _,service,runner=env
    sid=session(service,"uncertain",**material)
    called=[]
    async def one(sid):
        called.append(sid)
    runner.one=one
    asyncio.run(tick(runner))
    assert called==[] and service.details(sid)[0]["status"]=="uncertain"


def test_waiting_sessions_rotate_and_precede_terminal_cleanup(env):
    store,service,runner=env
    terminal=session(service,"failed",sms_lease_id="release-pending")
    with store.tx() as (s,_):
        s.get(Resource,terminal).updated_at=store.clock()-100
    waiting=[session(service,"waiting_code",sms_lease_id=f"waiting-{i}") for i in range(3)]
    called=[]
    async def one(sid):
        called.append(sid)
    runner.one=one
    async def run():
        for _ in range(3):
            await tick(runner)
    asyncio.run(run())
    assert set(called)==set(waiting) and len(called)==3
    assert terminal not in called


def test_explicitly_rejected_issued_receipt_is_not_automatically_retried(env):
    _,service,runner=env
    sid=session(service,"failed",issued_result={"identity":"wrong-id","credential":{"session":"synthetic"}})
    with service.store.tx() as (s,_):
        row=s.get(Resource,sid)
        data=json.loads(row.data)
        data.update(error="identity_mismatch",result_unresolved=True)
        row.data=json.dumps(data)
    called=[]
    async def one(sid):
        called.append(sid)
    runner.one=one
    asyncio.run(tick(runner))
    assert called==[]
