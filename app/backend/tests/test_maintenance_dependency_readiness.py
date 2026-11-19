import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Account,Job,Resource
from app.operations import DEFAULT_SETTINGS,Supervisor
from app.store import Store


@pytest.fixture
def env(tmp_path):
    now=[1794801600.0]
    store=Store(f"sqlite:///{tmp_path/'readiness.db'}",Fernet.generate_key(),clock=lambda:now[0])
    store.real_capabilities["Loomy"]={"balance","checkin"}
    aid=store.create_account("Loomy","fixture","fixture")
    store.put_credential(aid,json.dumps({"session":"fixture"}),0)
    for balance in (None,42):
        job=store.claim("worker")
        store.finish(job["id"],job["owner"],job["attempt"],True,**({"balance":balance} if balance is not None else {}))
    pool=store.resource_put("proxy_pool","empty",{"proxy_ids":[]})
    route=store.resource_put("maintenance_proxy_route","Loomy:balance",{"mode":"pool","pool_id":pool})
    now[0]+=1
    store.plan(aid,"balance")
    job=store.claim("worker")
    store.finish(job["id"],job["owner"],job["attempt"],False,error="proxy_pool_unavailable")
    now[0]+=1801
    supervisor=Supervisor(store,SimpleNamespace(capacity=8),SimpleNamespace(),".")
    yield store,supervisor,aid,route,now
    store.engine.dispose()


def tick(supervisor):
    supervisor.schedule({**DEFAULT_SETTINGS,"refresh_balances":True})


def test_known_empty_pool_does_not_generate_repeated_failed_tasks(env):
    store,supervisor,aid,route,now=env
    with store.read() as (s,_):
        before=len(list(s.scalars(select(Job))))
    for _ in range(3):
        tick(supervisor)
        now[0]+=1801
    with store.read() as (s,_):
        assert len(list(s.scalars(select(Job))))==before
        account=s.get(Account,aid)
        assert account.enabled and account.credential_status=="valid" and account.concurrency==1


def test_route_repair_requeues_on_next_pass_without_new_cooldown(env):
    store,supervisor,aid,route,now=env
    tick(supervisor)
    assert store.claim("worker") is None
    store.resource_put("maintenance_proxy_route","Loomy:balance",{"mode":"direct"},resource_id=route,expected_version=1)
    tick(supervisor)
    job=store.claim("worker")
    assert job and job["kind"]=="balance"


def test_manual_query_and_other_errors_keep_existing_schedule(env):
    store,supervisor,aid,route,now=env
    tick(supervisor)
    store.plan(aid,"balance")
    job=store.claim("worker")
    assert job
    store.finish(job["id"],job["owner"],job["attempt"],False,error="unknown_403")
    now[0]+=1801
    tick(supervisor)
    assert store.claim("worker") is not None


def test_current_target_probe_can_restore_existing_pool_next_pass(env):
    store,supervisor,aid,route,now=env
    node=store.resource_put("proxy","target",{"status":"active","target_check_required":True},secret="http://proxy.example.test:8888")
    with store.read() as (s,_):
        pool=json.loads(s.get(Resource,route).data)["pool_id"]
    store.resource_put("proxy_pool","empty",{"proxy_ids":[node]},resource_id=pool,expected_version=1)
    tick(supervisor)
    assert store.claim("worker") is None
    store.resource_put("proxy_target_check",f"{node}:Loomy",{
        "eligible":True,"status":"completed","node_version":1,
        "platform":"Loomy","checked_at":now[0]})
    tick(supervisor)
    assert store.claim("worker") is not None


def test_periodic_balance_rechecks_when_manual_query_is_queued_after_snapshot(env, monkeypatch):
    store, supervisor, aid, route, now = env
    store.resource_put("maintenance_proxy_route", "Loomy:balance", {"mode": "direct"}, resource_id=route, expected_version=1)
    read = store.read
    config = {**DEFAULT_SETTINGS, "refresh_balances": True}
    manual = []

    @contextmanager
    def read_then_queue():
        with read() as pair:
            yield pair
        if not manual:
            manual.append(store.plan(aid, "balance"))

    monkeypatch.setattr(store, "read", read_then_queue)
    supervisor.schedule_periodic_balances(config, now[0])
    with read() as (s, _):
        queued = list(s.scalars(select(Job).where(Job.status == "queued")))
        assert len(queued) == 1
        assert queued[0].id == manual[0]


def test_claim_does_not_lock_queued_jobs_for_a_busy_account(env, monkeypatch):
    store, _, aid, _, _ = env
    store.plan(aid, "balance")
    assert store.claim("held")["account_id"] == aid
    store.plan(aid, "balance")
    other = store.create_account("Loomy", "other", "other")
    store.put_credential(other, json.dumps({"session": "other"}), 0)
    account = store.account

    def lock(s, account_id):
        assert account_id != aid
        return account(s, account_id)

    monkeypatch.setattr(store, "account", lock)
    assert store.claim("next")["account_id"] == other


def test_fixed_proxy_connection_failure_defers_duplicate_periodic_balance_until_retry(env):
    from app.proxy_pool import record_proxy_result

    store, supervisor, aid, route, now = env
    endpoint = "http://proxy.example.test:8888"
    node = store.resource_put("proxy", "fixed", {"status": "active"}, secret=endpoint)
    store.resource_put("maintenance_proxy_route", "Loomy:balance", {"mode": "fixed", "proxy_id": node},
                       resource_id=route, expected_version=1)
    record_proxy_result(store, aid, endpoint, success=False, code="upstream_timeout", failure_phase="connect")
    tick(supervisor)
    assert store.claim("worker") is None
    now[0] += 31
    tick(supervisor)
    assert store.claim("worker")["kind"] == "balance"
