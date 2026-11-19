"""Findings from the chuncheon read-only audit (docs/线上核查-chuncheon-20261117.md)."""
import json

import pytest

from app.adapters import workbuddy_identity
from app.sms_channels import extract_code
from app.upstream import classify


@pytest.mark.parametrize("text,code", [
    ("123456", "123456"),
    ("【WPS】您的验证码是 482913，5 分钟内有效。", "482913"),
    ("【腾讯云】517204为您的登录验证码，请于5分钟内填写。", "517204"),  # code before the keyword
    ("【某平台】校验码：0931", "0931"),
    ("您本次操作的动态码 77812，勿告诉他人", "77812"),
])
def test_sms_code_formats(text, code):
    assert extract_code(text) == code


@pytest.mark.parametrize("text", [
    "[尚未收到]",
    "",
    "订单 2026 金额 3500 已支付",  # two digit runs and no keyword: never guess
    "联系客服 13800000000",  # a phone number is not a code
])
def test_sms_code_never_guessed(text):
    assert extract_code(text) is None


@pytest.mark.parametrize("code,expected,retry", [
    ("12153", "invalid_credentials", True),
    ("11102", "permission", True),
    ("11115", "request_rejected", False),
    ("11101", "request_rejected", False),
    ("11128", "request_rejected", False),
    ("11148", "request_rejected", False),
])
def test_workbuddy_business_codes(code, expected, retry):
    error = classify(400, code)
    assert error.code == expected and error.retryable == retry


def call(i, name="f"):
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}


def result(i, text="ok"):
    return {"role": "tool", "tool_call_id": f"call_{i}", "content": text}


def test_workbuddy_identity_keeps_enterprises_apart_and_personal_uid_unchanged():
    assert workbuddy_identity({"uid": "1001"}) == "1001"
    assert workbuddy_identity({"uid": "1001", "enterpriseId": ""}) == "1001"
    assert workbuddy_identity({"uid": "1001", "enterpriseId": "e-7"}) == "1001:e-7"
    assert workbuddy_identity({"uid": "1001", "enterpriseId": "e-7"}) != workbuddy_identity({"uid": "1001", "enterpriseId": "e-8"})


def test_number_ledger_is_per_platform_and_includes_imported_accounts(tmp_path):
    import asyncio
    from cryptography.fernet import Fernet
    from app.models import Resource
    from app.sms_channels import ChannelInput, SmsChannels
    from app.store import DomainError, Store, uid

    store = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key())

    class Registry:
        enabled = True

    sms = SmsChannels(store, Registry())
    channel = sms.configure(ChannelInput(name="c", provider="eomsg", base_url="https://sms.example.com/api", token="t" * 20, keyword="k"))
    retired = []

    async def call(channel, action, **params):
        assert action == "getPhone"
        return "13800000001"

    async def retire(lease, reason):
        retired.append(reason)

    sms.call, sms.retire = call, retire
    # The number already belongs to an imported Loomy account.
    aid = store.create_account("Loomy", "uid-1", "imported")
    with store.tx() as (s, _):
        store.record_phone(s, "Loomy", "13800000001", "imported", aid)

    def session(platform):
        sid = uid()
        with store.tx() as (s, _):
            s.add(Resource(id=sid, kind="registration", name="r", data=json.dumps({"platform": platform, "status": "prepared"}), updated_at=store.clock()))
        return sid

    with pytest.raises(DomainError) as used:
        asyncio.run(sms.acquire(channel, session("Loomy")))
    assert used.value.code == "phone_previously_used" and retired == ["duplicate_number"]
    # The same number may still register a different platform.
    lease, phone = asyncio.run(sms.acquire(channel, session("WPS 灵犀")))
    assert phone == "13800000001" and retired == ["duplicate_number"]
    store.engine.dispose()


