"""Recovery of admitted maintenance work without enabling new SMS/model probes."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, event

from app.models import Account, Job, ModelState, Resource, RequestAttempt, RequestLog
from app.operations import DEFAULT_SETTINGS, Supervisor
from app.store import Store
from app.store import DomainError
from app.worker import MaintenanceWorker


@pytest.fixture
def store(tmp_path):
    now = [1794801600.0]
    result = Store(f"sqlite:///{tmp_path / 'tasks.db'}", Fernet.generate_key(), clock=lambda: now[0])
    result.now = now
    result.real_capabilities["Loomy"] = {"balance", "checkin", "chat"}
    yield result
    result.engine.dispose()


def account(store):
    aid = store.create_account("Loomy", "synthetic", "synthetic")
    store.put_credential(aid, json.dumps({"session": "synthetic"}), 0)
    return aid


def finish(store, job, success, **kwargs):
    store.finish(job["id"], job["owner"], job["attempt"], success, **kwargs)


def tick(store):
    Supervisor(store, SimpleNamespace(capacity=8), SimpleNamespace(), ".").schedule(DEFAULT_SETTINGS)


def test_failed_initial_checkin_still_validates_credential_without_periodic_balance(store):
    aid = account(store)
    checkin = store.claim("worker")
    finish(store, checkin, False, error="network")
    tick(store)
    validation = store.claim("worker")
    assert validation and validation["kind"] == "balance"
    finish(store, validation, True, balance=42)
    with store.read() as (db, _):
        assert db.get(Account, aid).credential_status == "valid"
        assert db.get(Job, checkin["id"]).status == "failed"


def test_auth_review_network_failure_recovers_with_backoff_and_without_duplicate(store):
    aid = account(store)
    finish(store, store.claim("worker"), True)
    finish(store, store.claim("worker"), True, balance=42)
    with store.tx() as (db, state):
        store.observe_auth_failure(db, state, db.get(Account, aid))
    review = store.claim("worker")
    finish(store, review, False, error="network")
    tick(store)
    assert store.claim("worker") is None
    store.now[0] += 301
    tick(store)
    tick(store)
    retried = store.claim("worker")
    assert retried and retried["id"] == review["id"]
    finish(store, retried, True, balance=42)
    with store.read() as (db, _):
        assert db.get(Account, aid).credential_status == "valid"


def test_later_401_reopens_completed_auth_review_same_credential(store):
    aid = account(store)
    finish(store, store.claim("worker"), True)
    finish(store, store.claim("worker"), True, balance=42)
    for _ in range(2):
        with store.tx() as (db, state):
            store.observe_auth_failure(db, state, db.get(Account, aid))
        review = store.claim("worker")
        assert review and review["kind"] == "balance"
        finish(store, review, True, balance=42)


def test_lost_checkin_can_validate_balance_but_never_repeats_claim(store):
    aid = account(store)
    checkin = store.claim("lost", lease_seconds=1)
    store.now[0] += 2
    assert store.claim("other") is None
    tick(store)
    validation = store.claim("other")
    assert validation and validation["kind"] == "balance"
    finish(store, validation, True, balance=42)
    with store.read() as (db, _):
        assert db.get(Job, checkin["id"]).status == "uncertain"
        assert db.get(Account, aid).checkin_day is None


def test_permanent_validation_failure_is_not_repeated_automatically(store):
    """不可重试的业务拒绝：不做分钟级空转，但也不能永远不再看一眼。

    以前这里是「一次失败就彻底放弃」，结果现网 297 个 Loomy 账户停在 pending ——
    没有任务、没有结论、人工也看不到该做什么。现在的口径：按 6 小时长周期最多
    复核三次，期间不做任何自动重试。
    """
    account(store)
    finish(store, store.claim("worker"), False, error="network")
    tick(store)
    validation = store.claim("worker")
    assert validation
    finish(store, validation, False, error="permission")

    # 六小时内不重排：不做分钟级空转。
    store.now[0] += 3600
    tick(store)
    assert store.claim("worker") is None

    # 六小时后允许一次，总共复核三次（原任务 + 两次）之后彻底停手。
    for _ in range(2):
        store.now[0] += 21600
        tick(store)
        again = store.claim("worker")
        assert again and again["id"] == validation["id"]
        finish(store, again, False, error="permission")
    store.now[0] += 86400
    tick(store)
    assert store.claim("worker") is None


def test_shared_session_blocks_automatic_and_manual_renew(store):
    aid = account(store)
    store.real_capabilities["Loomy"].add("renew")
    store.put_credential(aid, json.dumps({"expires_at": store.clock() - 1, "session": "synthetic"}), 1)
    store.resource_put("migration_source", aid, {"credential_owner": "legacy"})
    supervisor = Supervisor(store, SimpleNamespace(capacity=8), SimpleNamespace(), ".")
    supervisor.schedule({**DEFAULT_SETTINGS, "auto_renew": True})
    with store.read() as (db, _):
        assert not db.scalar(select(Job.id).where(Job.kind == "renew"))
    with pytest.raises(DomainError) as error:
        store.plan(aid, "renew")
    assert error.value.code == 'independent_session_required'


def test_wildcard_block_never_probes_a_fabricated_model(store):
    """通配符限制没有可探测的模型：不排 verify，到期直接解除。

    r18 之前这条是「永远不探测也永远不解」，2024 个 Loomy 账户因此被永久摘出
    调度（同期旧网关对同一批号还有 3638 次成功）。现在改成到点交回候选池。
    """
    aid = account(store)
    finish(store, store.claim("worker"), True)
    finish(store, store.claim("worker"), True, balance=42)
    store.real_capabilities["Loomy"].add("verify")
    store.block_model(aid, "*", "quota", 1, store.clock() - 1)
    Supervisor(store, SimpleNamespace(capacity=8), SimpleNamespace(), ".").schedule({**DEFAULT_SETTINGS, "recovery_tests": True})
    assert store.claim("worker") is None
    with store.read() as (db, _):
        block = db.scalar(select(ModelState))
        assert block.verified_at is not None and block.model == "*"


def test_failed_verify_only_changes_its_own_target_backoff(store):
    aid = account(store)
    finish(store, store.claim("worker"), True)
    finish(store, store.claim("worker"), True, balance=42)
    store.real_capabilities["Loomy"].add("verify")
    store.block_model(aid, "one", "quota", 1, store.clock() + 900)
    store.block_model(aid, "two", "permission", 1, store.clock() + 900)
    store.plan(aid, "verify", "one")
    finish(store, store.claim("worker"), False, error="network")
    with store.read() as (db, _):
        assert {b.model: b.attempts for b in db.scalars(select(ModelState))} == {"one": 1, "two": 0}


@pytest.mark.parametrize("model,reason", [("one", "permission"), ("*", "identity"), ("*", "quota")])
def test_old_probe_cannot_clear_a_newer_rejection(store, model, reason):
    aid = account(store)
    finish(store, store.claim("worker"), True)
    finish(store, store.claim("worker"), True, balance=42)
    store.real_capabilities["Loomy"].add("verify")
    store.block_model(aid, model, reason, 1, store.clock() + 900)
    store.plan(aid, "verify", "one")
    probe = store.claim("worker")
    store.now[0] += 1
    with store.tx() as (db, _):
        db.add(RequestLog(id="real-request", account_id=aid, platform="Loomy", model="one", started_at=store.clock(), ended_at=store.clock(), status="failed"))
        db.add(RequestAttempt(id="real-attempt", request_id="real-request", account_id=aid, credential_version=1, started_at=store.clock(), ended_at=store.clock(), code=reason))
    finish(store, probe, True, effective_answer=True)
    with store.read() as (db, _):
        assert db.scalar(select(ModelState)).verified_at is None


def test_lost_worker_heartbeat_cancels_upstream_and_leaves_receipt_uncertain(store, monkeypatch):
    account(store)
    job = store.claim("worker", lease_seconds=1)
    assert job["kind"] == "checkin"
    started = []
    cancelled = []
    closed = []

    class Adapter:
        t = None

        async def checkin(self, day):
            started.append(True)
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

        async def close(self):
            closed.append(True)

    adapter = Adapter()
    adapter.t = adapter
    registry = SimpleNamespace(make=lambda *args: adapter)
    worker = MaintenanceWorker(store, registry=registry)
    worker.HEARTBEAT_SECONDS = 0.01

    def lost(_):
        # 只有上游调用真的开始了才丢租约。这里要验证的是「在途上游调用被取消」；
        # 如果抢在凭据查询阶段就取消，测试就退化成比数据库快慢 —— PostgreSQL 在
        # 整套测试并行时正是这种情况（本地 sqlite 快、单跑也快，所以一直没暴露）。
        if not started:
            return
        raise DomainError("stale_job_receipt")

    monkeypatch.setattr(store, "heartbeat_job", lost)
    monkeypatch.setattr(store, "account_proxy", lambda _: None)
    asyncio.run(worker.execute(job))
    assert cancelled == [True] and closed == [True]
    assert worker.last_error == "maintenance_lease_lost"
    store.now[0] += 2
    assert store.claim("new-worker") is None
    with store.read() as (db, _):
        assert db.get(Job, job["id"]).status == "uncertain"


def test_periodic_balance_cannot_bypass_pending_validation_backoff(store):
    aid = account(store)
    finish(store, store.claim("worker"), False, error="network")
    tick(store)
    validation = store.claim("worker")
    finish(store, validation, False, error="network")
    for delay in (301, 601, 1201):
        store.now[0] += delay
        tick(store)
        retried = store.claim("worker")
        assert retried and retried["id"] == validation["id"]
        finish(store, retried, False, error="network")
    # Four failures now require 40 minutes. The original job is older than
    # the periodic 30-minute interval, but that must not create a fresh query.
    store.now[0] += 60
    Supervisor(store, SimpleNamespace(capacity=8), SimpleNamespace(), ".").schedule({**DEFAULT_SETTINGS, "refresh_balances": True})
    assert store.claim("worker") is None
    with store.read() as (db, _):
        assert len(list(db.scalars(select(Job).where(Job.account_id == aid, Job.kind == "balance")))) == 1


def test_migration_signin_followup_precedes_remaining_signins(store):
    account(store)
    second = store.create_account("Loomy", "synthetic-two", "synthetic-two")
    store.put_credential(second, json.dumps({"session": "synthetic-two"}), 0)
    first = store.claim("worker")
    finish(store, first, True)
    next_job = store.claim("worker")
    assert next_job["kind"] == "balance" and next_job["account_id"] == first["account_id"]
    with store.read() as (db, _):
        assert db.get(Job, next_job["id"]).priority == 5
        assert any(j.kind == "checkin" and j.status == "queued" for j in db.scalars(select(Job)))
