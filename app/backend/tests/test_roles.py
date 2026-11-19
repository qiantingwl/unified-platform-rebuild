"""Four-container split: the api role forwards on its own; the task service only publishes status."""
import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store
from conftest import seed_sandbox


@pytest.fixture
def store(tmp_path):
    now = [1794801600.0]
    s = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key(), clock=lambda: now[0])
    s.now = now
    yield s
    s.engine.dispose()


def test_task_service_exposes_only_health(store):
    app = create_app(store, start_worker=False, role="tasks", admin_token="")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["role"] == "tasks"
        assert client.get("/api/snapshot").status_code == 404
        assert client.post("/v1/chat/completions", json={}).status_code == 404
        assert client.get("/").status_code == 404


def test_api_forwards_without_task_service_and_reports_it_offline(store):
    seed_sandbox(store)
    key = store.create_key("t", "Loomy", "sandbox-echo")["token"]
    app = create_app(store, start_worker=False, role="api")
    headers = {"X-Console-Client": "unified-local"}
    with TestClient(app) as client:
        runtime = client.get("/api/snapshot").json()["runtime"]
        assert runtime["role"] == "api" and runtime["task_service"]["online"] is False and runtime["worker_running"] is False
        # Request-time account selection lives in the api role: no task service needed.
        r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={"model": "sandbox-echo", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"].strip()
        # The task service publishes its status through the database.
        store.service_heartbeat("tasks", {"worker": {"running": True, "last_tick": store.clock(), "last_error": None}, "supervisor": {"running": True, "last_tick": store.clock()}, "browser_executor": {"status": "ok", "sessions": 0, "max_sessions": 8}})
        runtime = client.get("/api/snapshot").json()["runtime"]
        assert runtime["task_service"]["online"] is True and runtime["worker_running"] is True
        assert runtime["browser_executor"]["status"] == "ok"
        monitor = client.get("/api/monitor").json()
        assert monitor["scope"] == "task_service_process" and monitor["supervisor_running"] is True
        # A silent task service is shown as offline instead of keeping the last good state.
        store.now[0] += 120
        runtime = client.get("/api/snapshot").json()["runtime"]
        assert runtime["task_service"]["online"] is False and runtime["worker_running"] is False


def test_heartbeat_does_not_bump_console_revision(store):
    before = store.revision()
    store.service_heartbeat("tasks", {"worker": {"running": True}})
    store.service_heartbeat("tasks", {"worker": {"running": True}})
    assert store.revision() == before
    assert json.dumps(store.service_status("tasks"))


def test_prune_bounds_event_and_affinity_growth_but_keeps_history(store):
    from sqlalchemy import func, select
    from app.models import Affinity, Event, Job
    seed_sandbox(store)
    with store.tx() as (s, _):
        s.add(Affinity(id="old", account_id="x", expires_at=store.clock() - 2 * 86400))
        s.add(Affinity(id="live", account_id="x", expires_at=store.clock() + 32 * 86400))
    before = store.revision()
    store.now[0] += 31 * 86400
    store.create_account("Loomy", "fresh", "fresh")
    result = store.prune()
    assert result["events"] >= 28 and result["affinity"] >= 1
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Event)) == 1
        assert {a.id for a in s.scalars(select(Affinity))} == {"live"}
    assert store.revision() > before
