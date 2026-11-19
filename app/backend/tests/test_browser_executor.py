"""Browser executor split: the main program drives an isolated executor over HTTP.

The executor here is an in-memory fake behind httpx.MockTransport, so these
tests exercise the main program's task ownership, persistence and recovery
logic without Chromium and without any real network.
"""
import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.browser_login import BrowserHub, ExecutorClient
from app.models import Resource
from app.registration import RegistrationService
from app.store import DomainError, Store, uid


class Registry:
    enabled = True


class FakeExecutor:
    """Mimics backend/browser_executor/main.py's HTTP contract."""

    def __init__(self):
        self.instance = "instance-1"
        self.sessions = {}
        self.opened = []
        self.deleted = []

    def restart(self):
        self.instance = "instance-2"
        self.sessions.clear()

    def handler(self, request):
        assert request.headers["authorization"] == "Bearer executor-token-123456"
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == "/health":
            return httpx.Response(200, json={"instance": self.instance, "sessions": len(self.sessions)})
        if path == "/sessions":
            return httpx.Response(200, json={"instance": self.instance, "sessions": [{"sid": k, **v} for k, v in self.sessions.items()]})
        parts = path.split("/")
        sid = parts[2]
        tail = parts[3] if len(parts) > 3 else ""
        if method == "POST" and not tail:
            self.opened.append(body)
            self.sessions.setdefault(sid, {"url": body["url"], "closed": False, "captured": [], "state": body.get("storage_state") or {"cookies": [], "origins": []}})
            return httpx.Response(200, json={"instance": self.instance, "sid": sid, **self.view(sid)})
        if sid not in self.sessions:
            return httpx.Response(404, json={"error": {"code": "browser_session_missing"}})
        if method == "DELETE":
            self.deleted.append(sid)
            self.sessions.pop(sid)
            return httpx.Response(200, json={"ok": True})
        if method == "GET" and not tail:
            return httpx.Response(200, json={"instance": self.instance, "sid": sid, **self.view(sid)})
        if tail == "storage-state":
            return httpx.Response(200, json=self.sessions[sid]["state"])
        if tail == "action":
            return httpx.Response(200, json={"ok": True})
        if tail == "goto":
            self.sessions[sid]["url"] = body["url"]
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(path)

    def view(self, sid):
        item = self.sessions[sid]
        return {"url": item["url"], "closed": item["closed"], "captured": list(item["captured"])}


@pytest.fixture
def setup(tmp_path):
    now = [1794801600.0]
    store = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key(), clock=lambda: now[0])
    fake = FakeExecutor()
    client = ExecutorClient("http://browser:4190", "executor-token-123456", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), headers={"Authorization": "Bearer executor-token-123456"}))
    service = RegistrationService(store, Registry())
    hub = BrowserHub(store, Registry(), service, client)
    sid = uid()
    with store.tx() as (s, state):
        material = {"authorization_url": "https://login.example.com/authorize?state=x", "callback_url": "http://127.0.0.1:4180/oauth/callback"}
        s.add(Resource(id=sid, kind="registration", name="t", data=json.dumps({"platform": "CodeArts", "mode": "authorize", "account_id": None, "status": "waiting_authorization", "expires_at": now[0] + 600, "created_at": now[0]}),
                       secret=store.cipher.encrypt(json.dumps(material).encode()).decode(), updated_at=now[0]))
    yield store, fake, hub, service, sid
    store.engine.dispose()


def test_executor_restart_reopens_from_saved_state_and_never_reconsumes_callback(setup):
    store, fake, hub, service, sid = setup
    runs = []

    async def run(session_id, code=None, continuation=False):
        runs.append(code)
        return {"status": "waiting_authorization"}

    service.run = run

    async def flow():
        await hub.context(sid)
        assert fake.opened[0]["url"].startswith("https://login.example.com/")
        fake.sessions[sid]["state"] = {"cookies": [{"name": "s", "value": "1"}], "origins": []}
        await hub.checkpoint(sid)
        # The executor captures an OAuth redirect; the watcher persists it, then consumes it once.
        fake.sessions[sid]["captured"].append("http://127.0.0.1:4180/oauth/callback?code=abc&state=x")
        await hub.watch_one(sid)
        _, material = service.details(sid)
        assert material["captured_callback"].endswith("code=abc&state=x")
        assert runs == ["http://127.0.0.1:4180/oauth/callback?code=abc&state=x"]
        await hub.watch_one(sid)
        assert len(runs) == 1, "same evidence must not be exchanged twice"
        # Executor crash: the main program notices the new instance and reopens from saved state.
        fake.restart()
        await hub.watch_one(sid)
        assert sid not in hub.contexts
        await hub.context(sid)
        assert fake.opened[-1]["storage_state"] == {"cookies": [{"name": "s", "value": "1"}], "origins": []}
        assert hub.contexts[sid] == "instance-2"
        assert len(runs) == 1

    asyncio.run(flow())


def test_main_restart_adopts_live_sessions_and_closes_orphans(setup):
    store, fake, hub, service, sid = setup
    fake.sessions[sid] = {"url": "https://login.example.com/", "closed": False, "captured": [], "state": {}}
    fake.sessions["unknown-session"] = {"url": "https://x.example.com/", "closed": False, "captured": [], "state": {}}

    asyncio.run(hub.adopt())
    assert hub.contexts == {sid: "instance-1"}
    assert fake.deleted == ["unknown-session"]


def test_unconfigured_or_unreachable_executor_is_an_explicit_error(setup):
    store, fake, hub, service, sid = setup
    hub.executor = ExecutorClient("", "")
    with pytest.raises(DomainError) as error:
        asyncio.run(hub.context(sid))
    assert error.value.code == "browser_runtime_unavailable"

    def down(_):
        raise httpx.ConnectError("refused")

    hub.executor = ExecutorClient("http://browser:4190", "executor-token-123456", httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(DomainError) as error:
        asyncio.run(hub.context(sid))
    assert error.value.code == "browser_executor_unreachable"
    assert hub.executor.health["status"] == "unreachable"


def test_executor_service_rejects_missing_token_and_unauthenticated_calls():
    from browser_executor.main import create_app

    with pytest.raises(RuntimeError):
        create_app(token="short")
    app = create_app(token="executor-token-123456")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers={"Authorization": "Bearer wrong-token-0000000"}).status_code == 401
        ok = client.get("/health", headers={"Authorization": "Bearer executor-token-123456"})
        assert ok.status_code == 200 and ok.json()["sessions"] == 0
        missing = client.get("/sessions/none", headers={"Authorization": "Bearer executor-token-123456"})
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "browser_session_missing"
        bad = client.post("/sessions/x", headers={"Authorization": "Bearer executor-token-123456"}, json={"url": "http://insecure.example.com"})
        # Rejected before any browser is launched.
        assert bad.status_code == 422
