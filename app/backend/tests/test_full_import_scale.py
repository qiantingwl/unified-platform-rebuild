"""Full-population scheduling checks; no real accounts or upstream calls.

Assertions use SQL/ORM work counts instead of environment-specific latency.
"""
from collections import Counter
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import insert, update

from app.models import Account, Job, Lease, ModelState
from app.operations import DEFAULT_SETTINGS, Supervisor
from app.store import Store, day_at
from test_routing_large_population import measured


PLATFORMS = ["Loomy", "小浣熊", "WPS 灵犀", "CodeArts", "Marvis"]


@pytest.fixture
def population(tmp_path):
    now = 1794900000.0
    store = Store(f"sqlite:///{tmp_path/'full-population.db'}", Fernet.generate_key(), clock=lambda: now)
    store.real_capabilities = {p: {"chat", "balance", "checkin", "verify"} for p in PLATFORMS}
    accounts, blocks, leases = [], [], []
    for i in range(7000):
        slot = (i // len(PLATFORMS)) % 10
        aid = f"synthetic-{i:05d}"
        accounts.append({"id": aid, "platform": PLATFORMS[i % len(PLATFORMS)],
            "external_id": aid, "label": aid, "sandbox": False, "enabled": slot != 5,
            "credential_version": 1, "credential_status": {1: "pending", 2: "invalid", 3: "missing"}.get(slot, "valid"),
            "balance": 0 if slot == 4 else 100, "balance_at": now, "balance_version": 1,
            "concurrency": 1, "created_at": now, "updated_at": now, "checkin_day": day_at(now)})
        if slot in {6, 7, 8}:
            blocks.append({"id": aid, "account_id": aid, "model": "model", "credential_version": 1,
                "reason": {6: "quota", 7: "network", 8: "rate_limit"}[slot],
                "review_at": now - 1 if slot == 7 else now + 900, "verified_at": None})
        if slot == 9:
            leases.append({"id": aid, "account_id": aid, "request_id": aid, "expires_at": now + 180})
    with store.tx() as (s, _):
        s.execute(insert(Account), accounts)
        s.execute(insert(ModelState), blocks)
        s.execute(insert(Lease), leases)
    tokens = {p: store.create_key("scale-test", p, "model")["token"] for p in PLATFORMS}
    yield store, tokens
    store.engine.dispose()


def test_7000_account_totals_and_per_model_readiness_match_acquisition(population):
    store, tokens = population
    with measured(store, "7000-snapshot") as snapshot_cost:
        snapshot = store.snapshot()
    assert snapshot_cost["sql_calls"] < 60
    assert snapshot["totals"] == {"total": 7000, "ready": 1400, "busy": 700, "unavailable": 4900}
    assert snapshot["reasons"] == dict(Counter(a["status"] for a in snapshot["accounts"]))
    assert all(a["model_readiness"]["model"] == a["status"] for a in snapshot["accounts"])
    by_id = {a["id"]: a for a in snapshot["accounts"]}
    claims = []
    for platform, token in tokens.items():
        with measured(store, "7000-acquire-" + platform) as cost:
            current = store.acquire(token, "model")
        assert cost["Account_loaded"] <= 128
        assert cost["sql_calls"] < 30
        assert by_id[current["account_id"]]["status"] == "ready"
        assert by_id[current["account_id"]]["platform"] == platform
        claims.append(current)
    after = store.snapshot()
    assert after["totals"] == {"total": 7000, "ready": 1395, "busy": 705, "unavailable": 4900}
    for current in claims:
        store.end_request(current["id"], "cancelled", "cancelled")
    assert store.snapshot()["totals"] == snapshot["totals"]


def test_7000_fresh_balances_and_checkins_do_not_issue_per_account_queries(population, tmp_path):
    store, _ = population
    now = store.clock()
    with store.tx() as (s, _):
        # This case models completed credential validation, not the mixed
        # pending population used by the readiness case above.
        s.execute(update(Account).where(Account.credential_status == "pending").values(credential_status="valid"))
        s.execute(insert(Job), [{"id": f"balance-{i:05d}", "dedupe_key": f"balance-{i:05d}",
            "account_id": f"synthetic-{i:05d}", "kind": "balance", "business_day": day_at(now),
            "credential_version": 1, "status": "succeeded", "priority": 20,
            "available_at": now, "created_at": now - 60, "finished_at": now - 50} for i in range(7000)])
    supervisor = Supervisor(store, SimpleNamespace(tasks=[], capacity=64), SimpleNamespace(), tmp_path)
    with measured(store, "7000-stable-maintenance-tick") as cost:
        supervisor.schedule({**DEFAULT_SETTINGS, "daily_checkin": True, "refresh_balances": True})
    # The stable tick has no writes to enqueue. A query per account while
    # holding the global state lock would delay every request admission.
    assert cost["sql_calls"] < 30
    assert store.snapshot()["job_counts"] == {"succeeded": 7000}
