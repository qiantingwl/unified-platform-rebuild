import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.adapters import WPS, CodeArts, Raccoon, WorkBuddy, Marvis, Registry
from app.models import Account, Credential, Resource, Job, Lease, ModelState
from app.registration import RegistrationService
from app.store import Store, DomainError, day_at
from app.upstream import Transport, UpstreamError, classify
from app.worker import MaintenanceWorker


@pytest.fixture
def store(tmp_path):
    now = [1794801600.]
    s = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key(), clock=lambda: now[0])
    s.now = now
    yield s
    s.engine.dispose()


def test_wps_checkin_is_readback_not_generic_200():
    async def run():
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.method == "POST":
                assert request.url.params["date"] == "2026-11-16"
                return httpx.Response(200, json={"result":"ok", "data":{}})
            return httpx.Response(200, json={"data":{"tasks":[{"task_key":"daily_check_in","status":"claimed" if len(calls)>2 else "locked"}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WPS(Transport(client), {"cookie":"wps_sid=synthetic"}, "a")
            assert await adapter.checkin("2026-11-16") == {"day":"2026-11-16"}
            assert len(calls) == 3 and not any("balance" in p for p in calls)
    asyncio.run(run())


def test_codearts_undated_old_claim_is_not_today():
    async def run():
        adapter = CodeArts(None,{},"a")
        async def read(path):
            return {"items":[{"campaignId":"x","type":"USER_LOGIN","benefitUnit":"CREDIT","claimable":False,"status":"CONFIRMED"}]}
        async def ledger(key,value=None):
            return None
        adapter.read, adapter.ledger = read, ledger
        with pytest.raises(UpstreamError,match="unconfirmed_checkin"):
            await adapter.checkin("2026-11-16")
    asyncio.run(run())


def test_codearts_claim_persists_idempotency_before_sending():
    async def run():
        adapter = CodeArts(None,{},"a")
        saved, sent = {}, []
        async def ledger(key,value=None):
            if value is not None:
                saved[key] = dict(value)
            return saved.get(key)
        async def read(path):
            return {"items":[{"campaignId":"x","type":"USER_LOGIN","benefitUnit":"CREDIT","claimable":not sent,"status":"CONFIRMED" if sent else "AVAILABLE"}]}
        async def post(path, data):
            if path.endswith("claim"):
                assert saved["campaign:2026-11-16:x"]["idempotentKey"] == data["idempotentKey"]
            sent.append(path)
            return {"campaignId":"x"}
        adapter.read,adapter.post,adapter.ledger = read,post,ledger
        assert await adapter.checkin("2026-11-16") == {"day":"2026-11-16"}
        assert saved["checkin:2026-11-16"] == {"day":"2026-11-16"}
    asyncio.run(run())


def test_raccoon_today_evidence_and_iam_no_legacy_refresh():
    async def run():
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json={"code":0,"data":{"items":[{"biz_type":"daily_grant","points":100,"grant_at":"2026-11-16T08:00:00+08:00","created_at":"2026-11-16T08:00:00+08:00"}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=Raccoon(Transport(client),{"token":"synthetic"},"a")
            assert await adapter.checkin("2026-11-16") == {"day":"2026-11-16"}
            assert len(requests)==1 and requests[0].url.path.endswith("bills")
            import base64
            claim=base64.urlsafe_b64encode(json.dumps({"iss":"site/iam"}).encode()).decode().rstrip("=")
            adapter.c={"token":f"x.{claim}.x","refresh_token":"synthetic"}
            with pytest.raises(UpstreamError,match="iam_refresh_material_required"):
                await adapter.renew()
            assert len(requests)==1
    asyncio.run(run())


def test_wps_session_cache_is_account_version_model_conversation_scoped():
    async def run():
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={"data":{"session_id":"s","run_id":"r"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache={}
            adapter=WPS(Transport(client),{"cookie":"wps_sid=synthetic"},"a",1);adapter.cache=cache
            body={"model":"deepseek","messages":[{"role":"user","content":"test"}]}
            first=await adapter.prepare(body,"c")
            created=next(iter(cache.values()))["created"]
            assert first[1]["x-cc-run-id"]=="r"
            await adapter.prepare(body,"c")
            assert len(calls)==2 and next(iter(cache.values()))["created"]==created
            await adapter.prepare(body,"different")
            assert len(calls)==4
            adapter.version=2
            await adapter.prepare(body,"c")
            assert len(calls)==6
            with pytest.raises(UpstreamError,match="unsupported_image_input"):
                await adapter.prepare({"model":"m","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://example.com/p.png"}}]}]},"c")
    asyncio.run(run())


class FakeTransport:
    async def close(self):
        pass


class FakeRegistry:
    enabled=True
    transport_factory=staticmethod(lambda **kwargs:FakeTransport())


class Login:
    def __init__(self, t):
        pass
    async def start(self, material, mode):
        return {"challenge":"synthetic-challenge"}
    async def verify(self, material, mode, code):
        assert material["challenge"]=="synthetic-challenge"
        return {"identity":"identity-1", "credential":{"session":"synthetic-token"}}


def test_registration_atomic_import_and_credential_privacy(store):
    store.real_capabilities = {"Loomy": {"checkin", "balance"}}
    service=RegistrationService(store,FakeRegistry(),Login)
    sid=service.create("Loomy","new","sms",{"phone":"synthetic-number"})
    assert asyncio.run(service.run(sid))["status"]=="waiting_code"
    with pytest.raises(DomainError):
        asyncio.run(service.run(sid))
    result=asyncio.run(service.run(sid,"synthetic-code"))
    assert result["status"]=="succeeded"
    data=store.snapshot()
    assert len(data["accounts"])==1 and data["jobs"][0]["kind"]=="checkin"
    assert "synthetic-token" not in json.dumps(data) and "synthetic-number" not in json.dumps(store.resources("registration"))
    assert all(a["enabled"] for a in data["accounts"])


def test_registration_daily_limit_and_three_no_sms_failures(store):
    aid=store.create_account("Loomy","identity-1","account")
    service=RegistrationService(store,FakeRegistry(),Login)
    for index in range(3):
        sid=service.create("Loomy","try","sms",{"phone":"synthetic-number"},aid)
        asyncio.run(service.run(sid))
        # A second relogin is refused at creation while the first is unresolved.
        with pytest.raises(DomainError):
            service.create("Loomy","same day","sms",{"phone":"synthetic-number"},aid)
        store.now[0]+=301
        service.expire_sms(sid)
        with pytest.raises(DomainError):
            service.expire_sms(sid)
        assert store.snapshot()["accounts"][0]["relogin_failures"]==index+1
        store.now[0]+=86400
    sid=service.create("Loomy","fourth","sms",{"phone":"synthetic-number"},aid)
    with pytest.raises(DomainError) as failure:
        asyncio.run(service.run(sid))
    assert failure.value.code=="relogin_attempts_exhausted"


def test_registration_ambiguous_network_does_not_count_failure(store):
    aid=store.create_account("Loomy","identity-1","account")
    class Lost(Login):
        async def start(self,material,mode):
            raise UpstreamError("network",502,True)
    service=RegistrationService(store,FakeRegistry(),Lost)
    sid=service.create("Loomy","try","sms",{"phone":"synthetic-number"},aid)
    assert asyncio.run(service.run(sid))["status"]=="uncertain"
    assert store.snapshot()["accounts"][0]["relogin_failures"]==0


def test_permission_recovery_requires_answer_and_native_slot(store):
    aid=store.create_account("Loomy","i","account")
    store.real_capabilities={"Loomy":{"chat","balance","verify"}}
    store.put_credential(aid,json.dumps({"session":"synthetic"}),0)
    balance=store.claim("worker")
    store.finish(balance["id"],balance["owner"],balance["attempt"],True,balance=100)
    store.block_model(aid,"m","permission",1,store.clock()-1)
    jobid=store.plan(aid,"verify","m")
    job=store.claim("worker")
    assert job["id"]==jobid and job["model"]=="m"
    with store.tx() as (s,_):
        assert len(list(s.scalars(select(Lease))))==1
    class Adapter:
        t=FakeTransport()
        async def chat(self,body,session):
            yield {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}
            yield {"_done":True}
    class Registry(FakeRegistry):
        def make(self,*args):
            return Adapter()
    asyncio.run(MaintenanceWorker(store,registry=Registry()).execute_real(job))
    with store.tx() as (s,_):
        assert s.scalar(select(ModelState)).verified_at is not None
        assert list(s.scalars(select(Lease)))==[]


def test_classification_never_treats_empty_403_as_ban():
    assert classify(403).code=="unknown_403"
    assert classify(200,40331).code=="identity"
    assert classify(402).code=="quota"


def test_only_consecutive_no_sms_stops_relogin_other_errors_recorded_separately(store):
    aid=store.create_account("Loomy","identity-1","account")
    class Rejected(Login):
        async def start(self,material,mode):
            raise UpstreamError("login_rejected",401)
    no_sms=RegistrationService(store,FakeRegistry(),Login)
    rejected=RegistrationService(store,FakeRegistry(),Rejected)
    def miss():
        sid=no_sms.create("Loomy","try","sms",{"phone":"synthetic-number"},aid)
        asyncio.run(no_sms.run(sid))
        store.now[0]+=301
        no_sms.expire_sms(sid)
        store.now[0]+=86400
    miss(); miss()
    assert store.snapshot()["accounts"][0]["relogin_failures"]==2
    sid=rejected.create("Loomy","rejected","sms",{"phone":"synthetic-number"},aid)
    result=asyncio.run(rejected.run(sid))
    assert result["status"]=="failed"
    data,_=rejected.details(sid)
    assert data["error"]=="login_rejected"
    # A rejected login is recorded under its own code and is not counted as no_sms.
    assert store.snapshot()["accounts"][0]["relogin_failures"]==2
    store.now[0]+=86400
    miss()
    sid=no_sms.create("Loomy","fourth","sms",{"phone":"synthetic-number"},aid)
    with pytest.raises(DomainError) as failure:
        asyncio.run(no_sms.run(sid))
    assert failure.value.code=="relogin_attempts_exhausted"


def test_raccoon_balance_preserves_imported_organization_and_sends_one_bearer_prefix():
    async def run():
        def handler(request):
            assert request.url.path == "/api/web/points/v1/balance"
            assert request.headers["Authorization"] == "Bearer fixture"
            assert request.headers["X-Org-Code"] == "org-fixture"
            return httpx.Response(200, json={"code": 0, "data": {"available_points": 42}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Raccoon(Transport(client), {"accessToken": "Bearer fixture", "office_identity": "org-fixture"}, "a")
            assert await adapter.balance() == 42
            adapter.c["officeIdentity"] = "personal"
            assert "X-Org-Code" not in adapter.headers()

    asyncio.run(run())
