"""复核退避与人工解除。

背景：原来的复核间隔是固定 `recovery_interval`（900 秒）。上游持续拒绝时，
账户每 15 分钟被拉起来探一次、必然失败、再等 15 分钟 —— 实测 WorkBuddy
国际两个账户连续 12 轮 permission 全失败，一天烧掉近 200 次必然失败的上游
请求；而「必须原模型真实回答才解除」的条件在那种情况下永远无法满足。

这里锁定两件事：
1. 失败次数越多等待越久，且有上限；
2. 人工解除能把限制放回候选池，但不留「已可用」的假证据。
"""
import asyncio

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, select

from app.models import Job, ModelState
from app.operations import DEFAULT_SETTINGS, Supervisor, review_delay, should_auto_review
from app.store import Store
from app.worker import MaintenanceWorker
from test_gateway import FakeRegistry, delta, real_accounts


class _Block:
    def __init__(self, reason="quota", attempts=0):
        self.reason, self.attempts = reason, attempts


def test_backoff_doubles_from_configured_interval():
    config = {"recovery_interval": 900}
    assert [review_delay(config, _Block(attempts=n)) for n in range(5)] == [900, 1800, 3600, 7200, 14400]


def test_backoff_caps_at_one_day():
    assert review_delay({"recovery_interval": 900}, _Block(attempts=40)) == 86400


def test_stubborn_reasons_start_at_six_hours():
    assert review_delay({"recovery_interval": 900}, _Block("permission", 0)) == 21600
    assert review_delay({"recovery_interval": 60}, _Block("permission", 0)) == 21600
    assert review_delay({"recovery_interval": 900}, _Block("identity", 3)) == 21600
    # quota 是可回血的，保持原来的起步间隔，不做无谓拉长。
    assert review_delay({"recovery_interval": 900}, _Block("quota", 0)) == 900


def test_unrecoverable_reasons_are_left_to_manual_release():
    # 未实名、无权限、限流、网络抖动：自动探测只会重复制造同一个失败。
    for reason in ("identity", "permission", "rate_limit", "network"):
        assert should_auto_review(reason) is False
    assert should_auto_review("unknown_403") is False
    assert should_auto_review("quota") is True


@pytest.fixture
def store(tmp_path):
    s = Store(f'sqlite:///{tmp_path/"backoff.db"}', Fernet.generate_key())
    try:
        real_accounts(s, 1)
        yield s
    finally:
        s.engine.dispose()


def test_permission_block_is_not_probed_by_scheduler(store, tmp_path):
    """permission 只留人工解除，调度器不再排 verify（原来每 6 小时空转一次）。"""
    from types import SimpleNamespace
    store.real_capabilities["Loomy"].add("verify")
    account = store.snapshot()["accounts"][0]
    aid, version = account["id"], account["credential_version"]
    store.block_model(aid, "model", "permission", version, store.clock() - 1)
    with store.tx() as (db, _):
        db.execute(delete(Job))
    supervisor = Supervisor(store, SimpleNamespace(tasks=[], capacity=64), SimpleNamespace(), tmp_path)
    supervisor.schedule({**DEFAULT_SETTINGS, "recovery_tests": True})
    assert store.claim("probe", 1) is None


def test_quota_block_is_still_probed_by_scheduler(store, tmp_path):
    """对照：quota 可回血，调度器仍应排 verify。"""
    from types import SimpleNamespace
    store.real_capabilities["Loomy"].add("verify")
    account = store.snapshot()["accounts"][0]
    aid, version = account["id"], account["credential_version"]
    store.block_model(aid, "model", "quota", version, store.clock() - 1)
    with store.tx() as (db, _):
        db.execute(delete(Job))
    supervisor = Supervisor(store, SimpleNamespace(tasks=[], capacity=64), SimpleNamespace(), tmp_path)
    supervisor.schedule({**DEFAULT_SETTINGS, "recovery_tests": True})
    job = store.claim("probe", 1)
    assert job["kind"] == "verify" and job["model"] == "model"


