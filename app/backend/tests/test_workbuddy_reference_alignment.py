"""Contract regressions against the user's 7863 source; no live SMS/network."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from app.models import Resource
from app.registration import RegistrationService
from app.sms_channels import ChannelInput
from app.store import DomainError, Store
from app.upstream import Transport, UpstreamError
from app.workbuddy_sms import WorkBuddySMS


def test_oneid_wire_headers_mobile_and_cli_cookie_isolation():
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/state'):
            return httpx.Response(200, json={"code": 0, "data": {"state": "synthetic", "authUrl": "https://www.codebuddy.cn/login/"}})
        if request.url.path.endswith('/send'):
            assert json.loads(request.content)["mobile"] == "+86 13800000001"
            return httpx.Response(200, json={"state_token": "synthetic", "expires_in": 60})
        return httpx.Response(200, json={"code": 0, "data": {"uid": "synthetic"}})
    async def flow():
        transport = Transport(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        adapter = WorkBuddySMS(transport)
        async def checkpoint(patch):
            pass
        adapter.checkpoint = checkpoint
        try:
            await adapter.start({"phone": "13800000001"}, "sms")
            assert adapter.material["phone"] == "+8613800000001"
            transport.client.cookies.set("synthetic", "private", domain="copilot.tencent.com")
            await adapter.verify({**adapter.material, "sms_verified": True, "broker_complete": True,
                                  "ticket_written": True, "issued_token": {"accessToken": "synthetic"}}, "sms", None)
        finally:
            await transport.close()
    asyncio.run(flow())
    assert len(seen) == 3
    for request in seen:
        assert request.headers["origin"] == "https://www.codebuddy.cn"
        assert request.headers["referer"] == "https://www.codebuddy.cn/"
    assert seen[-1].headers["user-agent"].startswith("CLI/")
    assert "x-domain" not in seen[-1].headers
    assert "cookie" not in seen[-1].headers


@pytest.fixture
def service(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'reference.db'}", Fernet.generate_key(), clock=lambda: 1794900000.)
    service = RegistrationService(store, SimpleNamespace(enabled=True))
    yield service
    store.engine.dispose()


def reserved(service, status="waiting_code"):
    channel = service.sms.configure(ChannelInput(name="synthetic", provider="haozhuma", base_url="https://sms.example.test/api", token="first", project="synthetic"))
    sid = service.create("Loomy", "synthetic", "sms", {"phone": "13800000001"})
    lease = service.sms.existing_number(channel, sid, "13800000001")
    service.sms.update(lease, {"existing_number": False}, {"channel": service.sms.read(channel)})
    with service.store.tx() as (s, _):
        row = s.get(Resource, sid)
        data = json.loads(row.data)
        data["status"] = status
        row.data = json.dumps(data)
    return channel, sid, lease


def test_waiting_response_is_not_erased_by_later_token_network_error(service):
    channel, _, lease = reserved(service)
    first = service.sms.read(channel)
    second = {**first, "token": "second"}
    service.sms.candidates = lambda *args: [first, second]
    async def call(candidate, *args, **kwargs):
        if candidate["token"] == "first":
            return None
        raise UpstreamError("network")
    service.sms.call = call
    with pytest.raises(UpstreamError):
        asyncio.run(service.sms.poll(lease))
    observation, _ = service.sms.lease(lease)
    assert observation["poll_all_failed"] is False
    assert observation["poll_error"] == "network"


def test_previously_rejected_number_can_release_before_sending_sms(service):
    _, _, lease = reserved(service, "sending")
    calls = []
    async def call(channel, action, **kwargs):
        calls.append(action)
        return {"code": 0}
    service.sms.call = call
    asyncio.run(service.sms.retire(lease, "phone_rejected_previously"))
    assert calls == ["cancelRecv"]
    assert service.sms.lease(lease)[0]["status"] == "retired"


def test_acquire_preserves_preflight_rejection_and_releases_reservation(service):
    channel = service.sms.configure(ChannelInput(name="synthetic", provider="haozhuma", base_url="https://sms.example.test/api", token="synthetic", project="synthetic"))
    sid = service.create("Loomy", "synthetic", "sms", {"sms_channel_id": channel})
    with service.store.tx() as (s, _):
        row = s.get(Resource, sid)
        data = json.loads(row.data)
        data["status"] = "sending"
        row.data = json.dumps(data)
        service.store.mark_phone_rejected(s, "Loomy", "13800000001", "sms_number_rejected")
    calls = []
    async def call(channel, action, **kwargs):
        calls.append(action)
        return {"code": 0, "phone": "13800000001"}
    service.sms.call = call
    with pytest.raises(DomainError) as error:
        asyncio.run(service.sms.acquire(channel, sid))
    assert error.value.code == "phone_rejected_previously"
    assert calls == ["getPhone", "cancelRecv"]
    leases = service.store.resources("sms_lease")
    assert len(leases) == 1 and leases[0]["data"]["status"] == "retired"


def test_preflight_label_cannot_release_an_issued_result(service):
    _, sid, lease = reserved(service, "sending")
    with service.store.tx() as (s, _):
        row = s.get(Resource, sid)
        material = json.loads(service.store.cipher.decrypt(row.secret.encode()))
        material["issued_token"] = {"accessToken": "synthetic"}
        row.secret = service.store.cipher.encrypt(json.dumps(material).encode()).decode()
    with pytest.raises(DomainError) as error:
        asyncio.run(service.sms.retire(lease, "phone_rejected_previously"))
    assert error.value.code == "registration_result_unresolved"
