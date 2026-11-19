"""上游「账号已被停用」（Loomy 100003）必须落账户级 disabled。

背景（2026-11-17 现网实测）：297 个 Loomy 账户停在 pending，抽 16 个直接查
`/points/balance`，15 个回 `{"code":"100003","desc":"账号已被停用"}`。旧程序
把它当「禁用 + 长冷却」，新程序没有这条判定，于是：

1. 业务码落进 unknown 的 `upstream_rejected`，账户状态一直 pending；
2. 不可重试的失败不会再排任务，账户既不会被调度、也不会再被核实；
3. 每个周期还会白撞一次上游，失败任务日志里 2 小时 302 条 balance + 288 条 checkin。

这里锁定：判定、状态落库、调度排除、余额不能顺手复活、人工 recheck 的回路。
"""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Account, Job, ModelState
from app.store import DomainError, Store
from app.upstream import UpstreamError, classify
from test_gateway import real_accounts


@pytest.fixture
def store(tmp_path):
    instance = Store(f'sqlite:///{tmp_path/"disabled.db"}', Fernet.generate_key())
    try:
        yield instance
    finally:
        instance.engine.dispose()


def _only_account_id(store):
    with store.read() as (s, _):
        return s.scalar(select(Account.id))


def _run(store, kind, ok, error=None, balance=None):
    """领到指定类型的任务再结算；同账户的其它任务用「不可重试」的理由清掉。"""
    job_id = store.plan(_only_account_id(store), kind)
    for _ in range(8):
        job = store.claim("worker", 8)
        assert job is not None
        if job["kind"] == kind:
            store.finish(job["id"], job["owner"], job["attempt"], ok, error=error, balance=balance)
            assert job["id"] == job_id
            return _only_account_id(store)
        store.finish(job["id"], job["owner"], job["attempt"], False, error="checkin_inactive")
    raise AssertionError(f"{kind} job not claimed")


def _run_balance(store, ok, error=None, balance=None):
    return _run(store, "balance", ok, error, balance)


def test_banned_business_code_is_account_level_not_generic():
    error = classify(business="100003")
    assert error.code == "account_disabled"
    # 坏的是一个号，不是这个请求：允许网关换下一个账号。
    assert error.retryable is True
    # 其它未知业务码保持原样，不能被这条判定吞掉。
    assert classify(business="999999").code == "upstream_rejected"


def test_failed_balance_marks_account_disabled_and_leaves_candidates(store):
    token = real_accounts(store, 1)
    account_id = _run_balance(store, False, error="account_disabled")

    with store.read() as (s, _):
        account = s.get(Account, account_id)
        assert account.credential_status == "disabled"
        # 人的开关不动：程序不替人决定停用。
        assert account.enabled is True

    busy, blocked = {}, {}
    with store.read() as (s, _):
        account = s.get(Account, account_id)
        assert store.reason(account, busy, blocked, "model") == "upstream_disabled"
    with pytest.raises(DomainError) as refused:
        store.acquire(token, "model")
    assert refused.value.code == "no_available_account"


def test_disabled_account_is_not_scheduled_for_checkin_or_balance(store):
    from types import SimpleNamespace

    from app.operations import DEFAULT_SETTINGS, Supervisor

    real_accounts(store, 1)
    account_id = _run_balance(store, False, error="account_disabled")
    with store.tx() as (s, _):
        s.get(Account, account_id).checkin_day = None

    assert store.schedule_today() == 0
    config = {**DEFAULT_SETTINGS, "refresh_balances": True, "daily_checkin": True}
    Supervisor(store, SimpleNamespace(tasks=[], capacity=64), SimpleNamespace(), ".").schedule(config)
    with store.read() as (s, _):
        queued = [j.kind for j in s.scalars(select(Job).where(Job.status == "queued"))]
    assert "checkin" not in queued and "balance" not in queued


def test_balance_success_does_not_revive_an_upstream_disabled_account(store):
    real_accounts(store, 1)
    account_id = _run_balance(store, False, error="account_disabled")
    # 停用的是账号而不是凭证：余额能读出来不等于可以接单。
    _run_balance(store, True, balance=999)
    with store.read() as (s, _):
        account = s.get(Account, account_id)
        assert account.credential_status == "disabled"
        assert account.balance == 999


def test_manual_recheck_returns_the_account_to_validation(store):
    real_accounts(store, 1)
    account_id = _run_balance(store, False, error="account_disabled")
    job_id = store.recheck(account_id)
    with store.read() as (s, _):
        assert s.get(Account, account_id).credential_status == "pending"
        assert s.get(Job, job_id).kind == "balance"
    _run_balance(store, True, balance=120)
    with store.read() as (s, _):
        assert s.get(Account, account_id).credential_status == "valid"
    # 只有真的停用状态才允许 recheck，正常账号不该被这条接口绕开核验。
    with pytest.raises(DomainError) as refused:
        store.recheck(account_id)
    assert refused.value.code == "account_not_upstream_disabled"


def test_chat_rejection_disables_without_a_model_block(store):
    token = real_accounts(store, 2)
    current = store.acquire(token, "model")
    store.fail_attempt(current, UpstreamError("account_disabled", 403, True))
    with store.read() as (s, _):
        assert s.get(Account, current["account_id"]).credential_status == "disabled"
        # 不是「这个模型没权限」，是整个账号停了：不建模型级限制。
        assert list(s.scalars(select(ModelState))) == []
    # 剩下的账号还能接到这个请求：坏号不该把整条链路拖死。
    later = store.acquire(token, "model")
    assert later["account_id"] != current["account_id"]


def test_pending_business_rejection_gets_a_slow_second_chance(store):
    """不可重试的业务码也要有一次长周期复核，否则账户永远停在 pending。"""
    real_accounts(store, 1)
    account_id = _run_balance(store, False, error="upstream_rejected")
    with store.read() as (s, _):
        assert s.get(Account, account_id).credential_status == "valid"

    # pending 的账户由 reconcile_maintenance 接管：刚失败时不重排。
    with store.tx() as (s, _):
        s.get(Account, account_id).credential_status = "pending"
    assert store.reconcile_maintenance() == 0

    # 六小时后允许一次复核，而不是永远不再看它。
    with store.tx() as (s, _):
        for job in s.scalars(select(Job).where(Job.kind == "balance")):
            job.finished_at = job.finished_at - 21600
    assert store.reconcile_maintenance() == 1
