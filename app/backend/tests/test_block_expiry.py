"""限制到期必须自动解除，不能变成永久封锁。

背景（r18）：r15/r16 把旧网关 3066 的 `cool_kind`/`cool_until` 原样搬成了新程序的
`account_model_states`，model 一律写成 `"*"`。旧网关对额度不足/权限给的是一段
冷却时间，到点自动放回候选池；新程序却只要 `verified_at is None` 就拒派，
`review_at` 到了也没人管，于是：

- Loomy 2024 个、WPS 560 个、Marvis 5 个账户被永久摘出调度；
- 同一批号在旧网关近 6 小时还有 Loomy 3638 次、WPS 12.8 万次成功请求。

这里锁定三件事：
1. `review_at` 过了的限制不再拦派单；
2. 未到期（以及刚被真实请求拒过）的限制照旧拦；
3. 到期限制由周期任务显式标记解除，页面不会一直挂着过期封锁。
"""
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import ModelState
from app.store import Store
from test_gateway import real_accounts


@pytest.fixture
def store(tmp_path):
    s = Store(f'sqlite:///{tmp_path/"expiry.db"}', Fernet.generate_key())
    try:
        token = real_accounts(s, 1)
        yield s, token
    finally:
        s.engine.dispose()


def _only_account(store):
    return store.snapshot()["accounts"][0]


def test_expired_account_wide_block_does_not_stop_dispatch(store):
    s, token = store
    account = _only_account(s)
    s.block_model(account["id"], "*", "quota", account["credential_version"], s.clock() - 1)
    acquired = s.acquire(token, "model")
    assert acquired["account_id"] == account["id"]


def test_future_block_still_stops_dispatch(store):
    from app.store import DomainError
    s, token = store
    account = _only_account(s)
    s.block_model(account["id"], "*", "quota", account["credential_version"], s.clock() + 3600)
    with pytest.raises(DomainError) as error:
        s.acquire(token, "model")
    assert error.value.code == "no_available_account"


def test_rate_limit_expiry_keeps_working(store):
    """原来的 rate_limit/network 就到点放行，别被这次改动带坏。"""
    s, token = store
    account = _only_account(s)
    s.block_model(account["id"], "model", "rate_limit", account["credential_version"], s.clock() - 1)
    assert s.acquire(token, "model")["account_id"] == account["id"]


def test_expire_sweep_marks_blocks_verified(store):
    s, _ = store
    account = _only_account(s)
    s.block_model(account["id"], "*", "quota", account["credential_version"], s.clock() - 5)
    s.block_model(account["id"], "model", "permission", account["credential_version"], s.clock() + 3600)
    assert s.expire_model_blocks() == 1
    with s.read() as (db, _):
        rows = {m.model: m.verified_at for m in db.scalars(select(ModelState).where(ModelState.account_id == account["id"]))}
    assert rows["*"] is not None
    assert rows["model"] is None  # 未到期的原样保留
    # 没有到期的就没得清，返回 0（周期任务每轮都会调）。
    assert s.expire_model_blocks() == 0


def test_expired_block_reblocks_after_real_rejection(store):
    """到期放回候选池不等于宣布可用：真实请求再被额度拒，要重新拦住。"""
    s, token = store
    account = _only_account(s)
    s.block_model(account["id"], "*", "quota", account["credential_version"], s.clock() - 1)
    s.expire_model_blocks()
    acquired = s.acquire(token, "model")
    from app.upstream import UpstreamError
    s.fail_attempt(acquired, UpstreamError("quota", 429, retryable=True))
    with s.read() as (db, _):
        verified_at, review_at = db.execute(
            select(ModelState.verified_at, ModelState.review_at).where(
                ModelState.account_id == account["id"], ModelState.model == "model")
        ).one()
    assert verified_at is None and review_at > s.clock()
