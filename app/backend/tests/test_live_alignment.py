"""Behaviors aligned with the live services after the 2026-11-17 read-only audit (docs/线上核查-api-server-20261117.md)."""
import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.adapters import Loomy
from app.models import Account, Job, ModelState
from app.operations import DEFAULT_SETTINGS, Supervisor
from app.store import Store, day_at
from app.upstream import Transport, UpstreamError, classify
from test_gateway import FakeRegistry, delta, execute, real_accounts


@pytest.fixture
def store(tmp_path):
    now = [1794801600.0]
    s = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key(), clock=lambda: now[0])
    s.now = now
    yield s
    s.engine.dispose()


def test_old_balance_does_not_take_accounts_out_of_service(store):
    token = real_accounts(store, 1)
    store.now[0] += 6 * 3600  # maintenance stalled for hours
    assert store.snapshot()["accounts"][0]["status"] == "ready"
    assert store.acquire(token, "model")["platform"] == "Loomy"


def test_unsigned_accounts_keep_periodic_balance_only_confirmed_zero_waits(store):
    real_accounts(store, 2)
    with store.tx() as (s, _):
        zero, positive = list(s.scalars(select(Account).order_by(Account.external_id)))
        zero.balance = 0
    store.now[0] += 7200
    supervisor = Supervisor(store, type("W", (), {"capacity": 8})(), type("G", (), {})(), ".")
    supervisor.schedule({**DEFAULT_SETTINGS, "daily_checkin": True, "refresh_balances": True})
    with store.read() as (s, _):
        queued = {(j.account_id, j.kind) for j in s.scalars(select(Job).where(Job.status == "queued"))}
    assert (positive.id, "balance") in queued and (zero.id, "balance") not in queued
    assert (zero.id, "checkin") in queued and (positive.id, "checkin") in queued


def test_failed_checkin_retries_same_day_up_to_limit_but_uncertain_never(store):
    real_accounts(store, 2)
    first, second = [a["id"] for a in store.snapshot()["accounts"]]
    # Saving a credential already queued today's sign-in for both accounts.
    assert store.schedule_today() == 0
    for _ in range(2):
        job = store.claim("w", 8)
        store.finish(job["id"], "w", job["attempt"], False, error="network" if job["account_id"] == first else "unconfirmed_checkin")
    # Not before the retry interval.
    assert store.schedule_today() == 0
    store.now[0] += 1801
    assert store.schedule_today() == 2
    # Lost result (uncertain) is never resent automatically.
    job = store.claim("w", 8)
    store.now[0] += 120
    store.claim("other", 8)
    for _ in range(4):
        store.now[0] += 1801
        store.schedule_today()
        while j := store.claim("w", 8):
            store.finish(j["id"], "w", j["attempt"], False, error="network")
    with store.read() as (s, _):
        attempts = {j.account_id: (j.attempt, j.status) for j in s.scalars(select(Job).where(Job.kind == "checkin"))}
    assert max(a for a, _ in attempts.values()) <= 3
    assert attempts[job["account_id"]][1] == "uncertain"


@pytest.mark.parametrize("first", [
    {"choices": [{"delta": {"reasoning": "thinking"}}]},
    {"choices": [{"delta": {"function_call": {"name": "lookup", "arguments": "{}"}}}]},
])
def test_reasoning_and_function_call_count_as_output(store, first):
    reg = FakeRegistry([[first, {"choices": [{"delta": {}, "finish_reason": "stop"}]}, {"_done": True}]])
    result = execute(store, reg)
    assert result[-1] == {"finish": "stop"} and len(reg.calls) == 1


def test_content_filter_is_a_result_not_an_empty_reply_to_retry(store):
    reg = FakeRegistry([[{"choices": [{"delta": {}, "finish_reason": "content_filter"}]}, {"_done": True}], [delta("should not run")]])
    result = execute(store, reg)
    assert result[-1] == {"finish": "content_filter"} and len(reg.calls) == 1
    log = store.snapshot()["requests"][0]
    assert log["status"] == "failed" and log["error_code"] == "content_filter"


@pytest.mark.parametrize("code", ["4100406", "InvalidArgument", "context_length_exceeded"])
def test_refused_request_is_not_retried_and_does_not_restrict_account(store, code):
    error = classify(400, code)
    assert error.code == "request_rejected" and not error.retryable
    reg = FakeRegistry([[error], [delta("never")]])
    result = execute(store, reg)
    assert result[-1]["error"]["code"] == "request_rejected" and len(reg.calls) == 1
    with store.read() as (s, _):
        assert list(s.scalars(select(ModelState))) == []
    assert all(a["status"] == "ready" for a in store.snapshot()["accounts"])


def test_loomy_lost_checkin_is_reconciled_by_idempotent_first_login():
    day = "2026-11-17"
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"code": "000000", "data": {"alreadyProcessed": True, "dailyCycleDate": day, "dailyQuota": 5000}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Loomy(Transport(client), {"session": "synthetic"}, "a")
            assert await adapter.checkin_receipt(day) == {"day": day}
            with pytest.raises(UpstreamError):
                await adapter.checkin_receipt("2026-11-18")

    asyncio.run(run())
    assert all(path.endswith("/points/first-login") and method == "POST" for method, path in calls)


def test_keepalive_during_slow_first_output_and_cancel_releases_slot(store, monkeypatch):
    import app.main as main_module
    from fastapi.testclient import TestClient
    from app.models import Lease
    monkeypatch.setattr(main_module, "KEEPALIVE_SECONDS", 0.05)
    token = real_accounts(store, 1)
    reg = FakeRegistry([[0.2, delta("late answer"), {"choices": [{"delta": {}, "finish_reason": "stop"}]}, {"_done": True}]])
    app = main_module.create_app(store, start_worker=False, registry=reg)
    with TestClient(app) as client:
        for path, body, ping in [
            ("/v1/chat/completions", {"model": "model", "stream": True, "messages": [{"role": "user", "content": "q"}]}, ": keepalive"),
        ]:
            r = client.post(path, headers={"Authorization": f"Bearer {token}"}, json=body)
            text = r.text
            assert r.status_code == 200 and ping in text
            # The placeholder still comes first, keepalives never carry content, the answer arrives intact.
            assert text.index('"content": " "') < text.index(ping) < text.index("late answer")
            assert "[DONE]" in text
    log = store.snapshot()["requests"][0]
    assert log["status"] == "succeeded" and log["first_content_at"] is not None
    with store.read() as (s, _):
        assert list(s.scalars(select(Lease))) == []


def test_paced_cancellation_does_not_leave_the_stream_running():
    from app.main import KEEPALIVE, paced

    closed = []

    async def slow():
        try:
            await asyncio.sleep(10)
            yield "never"
        finally:
            closed.append(True)

    async def run():
        from contextlib import aclosing
        async with aclosing(slow()) as upstream, aclosing(paced(upstream, 0.01)) as frames:
            first = await anext(frames)
            assert first is KEEPALIVE
        # Leaving the block cancels the pending read and then closes the upstream generator.

    asyncio.run(run())
    assert closed == [True]
