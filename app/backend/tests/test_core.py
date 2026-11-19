import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import seed_sandbox
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Account, Credential, Job, ModelState
from app.store import DomainError, Store, day_at


@pytest.fixture
def store(tmp_path):
    now = [1794801600.0]
    key = Fernet.generate_key()
    s = Store(f"sqlite:///{tmp_path / 'test.db'}", key, clock=lambda: now[0])
    s.test_key = key
    s.test_clock = now
    yield s
    s.engine.dispose()


def seeded(store):
    seed_sandbox(store)
    return store.snapshot()["accounts"]


def drain(store):
    while j := store.claim("test", capacity=64):
        store.finish(j["id"], j["owner"], j["attempt"], True, balance=3000 if j["kind"] == "balance" else None, effective_answer=j["kind"] == "verify")


def test_totals_and_reasons_share_snapshot(store):
    seeded(store)
    d = store.snapshot()
    assert d["totals"] == {"total": 28, "ready": 7, "busy": 0, "unavailable": 21}
    assert sum(d["reasons"].values()) == 28
    assert all(a["enabled"] for a in d["accounts"])


def test_account_identity_dedupes_atomically(store):
    def create(_):
        return store.create_account("Loomy", "same-id", "same")
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(16)))
    assert len(set(ids)) == 1
    assert store.snapshot()["totals"]["total"] == 1


def test_priority_shared_capacity_and_per_account_serialization(store):
    rows = seeded(store)
    ready = [a for a in rows if a["status"] == "ready"]
    store.plan(ready[0]["id"], "balance")
    store.plan(ready[0]["id"], "checkin")
    store.plan(ready[1]["id"], "checkin")
    first = store.claim("worker-1", capacity=2)
    second = store.claim("worker-2", capacity=2)
    assert first["kind"] == second["kind"] == "checkin"
    assert first["account_id"] != second["account_id"]
    assert store.claim("worker-3", capacity=2) is None


def test_checkin_atomic_followup_and_no_duplicate_after_restart(store):
    a = seeded(store)[0]
    job_id = store.plan(a["id"], "checkin")
    assert store.plan(a["id"], "checkin") == job_id
    j = store.claim("worker")
    store.finish(j["id"], "worker", j["attempt"], True)
    with pytest.raises(DomainError, match="already_checked_in"):
        store.plan(a["id"], "checkin")
    with pytest.raises(DomainError, match="stale_job_receipt"):
        store.finish(j["id"], "worker", j["attempt"], True)
    jobs = store.snapshot()["jobs"]
    assert len([x for x in jobs if x["kind"] == "balance"]) == 1
    restarted = Store(store.engine.url.render_as_string(hide_password=False), store.test_key, clock=store.clock)
    try:
        with pytest.raises(DomainError, match="already_checked_in"):
            restarted.plan(a["id"], "checkin")
        assert len(restarted.snapshot()["jobs"]) == 2
    finally:
        restarted.engine.dispose()


def test_balance_failure_does_not_zero_balance_or_repeat_checkin(store):
    a = seeded(store)[0]
    store.plan(a["id"], "checkin")
    j = store.claim("worker")
    store.finish(j["id"], "worker", j["attempt"], True)
    j = store.claim("worker")
    store.finish(j["id"], "worker", j["attempt"], False, error="network")
    assert next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])["balance"] == 2000
    # The post-sign-in balance is requeued on its own five minutes later; no manual retry.
    failed = next(x for x in store.snapshot()["jobs"] if x["id"] == j["id"])
    assert failed["status"] == "queued" and failed["available_at"] >= store.clock() + 300
    store.test_clock[0] += 301
    drain(store)
    jobs = store.snapshot()["jobs"]
    assert len([j for j in jobs if j["kind"] == "checkin"]) == 1
    assert next(j for j in jobs if j["kind"] == "balance")["attempt"] == 2


