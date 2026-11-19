import asyncio
import threading
import time

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.gateway import Gateway
from app.models import Lease, RequestAttempt, RequestLog
from app.store import DomainError, Store
from app.upstream import UpstreamError
from test_gateway import FakeRegistry, delta, real_accounts


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path/'lease.db'}", Fernet.generate_key())
    yield s
    s.engine.dispose()


@pytest.mark.parametrize("commit_before_cancel", [False, True])
def test_retry_acquisition_cancel_before_or_after_commit_is_clean(store, monkeypatch, commit_before_cancel):
    token = real_accounts(store, 2)
    current = store.acquire(token, "model")
    original = store.acquire
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    def acquire(*args, **kwargs):
        try:
            if commit_before_cancel:
                result = original(*args, **kwargs)
            entered.set()
            assert release.wait(10)
            return result if commit_before_cancel else original(*args, **kwargs)
        finally:
            returned.set()

    monkeypatch.setattr(store, "acquire", acquire)
    registry = FakeRegistry([[UpstreamError("upstream_5xx", 502, True)]])

    async def run():
        async def consume():
            return [frame async for frame in Gateway(store, registry).frames(token, {"model": "model"}, current)]
        consumer = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            release.set()
            assert await asyncio.to_thread(returned.wait, 10)
        finally:
            release.set()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())
    with store.read() as (s, _):
        assert list(s.scalars(select(Lease))) == []
        request = s.get(RequestLog, current["id"])
        assert request.status == "cancelled"
        attempts = list(s.scalars(select(RequestAttempt)))
        assert len(attempts) == (2 if commit_before_cancel else 1)
        assert all(a.ended_at is not None for a in attempts)
    assert len(registry.calls) == 1


@pytest.mark.parametrize("failure,expected", [(DomainError("request_lease_lost"), "request_lease_lost"), (RuntimeError("synthetic database failure"), "internal_error")])
def test_heartbeat_failure_interrupts_silent_upstream_immediately(store, monkeypatch, failure, expected):
    token = real_accounts(store, 1)
    current = store.acquire(token, "model")

    def heartbeat(_):
        raise failure

    monkeypatch.setattr(store, "heartbeat_request", heartbeat)
    registry = FakeRegistry([[20.0]])

    async def run():
        gateway = Gateway(store, registry, startup=30)
        gateway.heartbeat_interval = .01
        async def consume():
            return [frame async for frame in gateway.frames(token, {"model": "model"}, current)]
        return await asyncio.wait_for(consume(), 1)

    frames = asyncio.run(run())
    assert frames[-1]["error"]["code"] == expected
    assert registry.closed == 1
    with store.read() as (s, _):
        assert list(s.scalars(select(Lease))) == []
        assert s.get(RequestLog, current["id"]).status == "failed"


def test_heartbeat_does_not_treat_retry_admission_gap_as_lost_active_lease(store, monkeypatch):
    token = real_accounts(store, 2)
    current = store.acquire(token, "model")
    original = store.acquire
    heartbeat = store.heartbeat_request
    beats = []

    def delayed_acquire(*args, **kwargs):
        time.sleep(.06)
        return original(*args, **kwargs)

    def observed_heartbeat(request_id):
        heartbeat(request_id)
        beats.append(True)

    monkeypatch.setattr(store, "acquire", delayed_acquire)
    monkeypatch.setattr(store, "heartbeat_request", observed_heartbeat)
    registry = FakeRegistry([[.03, UpstreamError("upstream_5xx", 502, True)], [.03, delta("healthy retry"), {"_done": True}]])

    async def run():
        gateway = Gateway(store, registry)
        gateway.heartbeat_interval = .01
        return [frame async for frame in gateway.frames(token, {"model": "model"}, current)]

    frames = asyncio.run(run())
    assert frames[-1] == {"finish": "stop"} and len(registry.calls) == 2
    assert len(beats) >= 2
    with store.read() as (s, _):
        assert list(s.scalars(select(Lease))) == []
        assert s.get(RequestLog, current["id"]).status == "succeeded"


@pytest.mark.parametrize("after_content", [False, True])
def test_http_disconnect_cancel_scope_releases_account_immediately(store, after_content):
    """Starlette disconnects use level cancellation, unlike Task.cancel()."""
    import anyio
    token = real_accounts(store, 1)
    current = store.acquire(token, "model")
    registry = FakeRegistry([([delta("partial answer")] if after_content else []) + [30.0]])
    original_make = registry.make

    def make(*args, **kwargs):
        adapter = original_make(*args, **kwargs)
        close = adapter.close

        async def asynchronous_close():
            await asyncio.sleep(.01)
            await close()

        adapter.close = asynchronous_close
        return adapter

    registry.make = make

    async def run():
        with anyio.move_on_after(.15) as scope:
            async for _ in Gateway(store, registry).frames(token, {"model": "model"}, current):
                pass
        assert scope.cancel_called

    asyncio.run(run())
    with store.read() as (s, _):
        assert list(s.scalars(select(Lease))) == []
        assert s.get(RequestLog, current["id"]).status == "cancelled"
        assert all(a.ended_at is not None for a in s.scalars(select(RequestAttempt)))
    assert registry.closed == 1
