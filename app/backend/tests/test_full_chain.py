"""End-to-end offline chain on one database, with a scripted platform instead of the network:

registration -> account + credential stored -> sign-in first -> balance right after ->
account selection -> forwarding -> classified failures -> relogin -> recovery.

Every step goes through the same store, workers and gateway the services use;
only the platform adapter and login connector are replaced by scripts.
"""
import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.gateway import Gateway
from app.models import Account, Credential, Job, Lease, ModelState
from app.registration import RegistrationService
from app.store import DomainError, Store, day_at
from app.upstream import UpstreamError
from app.worker import MaintenanceWorker


class Transport:
    async def close(self):
        pass


class Platform:
    """Scripted platform state shared by login, maintenance and chat."""

    def __init__(self):
        self.balance = 120.0
        self.checkins = []
        self.balance_error = None
        self.chat_script = []
        self.issued = 0
        self.credentials_seen = []


class Registry:
    enabled = True

    def __init__(self, platform):
        self.platform = platform
        self.transport_factory = staticmethod(lambda **kwargs: Transport())

    def make(self, name, credential, account_id, version, proxy):
        p = self.platform
        p.credentials_seen.append((version, credential))

        class Adapter:
            t = Transport()

            async def checkin(self, day):
                p.checkins.append(day)
                return {"day": day}

            async def checkin_receipt(self, day):
                return {"day": day} if day in p.checkins else {}

            async def balance(self):
                if p.balance_error:
                    raise p.balance_error
                return p.balance

            async def chat(self, body, session):
                for item in p.chat_script.pop(0):
                    if isinstance(item, Exception):
                        raise item
                    yield item

        return Adapter()


def login_factory(platform):
    class Login:
        def __init__(self, t):
            pass

        async def start(self, material, mode):
            return {"challenge": "c"}

        async def verify(self, material, mode, code):
            platform.issued += 1
            return {"identity": "uid-1001", "credential": {"session": f"token-{platform.issued}", "phone": material.get("phone")}}

    return Login


@pytest.fixture
def env(tmp_path):
    now = [1794801600.0]
    store = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key(), clock=lambda: now[0])
    store.now = now
    store.real_capabilities = {"Loomy": {"chat", "balance", "checkin", "verify"}}
    platform = Platform()
    registry = Registry(platform)
    service = RegistrationService(store, registry, login_factory(platform))
    worker = MaintenanceWorker(store, registry=registry, capacity=8)
    yield store, platform, registry, service, worker
    store.engine.dispose()


def drain(store, worker):
    """Run every claimable job exactly as the task service does."""
    ran = []
    while job := store.claim(worker.owner, 8):
        ran.append(job["kind"])
        asyncio.run(worker.execute(job))
    return ran


def account(store):
    return store.snapshot()["accounts"][0]


def forward(store, registry, token, script, model="model"):
    registry.platform.chat_script.append(script)
    current = store.acquire(token, model, conversation="c")

    async def run():
        return [x async for x in Gateway(store, registry).frames(token, {"model": model, "messages": [{"role": "user", "content": "q"}]}, current, "c")]

    return asyncio.run(run())