def test_stale_credential_error_cannot_overwrite_new_version(store):
    a = seeded(store)[0]
    store.plan(a["id"], "balance")
    j = store.claim("worker")
    assert store.put_credential(a["id"], "new-sensitive-test-only", 1) == 2
    store.finish(j["id"], "worker", j["attempt"], False, error="invalid_credentials")
    account = next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])
    assert account["credential_status"] == "pending"
    assert next(x for x in store.snapshot()["jobs"] if x["id"] == j["id"])["status"] == "superseded"
    with store.tx() as (s, _):
        ciphertext = s.scalar(select(Credential.ciphertext).where(Credential.account_id == a["id"], Credential.version == 2))
        assert "new-sensitive-test-only" not in ciphertext
        assert store.cipher.decrypt(ciphertext.encode()).decode() == "new-sensitive-test-only"
    assert "new-sensitive-test-only" not in json.dumps(store.snapshot())
    with pytest.raises(DomainError, match="credential_version_conflict"):
        store.put_credential(a["id"], "old", 1)


def test_lease_loss_does_not_repeat_checkin_or_count_login_failure(store):
    a = seeded(store)[0]
    store.plan(a["id"], "checkin")
    j = store.claim("dead-worker", lease_seconds=1)
    store.test_clock[0] += 2
    assert store.claim("new-worker") is None
    assert next(x for x in store.snapshot()["jobs"] if x["id"] == j["id"])["status"] == "uncertain"
    with pytest.raises(DomainError, match="stale_job_receipt"):
        store.finish(j["id"], "dead-worker", j["attempt"], True)
    assert next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])["relogin_failures"] == 0


def test_safe_query_recovery_fences_old_attempt(store):
    a = seeded(store)[0]
    store.plan(a["id"], "balance")
    first = store.claim("same-worker", lease_seconds=1)
    store.test_clock[0] += 2
    next_attempt = store.claim("same-worker")
    assert next_attempt["id"] == first["id"]
    assert next_attempt["attempt"] == 2
    with pytest.raises(DomainError, match="stale_job_receipt"):
        store.finish(first["id"], "same-worker", 1, True, balance=1)
    store.finish(next_attempt["id"], "same-worker", 2, True, balance=500)


def test_daily_relogin_three_failures_persist_across_days(store):
    a = seeded(store)[0]
    for i in range(3):
        store.plan(a["id"], "relogin")
        with pytest.raises(DomainError, match="relogin_already_scheduled_today"):
            store.plan(a["id"], "relogin")
        j = store.claim("worker")
        store.finish(j["id"], "worker", j["attempt"], False, error="no_sms")
        store.test_clock[0] += 86400
    with pytest.raises(DomainError, match="relogin_attempts_exhausted"):
        store.plan(a["id"], "relogin")
    assert next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])["relogin_failures"] == 3


def test_missing_material_and_worker_failure_do_not_count_as_account_failures(store):
    a = seeded(store)[0]
    store.plan(a["id"], "relogin")
    j = store.claim("worker")
    store.finish(j["id"], "worker", j["attempt"], False, error="missing_material")
    assert next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])["relogin_failures"] == 0


def test_uncertain_login_blocks_next_day_replay(store):
    a = seeded(store)[0]
    store.plan(a["id"], "relogin")
    store.claim("dead-worker", lease_seconds=1)
    store.test_clock[0] += 86400
    store.claim("new-worker")
    with pytest.raises(DomainError, match="relogin_result_unresolved"):
        store.plan(a["id"], "relogin")


def test_permission_requires_effective_answer_not_time_or_balance(store):
    rows = seeded(store)
    a = next(x for x in rows if x["status"] == "permission")
    store.test_clock[0] += 120
    store.plan(a["id"], "balance")
    drain(store)
    assert next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])["status"] == "permission"
    store.plan(a["id"], "verify")
    j = store.claim("worker")
    store.finish(j["id"], "worker", j["attempt"], True, effective_answer=False)
    assert next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])["status"] == "permission"
    store.retry(j["id"])
    drain(store)
    assert next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])["status"] == "ready"


def test_atomic_business_concurrency_across_connections(store):
    seeded(store)
    key = store.create_key("test", "Loomy", "sandbox-echo")["token"]
    def acquire(_):
        try:
            return store.acquire(key, "sandbox-echo")
        except DomainError as e:
            return e.code
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(acquire, range(24)))
    acquired = [r for r in results if isinstance(r, dict)]
    assert len(acquired) == 1
    assert all(r == "no_available_account" for r in results if isinstance(r, str))
    r = acquired[0]
    store.end_request(r["id"], "cancelled", "cancelled")
    assert store.acquire(key, "sandbox-echo")["platform"] == "Loomy"