def test_failed_review_increments_attempts(store):
    store.real_capabilities["Loomy"].add("verify")
    account = store.snapshot()["accounts"][0]
    aid, version = account["id"], account["credential_version"]
    store.block_model(aid, "model", "quota", version, store.clock() + 900)
    with store.tx() as (db, _):
        db.execute(delete(Job))
    jid = store.plan(aid, "verify", "model")
    job = store.claim("test-owner", 1)
    assert job["id"] == jid
    job.pop("sandbox", None)  # 缺这个标记绝不能伪造成功
    asyncio.run(MaintenanceWorker(store, registry=FakeRegistry([[delta("answer")]])).execute(job))
    with store.read() as (db, _):
        attempts = {m.model: m.attempts for m in db.scalars(select(ModelState).where(ModelState.account_id == aid))}
    assert attempts["model"] == 1


def test_successful_review_does_not_leave_attempts_behind(store):
    store.real_capabilities["Loomy"].add("verify")
    account = store.snapshot()["accounts"][0]
    aid, version = account["id"], account["credential_version"]
    store.block_model(aid, "model", "quota", version, store.clock() + 900)
    with store.tx() as (db, _):
        db.execute(delete(Job))
    store.plan(aid, "verify", "model")
    job = store.claim("test-owner", 1)
    job.pop("sandbox", None)
    asyncio.run(MaintenanceWorker(store, registry=FakeRegistry([[delta("answer"), {"_done": True}]])).execute(job))
    with store.read() as (db, _):
        verified = db.scalar(select(ModelState.verified_at).where(ModelState.account_id == aid, ModelState.model == "model"))
    assert verified is not None


def test_manual_release_returns_block_to_pool(store):
    account = store.snapshot()["accounts"][0]
    aid, version = account["id"], account["credential_version"]
    store.block_model(aid, "*", "quota", version, store.clock() + 3600)
    assert store.release_model_block(aid) == [{"model": "*", "reason": "quota"}]
    with store.read() as (db, _):
        block = db.scalar(select(ModelState).where(ModelState.account_id == aid, ModelState.model == "*"))
        assert block.verified_at is not None and block.attempts == 0
    # 没有未解除的限制时重复解除应返回空，由路由翻译成 404。
    assert store.release_model_block(aid) == []


def test_release_only_touches_requested_model(store):
    account = store.snapshot()["accounts"][0]
    aid, version = account["id"], account["credential_version"]
    store.block_model(aid, "a", "quota", version, store.clock() + 3600)
    store.block_model(aid, "b", "permission", version, store.clock() + 3600)
    assert store.release_model_block(aid, "a") == [{"model": "a", "reason": "quota"}]
    with store.read() as (db, _):
        remaining = {m.model: m.verified_at for m in db.scalars(select(ModelState).where(ModelState.account_id == aid))}
    assert remaining["a"] is not None
    assert remaining["b"] is None


def test_unclassified_403_expires_without_probe_and_directory_matches_gateway(store):
    from app.account_directory import directory_query
    from app.models import Account
    from app.store import DomainError
    from app.upstream import UpstreamError

    start = store.clock()
    now = [start]
    store.clock = lambda: now[0]
    token = store.create_key("temporary-refusal-test", "Loomy", "model")["token"]
    first = store.acquire(token, "model")
    store.fail_attempt(first, UpstreamError("unknown_403", 403, True))
    store.end_request(first["id"], "failed", "unknown_403")

    def directory_status():
        with store.read() as (session, _):
            row = session.execute(directory_query(store, session, store.clock()).where(Account.id == first["account_id"])).one()
            return row._mapping["directory_status"]

    assert directory_status() == "unknown_403"
    with pytest.raises(DomainError, match="no_available_account"):
        store.acquire(token, "model")
    now[0] = start + 901
    assert directory_status() == "ready"
    current = store.acquire(token, "model")
    assert current["account_id"] == first["account_id"]
    with store.read() as (session, _):
        assert session.get(Account, first["account_id"]).credential_status == "valid"
        assert session.scalar(select(Job.id).where(Job.kind.in_(["verify", "relogin"]))) is None
