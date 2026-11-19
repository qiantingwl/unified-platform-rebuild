"""In-process management-route contract tests; no worker, network or real credentials."""
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Account, Job
from app.store import Store, DomainError
from app.upstream import UpstreamError


class OfflineRegistry:
    enabled = True  # only this injected fake may satisfy the invitation gate

    def __init__(self):
        self.fail_models = False
        self.closed = 0
        self.made = []

    def make(self, platform, credential, identity, version, proxy):
        self.made.append((platform, identity, version))
        registry = self

        class Adapter:
            def __init__(self):
                self.t = self

            async def close(self):
                registry.closed += 1

            async def models(self):
                if registry.fail_models:
                    raise UpstreamError("network", 503)
                return [{"id": "fake-model", "context_window": 2048}]

        return Adapter()


@pytest.fixture
def context(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'isolated.db'}", Fernet.generate_key())
    registry = OfflineRegistry()
    # Injected registry has no transport, no HTTP client, and no background worker.
    app = create_app(store=store, registry=registry, start_worker=False,
                     admin_token="isolated-only-admin-token", role="api")
    with TestClient(app, headers={"X-Console-Client": "unified-local"}) as client:
        login = client.post("/api/session", json={"token": "isolated-only-admin-token"})
        assert login.status_code == 200
        yield store, registry, client
    store.engine.dispose()


def account(store, external_id):
    aid = store.create_account("Loomy", external_id, "Synthetic Loomy")
    store.put_credential(aid, json.dumps({"session": "synthetic-not-real"}), 0)
    return aid


def test_invitation_read_bind_routes_queue_and_dedupe_without_worker(context):
    store, registry, client = context
    aid = account(store, "fake-invite")
    read = client.post(f"/api/accounts/{aid}/invitation/read")
    assert read.status_code == 200, read.text
    job_id = read.json()["job_id"]
    with store.read() as (session, _):
        job = session.get(Job, job_id)
        assert job.kind == "invitation" and job.status == "queued"
    again = client.post(f"/api/accounts/{aid}/invitation/read")
    assert again.status_code == 200 and again.json()["job_id"] == job_id
    bind_busy = client.post(f"/api/accounts/{aid}/invitation/bind", json={"code": "fake-code"})
    assert bind_busy.status_code >= 400 and bind_busy.json()["error"]["code"] == "invitation_busy"
    assert registry.made == []  # queue does not execute adapter or reach upstream


def test_invitation_bind_route_persists_target_and_rejects_bad_identity(context):
    store, registry, client = context
    aid = account(store, "fake-bind")
    response = client.post(f"/api/accounts/{aid}/invitation/bind", json={"code": "fake-code"})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    with store.read() as (session, _):
        job = session.get(Job, job_id)
        assert job.kind == "invitation" and job.status == "queued"
    assert client.post(f"/api/accounts/{aid}/invitation/bind", json={"code": "again"}).json()["error"]["code"] == "invitation_busy"
    missing = client.post("/api/accounts/missing-id/invitation/read")
    assert missing.status_code >= 400
    other = store.create_account("WorkBuddy 国内", "fake-wrong-platform", "Synthetic other")
    store.put_credential(other, json.dumps({"session": "synthetic-not-real"}), 0)
    wrong = client.post(f"/api/accounts/{other}/invitation/read")
    assert wrong.status_code >= 400 and wrong.json()["error"]["code"] == "adapter_unavailable"
    invalid = client.post(f"/api/accounts/{aid}/invitation/bind", json={"code": "contains spaces"})
    assert invalid.status_code >= 400 and invalid.json()["error"]["code"] == "invalid_input"
    assert registry.made == []


def test_model_sync_fake_catalog_persists_and_failure_preserves_prior(context):
    store, registry, client = context
    aid = account(store, "fake-catalog")
    response = client.post(f"/api/accounts/{aid}/models/sync")
    assert response.status_code == 200, response.text
    rows = store.resources("model_catalog")
    assert len(rows) == 1 and rows[0]["name"] == "Loomy"
    assert rows[0]["data"]["models"] == ["fake-model"]
    assert rows[0]["data"]["source_account_id"] == aid
    old = rows[0]["data"].copy()
    registry.fail_models = True
    failed = client.post(f"/api/accounts/{aid}/models/sync")
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "network"
    assert store.resources("model_catalog")[0]["data"] == old
    assert registry.closed == 2


def test_catalog_account_picker_returns_only_enabled_valid_platform_accounts(context):
    store, registry, client = context
    account_ids = {name: account(store, f"picker-{name}") for name in ("valid", "disabled", "pending")}
    foreign = store.create_account("WPS 灵犀", "picker-foreign", "Synthetic WPS")
    with store.tx() as (s, _):
        for aid in (account_ids['valid'], account_ids['disabled'], foreign):
            s.get(Account, aid).credential_status = "valid"
        s.get(Account, account_ids['disabled']).enabled = False

    response = client.get('/api/accounts/picker', params={'platform': 'Loomy', 'capability': 'models'})
    assert response.status_code == 200
    assert response.json()['total'] == 1
    item, = response.json()['items']
    assert item['id'] == account_ids['valid'] and item['platform'] == 'Loomy'
    assert not {'credential', 'external_id', 'balance', 'credential_status'}.intersection(item)
    assert client.get('/api/accounts/picker', params={'platform': 'Loomy', 'capability': 'models', 'q': 'picker-disabled'}).json()['total'] == 0
    assert client.get('/api/accounts/picker', params={'platform': 'Loomy'}).json()['total'] == 3
    assert registry.made == []