def test_full_chain_registration_to_recovery(env):
    store, platform, registry, service, worker = env

    # 1. Registration: claim -> run -> report; identity, phone and credential stored once.
    sid = service.create("Loomy", "new", "sms", {"phone": "13800000000"})
    assert asyncio.run(service.run(sid))["status"] == "waiting_code"
    assert asyncio.run(service.run(sid, "123456"))["status"] == "succeeded"
    with pytest.raises(DomainError):
        asyncio.run(service.run(sid, "123456"))  # a repeated report does not create anything
    with store.tx() as (s, _):
        accounts = list(s.scalars(select(Account)))
        assert len(accounts) == 1 and accounts[0].external_id == "uid-1001" and accounts[0].enabled
        saved = s.scalar(select(Credential).where(Credential.account_id == accounts[0].id))
        stored = json.loads(store.cipher.decrypt(saved.ciphertext.encode()))
        assert stored["session"] == "token-1" and stored["phone"] == "13800000000"
    assert "13800000000" not in json.dumps(store.snapshot())

    # 2. Sign-in first, balance right after, no second sign-in today.
    assert drain(store, worker) == ["checkin", "balance"]
    a = account(store)
    assert a["checkin_day"] == day_at(store.clock()) and a["balance"] == 120 and a["credential_status"] == "valid"
    assert store.schedule_today() == 0 and drain(store, worker) == []
    assert platform.checkins == [day_at(store.clock())]

    # A failed balance query keeps the last value and is reported as a failure, never as 0.
    platform.balance_error = UpstreamError("network", 502, True)
    store.plan(a["id"], "balance")
    assert drain(store, worker) == ["balance"]
    a = account(store)
    assert a["balance"] == 120 and a["balance_error"] == "network"
    platform.balance_error = None

    # 3. Forwarding: only an eligible account is selected; slot is released after.
    token = store.create_key("k", "Loomy", "model")["token"]
    frames = forward(store, registry, token, [{"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}, {"_done": True}])
    assert frames[0].get("placeholder") and frames[-1] == {"finish": "stop"}
    with store.tx() as (s, _):
        assert list(s.scalars(select(Lease))) == []
    assert store.snapshot()["requests"][0]["status"] == "succeeded"

    # 4. Classified failures: rate limit pauses only that model until its review time.
    frames = forward(store, registry, token, [UpstreamError("rate_limit", 429, False, retry_after=60)])
    assert frames[-1]["error"]["code"] == "rate_limit"
    assert account(store)["status"] == "rate_limit"
    with pytest.raises(DomainError) as busy:
        store.acquire(token, "model")
    assert busy.value.code == "no_available_account"
    store.now[0] += 61
    assert account(store)["status"] == "ready"

    # Permission: only an effective answer clears it, a successful balance query does not.
    frames = forward(store, registry, token, [UpstreamError("permission", 403, False)])
    assert account(store)["status"] == "permission"
    store.plan(a["id"], "balance")
    drain(store, worker)
    assert account(store)["status"] == "permission"
    store.plan(a["id"], "verify", "model")
    platform.chat_script.append([{"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}])
    assert drain(store, worker) == ["verify"]
    assert account(store)["status"] == "ready"

    # 5. Invalid credential -> relogin -> new version active, old failure state cleared.
    frames = forward(store, registry, token, [UpstreamError("invalid_credentials", 401, False)])
    a = account(store)
    # One 401 only pauses the account and queues a review; no SMS is spent yet.
    assert a["status"] == "credential_review" and a["enabled"]
    platform.balance_error = UpstreamError("invalid_credentials", 401, False)
    assert drain(store, worker) == ["balance"]
    platform.balance_error = None
    a = account(store)
    assert a["status"] == "needs_relogin" and a["enabled"]
    store.now[0] += 86400  # next business day
    sid = service.create("Loomy", "relogin", "sms", {"phone": "13800000000"}, a["id"])
    assert asyncio.run(service.run(sid))["status"] == "waiting_code"
    assert asyncio.run(service.run(sid, "654321"))["status"] == "succeeded"
    a = account(store)
    assert a["credential_version"] == 2 and a["relogin_failures"] == 0
    assert drain(store, worker) == ["checkin", "balance"]
    a = account(store)
    assert a["status"] == "ready" and a["credential_status"] == "valid"
    assert platform.credentials_seen[-1][1]["session"] == "token-2"

    # 6. Recovery: forwarding works again on the new credential.
    frames = forward(store, registry, token, [{"choices": [{"delta": {"content": "back"}, "finish_reason": "stop"}]}])
    assert frames[-1] == {"finish": "stop"}
    with store.tx() as (s, _):
        assert {j.status for j in s.scalars(select(Job))} <= {"succeeded", "failed", "superseded"}
        assert not list(s.scalars(select(ModelState).where(ModelState.verified_at.is_(None), ModelState.credential_version == 2)))


def test_task_service_restart_does_not_repeat_checkin_or_lose_job(env):
    store, platform, registry, service, worker = env
    sid = service.create("Loomy", "new", "sms", {"phone": "13800000000"})
    asyncio.run(service.run(sid))
    asyncio.run(service.run(sid, "123456"))
    # The task container dies after claiming the sign-in, before reporting.
    job = store.claim("old-container", 8)
    assert job["kind"] == "checkin"
    store.now[0] += 120  # lease expires
    replacement = MaintenanceWorker(store, registry=registry, capacity=8)
    ran = drain(store, replacement)
    # The uncertain sign-in is not blindly sent again.
    assert platform.checkins == []
    with store.tx() as (s, _):
        state = s.get(Job, job["id"])
        assert state.status in {"uncertain", "queued", "failed"}
    snapshot = store.snapshot()
    assert snapshot["job_counts"].get("uncertain", 0) + snapshot["job_counts"].get("queued", 0) >= 1 or "checkin" in ran
    # A late report from the dead container is rejected.
    with pytest.raises(DomainError) as stale:
        store.finish(job["id"], job["owner"], job["attempt"], True)
    assert stale.value.code == "stale_job_receipt"
