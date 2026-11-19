"""Capacity queue responses must not mark otherwise healthy accounts limited."""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import ModelState, RequestAttempt
from app.store import Store
from app.upstream import classify, frame_failure
from test_gateway import FakeRegistry, delta, execute


@pytest.fixture
def store(tmp_path):
    result = Store(f"sqlite:///{tmp_path/'queue.db'}", Fernet.generate_key())
    yield result
    result.engine.dispose()


def test_queue_requires_exact_structured_429_code_combination():
    for value in (6020, "6020"):
        failure = classify(429, value)
        assert (failure.code, failure.status, failure.retryable) == ("upstream_queue_wait", 429, True)
    assert frame_failure({"error": {"status": 429, "code": 6020}}).code == "upstream_queue_wait"
    assert classify(429).code == "rate_limit"
    assert classify(429, 6021).code == "rate_limit"
    assert classify(400, 6020).code == "upstream_rejected"
    assert classify(429, "rate_limit_exceeded", 60).retry_after == 60


def test_queue_can_retry_without_restricting_account_or_model(store):
    registry = FakeRegistry([[classify(429, 6020)], [delta("OK"), {"_done": True}]])
    result = execute(store, registry)
    assert result[-1] == {"finish": "stop"} and len(set(registry.calls)) == 2
    with store.read() as (session, _):
        assert list(session.scalars(select(ModelState))) == []
        attempts = list(session.scalars(select(RequestAttempt)))
        assert sum(a.code == "upstream_queue_wait" for a in attempts) == 1
    assert all(a["enabled"] and a["credential_status"] == "valid" and a["balance"] == 200 for a in store.snapshot()["accounts"])


def test_queue_exhaustion_is_visible_without_account_penalty(store):
    registry = FakeRegistry([[classify(429, 6020)]] * 3)
    result = execute(store, registry)
    assert result[-1]["error"]["code"] == "upstream_queue_wait"
    assert len(registry.calls) == 3
    assert store.snapshot()["requests"][0]["error_code"] == "upstream_queue_wait"
    with store.read() as (session, _):
        assert list(session.scalars(select(ModelState))) == []


def test_queue_does_not_restart_startup_deadline(store):
    registry = FakeRegistry([[.03, classify(429, 6020)], [.12, delta("late")]])
    result = execute(store, registry, startup=.09)
    assert result[-1]["error"]["code"] == "startup_timeout"
    assert store.snapshot()["requests"][0]["first_content_at"] is None


def test_queue_after_effective_content_is_not_replayed(store):
    registry = FakeRegistry([[delta("partial"), classify(429, 6020)]])
    result = execute(store, registry)
    assert result[-1]["error"]["code"] == "upstream_queue_wait"
    assert len(registry.calls) == 1


def test_plain_429_retains_model_rate_limit(store):
    registry = FakeRegistry([[classify(429)], [delta("OK"), {"_done": True}]])
    execute(store, registry)
    with store.read() as (session, _):
        restrictions = list(session.scalars(select(ModelState)))
        assert len(restrictions) == 1 and restrictions[0].reason == "rate_limit"
