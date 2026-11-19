import asyncio
import json

import pytest
import httpx
from cryptography.fernet import Fernet
from sqlalchemy import delete, select

from app.invitations import Invitations
from app.models import Job, Resource
from app.operations import DEFAULT_SETTINGS
from app.store import DomainError, Store, uid
from app.upstream import UpstreamError, Transport
from app.adapters import Loomy
from app.worker import MaintenanceWorker
from test_gateway import real_accounts


class Registry:
    enabled = True

    def __init__(self):
        self.active = {}
        self.submits = []
        self.fail_read = False
        self.fail_after_submit = False

    def make(self, platform, credential, account, version, proxy):
        registry = self

        class Adapter:
            def __init__(self):
                self.t = self

            async def close(self):
                pass

            async def invite_read(self):
                if registry.fail_read:
                    raise UpstreamError("network")
                return {"activated": account in registry.active, "appliedInvitationCode": registry.active.get(account)}

            async def invite_submit(self, code):
                registry.submits.append((account, code))
                registry.active[account] = code
                if registry.fail_after_submit:
                    registry.fail_read = True
                    raise UpstreamError("network")

            async def balance(self):
                return 200

            async def invite_codes(self):
                return {"list": [{"inviteCode": "synthetic-code", "maxUses": 3, "usedCount": 0, "status": "active"}]}

        return Adapter()


@pytest.fixture
def context(tmp_path):
    now = [1794801600.0]
    store = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key(), clock=lambda: now[0])
    real_accounts(store, 2)
    store.real_capabilities["Loomy"].add("invitation")
    with store.tx() as (s, _):
        s.execute(delete(Job))
    registry = Registry()
    service = Invitations(store, registry)
    worker = MaintenanceWorker(store, registry=registry)
    worker.invitations = service
    accounts = [a["id"] for a in store.snapshot()["accounts"]]
    yield store, service, worker, registry, accounts, now
    store.engine.dispose()


def run_job(store, worker):
    job = store.claim("w", 8)
    assert job
    asyncio.run(worker.execute(job))
    return job


def test_shared_queue_dedup_capacity_and_account_mutex(context):
    store, service, worker, registry, accounts, now = context
    first = service.queue(accounts[0])
    assert service.queue(accounts[0])["job_id"] == first["job_id"]
    job = store.claim("w", 1)
    service.queue(accounts[1])
    assert store.claim("w2", 1) is None
    balance = store.plan(accounts[0], "balance")
    other = store.claim("w2", 2)
    assert other["account_id"] == accounts[1]  # same-account balance cannot overlap
    assert store.claim("w3", 8) is None
    assert job["kind"] == other["kind"] == "invitation"


def test_bind_confirmation_followup_once_and_no_regression(context):
    store, service, worker, registry, accounts, now = context
    service.queue(accounts[0], "synthetic-code")
    run_job(store, worker)
    record = service.records()[0]["data"]
    assert record["status"] == "bound" and record["balance_followup_id"]
    run_job(store, worker)  # balance followup
    registry.fail_read = True
    service.queue(accounts[0])
    run_job(store, worker)
    record2 = service.records()[0]["data"]
    assert record2["status"] == "bound" and record2["binding_confirmed"]
    assert record2["balance_followup_id"] == record["balance_followup_id"]
    assert len(registry.submits) == 1


def test_lost_submit_response_recovers_by_read_only(context):
    store, service, worker, registry, accounts, now = context
    registry.fail_after_submit = True
    service.queue(accounts[0], "synthetic-code")
    run_job(store, worker)
    assert service.records()[0]["data"]["status"] == "uncertain"
    now[0] += 121
    registry.fail_read = False
    service.schedule()  # automatic binding is off, recovery read still runs
    run_job(store, worker)
    assert service.records()[0]["data"]["status"] == "bound"
    assert len(registry.submits) == 1


def test_stale_credential_or_expired_job_cannot_write_receipt(context):
    store, service, worker, registry, accounts, now = context
    service.queue(accounts[0])
    job = store.claim("w", 8)
    rid, token, *_ = service.begin(accounts[0], None, job)
    store.put_credential(accounts[0], json.dumps({"session": "new-synthetic"}), 1)
    with pytest.raises(DomainError, match="stale_invitation_receipt"):
        service.update(rid, token, {"activated": True, "binding_confirmed": True, "status": "bound"})


def test_automatic_selection_and_no_rebinding_after_negative_read(context):
    store, service, worker, registry, accounts, now = context
    service.queue(accounts[0], auto=True)
    run_job(store, worker)
    assert registry.submits == [(accounts[0], "synthetic-code")]
    run_job(store, worker)
    registry.active.clear()  # inconsistent later platform response cannot erase confirmation
    service.queue(accounts[0], auto=True)
    run_job(store, worker)
    assert len(registry.submits) == 1
    assert service.records()[0]["data"]["status"] == "bound"


def test_automatic_queue_defaults_off(context):
    store, service, worker, registry, accounts, now = context
    service.schedule()
    assert store.claim("w") is None
    with store.tx() as (s, _):
        s.add(Resource(id=uid(), kind="settings", name="runtime", data=json.dumps({**DEFAULT_SETTINGS, "auto_invitation": True}), updated_at=now[0]))
    service.schedule()
    assert store.claim("w")["kind"] == "invitation"


def test_code_is_encrypted_and_queue_does_not_call_platform(context):
    store, service, worker, registry, accounts, now = context
    result = service.queue(accounts[0], "private-invite-code")
    assert result["status"] == "queued" and not registry.submits
    with store.read() as (s, _):
        target = s.scalar(select(Resource).where(Resource.kind == "job_target"))
        assert "private-invite-code" not in target.data + target.secret


def test_invitation_code_endpoint_matches_live_contract():
    def handle(request):
        assert str(request.url) == "https://loomyad.xunfei.cn/api/v1/invitation-codes"
        return httpx.Response(200, json={"code": "000000", "data": {"list": []}})
    async def run():
        transport = Transport(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        try:
            assert await Loomy(transport, {"session": "synthetic"}, "a").invite_codes() == {"list": []}
        finally:
            await transport.close()
    asyncio.run(run())