def test_platform_binding_and_key_revocation(store):
    rows = seeded(store)
    for a in rows:
        if a["platform"] == "Loomy":
            store.block_model(a["id"], "sandbox-echo", "permission", 1, store.clock())
    key = store.create_key("Loomy only", "Loomy", "sandbox-echo")
    with pytest.raises(DomainError, match="no_available_account"):
        store.acquire(key["token"], "sandbox-echo")
    with pytest.raises(DomainError, match="model_not_found"):
        store.acquire(key["token"], "some-other-model")
    store.revoke_key(key["id"])
    with pytest.raises(DomainError, match="invalid_api_key"):
        store.acquire(key["token"], "sandbox-echo")


def test_placeholder_does_not_make_empty_request_success(store):
    seeded(store)
    key = store.create_key("test", "Loomy", "sandbox-echo")["token"]
    r = store.acquire(key, "sandbox-echo")
    store.mark_output(r["id"], placeholder=True)
    store.end_request(r["id"], "succeeded")
    row = store.snapshot()["requests"][0]
    assert row["status"] == "failed"
    assert row["error_code"] == "no_effective_output"
    assert row["placeholder_at"] is not None and row["first_content_at"] is None
    assert store.snapshot()["totals"]["busy"] == 0


def test_expired_business_lease_does_not_leave_success_record(store):
    seeded(store)
    key = store.create_key("test", "Loomy", "sandbox-echo")["token"]
    first = store.acquire(key, "sandbox-echo")
    store.test_clock[0] += 181
    second = store.acquire(key, "sandbox-echo")
    old = next(r for r in store.snapshot()["requests"] if r["id"] == first["id"])
    assert old["status"] == "failed" and second["id"] != first["id"]


def test_api_streaming_snapshot_privacy_and_validation(store):
    app = create_app(store, start_worker=False)
    with TestClient(app) as client:
        headers = {"X-Console-Client": "unified-local"}
        seed_sandbox(store)
        key = client.post('/api/keys', headers=headers, json={"name":"test", "platform":"Loomy", "model":"sandbox-echo"}).json()["token"]
        response = client.post('/v1/chat/completions', headers={"Authorization":f"Bearer {key}"}, json={"model":"sandbox-echo", "stream":True, "messages":[{"role":"user", "content":"private-test-prompt"}]})
        assert response.status_code == 200
        chunks = [json.loads(x[6:]) for x in response.text.split('\n') if x.startswith('data: {')]
        assert chunks[0]["choices"][0]["delta"]["content"] == " "
        assert '[DONE]' in response.text
        d = client.get('/api/snapshot').json()
        assert d["requests"] == [] and d["accounts"] == []
        requests = client.get("/api/requests",params={"start":store.clock()-60,"end":store.clock()+1}).json()["rows"]
        assert requests[0]["status"] == "succeeded"
        assert requests[0]["first_content_at"] is not None
        assert key not in json.dumps(d) and 'private-test-prompt' not in json.dumps(d)
        account = client.get("/api/accounts").json()["items"][0]
        bad = client.put(f'/api/accounts/{account["id"]}/credential',headers=headers,json={"credential":"super-secret", "expected_version":-1})
        assert bad.status_code == 422 and 'super-secret' not in bad.text
        assert client.post('/api/maintenance/plan-today',json={}).status_code == 403
        assert client.post('/api/keys', headers={**headers,"Origin":"https://malicious.example"},json={"name":"evil","platform":"Loomy","model":"m"}).status_code == 403
        assert client.post('/v1/messages').status_code == 401


def test_admin_auth_and_unsupported_adapter_are_explicit(store):
    app = create_app(store, start_worker=False, admin_token="local-test-admin")
    with TestClient(app) as client:
        headers = {"X-Console-Client":"unified-local"}
        assert client.get('/api/snapshot').status_code == 401
        assert client.post('/api/session',headers=headers,json={"token":"bad"}).status_code == 401
        assert client.post('/api/session',headers=headers,json={"token":"local-test-admin"}).status_code == 200
        created = client.post('/api/accounts',headers=headers,json={"platform":"WPS 灵犀","external_id":"example-id","label":"test"}).json()
        result = client.post(f'/api/accounts/{created["id"]}/jobs',headers=headers,json={"kind":"checkin"})
        assert result.status_code == 501 and result.json()["error"]["code"] == "adapter_unavailable"


