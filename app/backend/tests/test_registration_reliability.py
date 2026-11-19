"""Regression cases from the r14 registration/scheduling handoff audit."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.models import Account, Resource
from app.registration import LoomyLogin, RegistrationService
from app.registration_runner import BatchInput, RegistrationRunner
from app.sms_channels import ChannelInput, SmsChannels
from app.store import DomainError, Store
from app.upstream import UpstreamError
from app.native_lifecycle import record_native_login


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'registration.db'}", Fernet.generate_key(), clock=lambda: 1794900000.)
    class Transport:
        async def close(self):
            pass
    registry = SimpleNamespace(enabled=True, transport_factory=lambda **kwargs: Transport())
    service = RegistrationService(store, registry)
    yield store, service
    store.engine.dispose()


def test_resumed_code_persists_submission_before_ambiguous_response(env):
    store, service = env
    called = []
    class Lost:
        def __init__(self, transport):
            pass
        async def verify(self, material, mode, code):
            called.append(code)
            assert service.details(sid)[0]["code_submitted"]
            raise UpstreamError("network", 502, True)
    service.factory = Lost
    sid = service.create("Loomy", "synthetic", "sms", {"phone": "13800000001"})
    with store.tx() as (s, _):
        row = s.get(Resource, sid)
        data = json.loads(row.data)
        data.update(status="uncertain", expires_at=store.clock()+300)
        row.data = json.dumps(data)
    assert asyncio.run(service.run(sid, code="123456"))["status"] == "uncertain"
    with pytest.raises(DomainError) as error:
        asyncio.run(service.run(sid, code="123456"))
    assert error.value.code == "registration_state_conflict"
    assert called == ["123456"]


@pytest.mark.parametrize("reason,permanent", [
    ("sms_send_rejected", False), ("sms_rejected", False),
    ("sms_number_rejected", True), ("number_not_supported", True),
])
def test_only_number_specific_refusal_is_recorded_as_permanent(env, reason, permanent):
    store, service = env
    class Refused:
        def __init__(self, transport):
            pass
        async def start(self, material, mode):
            raise UpstreamError(reason, 400)
    service.factory = Refused
    sid = service.create("Loomy", "synthetic", "sms", {"phone": "13800000001"})
    assert asyncio.run(service.run(sid))["error"] == reason
    assert bool(store.resources("phone_rejected")) is permanent


def test_buy_slots_are_atomic_across_instances_and_restart(env):
    store, service = env
    other = SmsChannels(store, service.registry)
    with ThreadPoolExecutor(max_workers=8) as pool:
        waits = list(pool.map(lambda i: (service.sms if i % 2 else other).reserve_buy("shared"), range(8)))
    assert sorted(waits) == [float(i*2) for i in range(8)]
    assert SmsChannels(store, service.registry).reserve_buy("shared") == 16
    assert other.reserve_buy("independent") == 0


def test_relogin_scans_are_bounded_and_resume_after_skipped_accounts(env):
    store, service = env
    with store.tx() as (s, _):
        for i in range(130):
            account=Account(id=f"a{i:04}", platform="Loomy", external_id=f"synthetic-{i}", label="synthetic", sandbox=False,
                          credential_version=1,credential_status="invalid", created_at=store.clock(), updated_at=store.clock())
            s.add(account)
            record_native_login(store,s,account,'password',{'session':f'official-session-{i}'})
    seen = []
    service.recovery.plan = lambda aid: seen.append(aid) or {"action": "material"}
    RegistrationRunner(service, None).schedule_relogin(1)
    assert len(seen) == 64
    RegistrationRunner(service, None).schedule_relogin(1)
    assert len(seen) == 128 and len(set(seen)) == 128
    RegistrationRunner(service, None).schedule_relogin(1)
    assert set(seen) == {f"a{i:04}" for i in range(130)}


def test_unverified_import_skips_automatic_but_allows_explicit_prepare(env):
    store, service = env
    aid = store.create_account("Loomy", "synthetic", "synthetic")
    store.put_credential(aid, json.dumps({"session": "synthetic", "phone": "13800000001", "password": "synthetic"}), 0)
    with store.tx() as (s, _):
        s.get(Account, aid).credential_status = "invalid"
    plan = service.recovery.plan(aid)
    assert plan["lifecycle"]["state"] == "verification_required" and not plan["automatic_login_allowed"]
    RegistrationRunner(service, None).schedule_relogin()
    assert store.resources("registration") == []
    result = service.recovery.prepare(aid)
    assert result["session_id"]
    assert service.details(result["session_id"])[0]["status"] == "prepared"


def test_loomy_batch_uses_registration_mode(env):
    store, service = env
    channel = service.sms.configure(ChannelInput(name="synthetic", provider="eomsg", base_url="https://sms.example.test/api", token="synthetic", keyword="synthetic"))
    RegistrationRunner(service, None).make_batch(BatchInput(name="batch", platform="Loomy", count=2, sms_channel_id=channel))
    sessions = store.resources("registration")
    assert len(sessions) == 2 and all(r["data"]["mode"] == "register" for r in sessions)


def test_balance_priority_keeps_unknown_tokens_as_fallback(env):
    store, service = env
    channel = service.sms.configure(ChannelInput(name="synthetic", provider="eomsg", base_url="https://sms.example.test/api", tokens=["known", "unknown", "empty"], keyword="synthetic"))
    service.sms.token_balances = lambda *_: {"known": 3., "empty": 0.}
    variants = service.sms.candidates(channel, service.sms.read(channel), rotate=True)
    assert [item["token"] for item in variants] == ["known", "empty", "unknown"]


@pytest.mark.parametrize("first_error", [None, "loomy_not_registered", "rate_limit"])
def test_loomy_registers_only_after_explicit_not_registered(first_error):
    adapter = LoomyLogin(None)
    calls = []
    material = {"phone": "13800000001", "password": "synthetic-new-password", "device": "synthetic", "challenge": "synthetic"}
    async def call(path, params, device):
        calls.append(path)
        if len(calls) == 1 and first_error:
            raise UpstreamError(first_error, 400)
        return {"userid": "synthetic", "session": "synthetic-session"}
    adapter.call_explained = call
    if first_error == "rate_limit":
        with pytest.raises(UpstreamError) as error:
            asyncio.run(adapter.verify(material, "register", "123456"))
        assert error.value.code == "rate_limit" and len(calls) == 1
    else:
        result = asyncio.run(adapter.verify(material, "register", "123456"))
        assert len(calls) == (2 if first_error else 1)
        assert ("password" in result["credential"]) is bool(first_error)
        if first_error:
            assert calls[1] == "/register/phone/submit"