def test_batch_runs_to_target_successes_with_skip_and_failure_breakers(tmp_path):
    from cryptography.fernet import Fernet
    from app.models import Resource
    from app.registration import RegistrationService
    from app.registration_runner import BatchInput, RegistrationRunner
    from app.sms_channels import ChannelInput
    from app.store import Store

    now = [1794801600.0]
    store = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key(), clock=lambda: now[0])

    class Registry:
        enabled = True

    service = RegistrationService(store, Registry())
    runner = RegistrationRunner(service, None)
    channel = service.sms.configure(ChannelInput(name="c", provider="eomsg", base_url="https://sms.example.com/api", token="t" * 20, keyword="k"))

    def batch(**kw):
        return runner.make_batch(BatchInput(name="b", platform="Loomy", sms_channel_id=channel, **kw))

    def data(bid):
        with store.read() as (s, _):
            return json.loads(s.get(Resource, bid).data)

    def finish_open(bid, status, error=None):
        for sid in data(bid)["sessions"]:
            with store.tx() as (s, _):
                row = s.get(Resource, sid)
                item = json.loads(row.data)
                if item["status"] == "prepared":
                    now[0] += 1
                    item.update(status=status, error=error, finished_at=now[0])
                    row.data = json.dumps(item)

    # Target 3 successes, 2 in parallel: attempts are topped up until 3 succeed.
    b = batch(count=3, parallel=2)
    assert len(data(b)["sessions"]) == 2
    finish_open(b, "failed", "phone_previously_used")  # skips are not failures
    runner.refill_batches()
    assert len(data(b)["sessions"]) == 4 and data(b)["status"] == "active"
    finish_open(b, "succeeded")
    runner.refill_batches()
    assert len(data(b)["sessions"]) == 5  # one more needed, only one opened
    finish_open(b, "succeeded")
    runner.refill_batches()
    assert data(b)["status"] == "completed" and len(data(b)["sessions"]) == 5
    progress = {x["id"]: x for x in runner.batches()}[b]["progress"]
    assert progress["succeeded"] == 3 and progress["attempts"] == 5

    # Consecutive real failures pause the batch with a reason; nothing restarts it alone.
    b = batch(count=10, parallel=3, max_consecutive_failures=3)
    finish_open(b, "failed", "no_sms")
    runner.refill_batches()
    assert data(b)["status"] == "paused" and data(b)["pause_reason"] == "consecutive_failures"
    runner.refill_batches()
    assert len(data(b)["sessions"]) == 3

    # A dry number pool stops on consecutive used numbers.
    b = batch(count=10, parallel=5, max_consecutive_skips=5)
    finish_open(b, "failed", "phone_previously_used")
    runner.refill_batches()
    assert data(b)["pause_reason"] == "consecutive_used_numbers"

    # The number budget caps total purchases.
    b = batch(count=10, parallel=4, number_budget=4, max_consecutive_failures=50)
    finish_open(b, "failed", "verification_expired")
    runner.refill_batches()
    assert data(b)["pause_reason"] == "number_budget_exhausted" and len(data(b)["sessions"]) == 4
    store.engine.dispose()


def test_rejected_number_is_released_without_burning_a_code(tmp_path):
    """平台拒发过的号码买回来只会再被拒：取号阶段直接释放，换号再试。"""
    import asyncio
    from cryptography.fernet import Fernet
    from app.models import Resource
    from app.sms_channels import ChannelInput, SmsChannels
    from app.store import DomainError, Store, uid

    store = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key())

    class Registry:
        enabled = True

    sms = SmsChannels(store, Registry())
    sms.buy_interval = 0
    channel = sms.configure(ChannelInput(name="c", provider="eomsg", base_url="https://sms.example.com/api", token="t" * 20, keyword="k"))
    retired = []

    async def call(channel, action, **params):
        return "13800000002"

    async def retire(lease, reason):
        retired.append(reason)

    sms.call, sms.retire = call, retire

    def session(platform):
        sid = uid()
        with store.tx() as (s, _):
            s.add(Resource(id=sid, kind="registration", name="r", data=json.dumps({"platform": platform, "status": "prepared"}), updated_at=store.clock()))
        return sid

    with store.tx() as (s, _):
        store.mark_phone_rejected(s, "Loomy", "13800000002", "sms_send_rejected")

    with pytest.raises(DomainError) as refused:
        asyncio.run(sms.acquire(channel, session("Loomy")))
    assert refused.value.code == "phone_rejected_previously"
    assert retired == ["phone_rejected_previously"]
    # 黑名单按平台隔离：同一个号在别的平台没有被拒过。
    lease, phone = asyncio.run(sms.acquire(channel, session("WPS 灵犀")))
    assert phone == "13800000002"
    # 重复登记只累计次数，不新增记录。
    with store.tx() as (s, _):
        store.mark_phone_rejected(s, "Loomy", "13800000002", "sms_send_rejected")
        store.mark_phone_rejected(s, "Loomy", "not-a-phone", "sms_send_rejected")
    rows = [r for r in store.resources("phone_rejected")]
    assert len(rows) == 1 and rows[0]["data"]["count"] == 2
    store.engine.dispose()