def test_business_day_is_beijing_not_machine_timezone():
    from datetime import datetime, timezone
    t = datetime(2026,11,16,16,0,tzinfo=timezone.utc).timestamp()
    assert day_at(t) == "2026-11-17"


def test_verified_login_starts_new_cycle_without_erasing_history(store):
    a = seeded(store)[0]
    store.plan(a["id"], "relogin")
    j = store.claim("worker")
    store.finish(j["id"], "worker", j["attempt"], False, error="no_sms")
    store.test_clock[0] += 86400
    store.plan(a["id"], "relogin")
    j = store.claim("worker")
    with pytest.raises(DomainError, match="verified_credential_receipt_required"):
        store.finish_login(j["id"], "worker", j["attempt"], "new", False)
    store.finish_login(j["id"], "worker", j["attempt"], "new-local-test", True)
    d = store.snapshot()
    account = next(x for x in d["accounts"] if x["id"] == a["id"])
    assert account["relogin_failures"] == 0 and account["credential_version"] == 2
    assert account["credential_status"] == "pending"
    assert len([j for j in d["jobs"] if j["kind"] == "relogin"]) == 2
    assert len([j for j in d["jobs"] if j["kind"] == "balance"]) == 1


def test_current_answer_verifies_old_block_but_not_other_models(store):
    rows = seeded(store)
    a = next(x for x in rows if x["status"] == "permission")
    store.block_model(a["id"], "another-model", "permission", 1, store.clock())
    store.put_credential(a["id"], "new-test-credential", 1)
    drain(store)
    store.plan(a["id"], "verify")
    drain(store)
    current = next(x for x in store.snapshot()["accounts"] if x["id"] == a["id"])
    assert current["status"] == "ready"
    with store.tx() as (s, _):
        other = s.scalar(select(ModelState).where(ModelState.account_id == a["id"], ModelState.model == "another-model"))
        assert other.verified_at is None


def test_balance_can_refresh_again_today_but_pending_query_dedupes(store):
    a = seeded(store)[0]
    first = store.plan(a["id"], "balance")
    assert store.plan(a["id"], "balance") == first
    drain(store)
    second = store.plan(a["id"], "balance")
    assert second != first
    drain(store)
    assert len([j for j in store.snapshot()["jobs"] if j["kind"] == "balance"]) == 2


def test_queued_checkin_rebinds_new_credential_without_duplicate(store):
    a = seeded(store)[0]
    first = store.plan(a["id"], "checkin")
    store.put_credential(a["id"], "new", 1)
    store.schedule_today()
    j = next(j for j in store.snapshot()["jobs"] if j["id"] == first)
    assert j["status"] == "queued" and j["credential_version"] == 2


def test_confirmed_old_credential_checkin_is_kept_without_credential_rollback(store):
    a = seeded(store)[0]
    store.plan(a["id"], "checkin")
    j = store.claim("worker")
    store.put_credential(a["id"], "new", 1)
    store.finish(j["id"], "worker", j["attempt"], True)
    d = store.snapshot()
    row = next(x for x in d["accounts"] if x["id"] == a["id"])
    assert row["credential_version"] == 2 and row["credential_status"] == "pending"
    assert row["checkin_day"] == day_at(store.clock())
    assert all(j["credential_version"] == 2 for j in d["jobs"] if j["kind"] == "balance")
    with pytest.raises(DomainError, match="already_checked_in"):
        store.plan(a["id"], "checkin")


def test_after_checkin_balance_counts_do_not_include_manual_queries(store):
    a = seeded(store)[0]
    store.plan(a["id"], "balance")
    drain(store)
    assert store.snapshot()["after_checkin_balance"] == {}
    store.plan(a["id"], "checkin")
    drain(store)
    assert store.snapshot()["after_checkin_balance"] == {"succeeded": 1}
    assert store.snapshot()["today"]["balance"] == {"succeeded": 2}
