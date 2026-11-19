import asyncio
import threading
from types import SimpleNamespace

import anyio
import pytest
from cryptography.fernet import Fernet

from app.registration import RegistrationService
from app.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'cancel.db'}", Fernet.generate_key())
    service = RegistrationService(store, SimpleNamespace(enabled=True))
    sid = service.create("Loomy", "synthetic", "sms", {"phone": "13800000001"})
    yield store, service, sid
    store.engine.dispose()


def no_leases(store):
    with store.read() as (s, _):
        assert store.execution_leases(s) == []


@pytest.mark.parametrize("commit_first", [False, True])
def test_cancel_during_database_acquisition_cannot_orphan_a_slot(env, monkeypatch, commit_first):
    store, service, sid = env
    entered, release = threading.Event(), threading.Event()
    original = store.acquire_execution

    def acquire(*args):
        if commit_first:
            result = original(*args)
        entered.set()
        assert release.wait(5)
        return result if commit_first else original(*args)

    monkeypatch.setattr(store, "acquire_execution", acquire)

    async def flow():
        async def action():
            async with service.execution(sid):
                pytest.fail("cancelled acquisition must not execute registration")
        task = asyncio.create_task(action())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(flow())
    no_leases(store)


def test_http_cancel_scope_releases_registration_slot(env):
    store, service, sid = env

    async def flow():
        with anyio.CancelScope() as scope:
            async with service.execution(sid):
                scope.cancel()
                await asyncio.sleep(0)
        assert scope.cancel_called

    asyncio.run(flow())
    no_leases(store)


def test_cancelled_login_marks_uncertain_and_closes_transport(env):
    store, service, sid = env
    closed = []
    entered = asyncio.Event()

    class Transport:
        async def close(self):
            await asyncio.sleep(0)
            closed.append(True)

    class Adapter:
        def __init__(self, transport):
            pass

        async def start(self, material, mode):
            entered.set()
            await asyncio.sleep(20)

    service.registry.transport_factory = lambda **kwargs: Transport()
    service.factory = Adapter

    async def flow():
        async def cancel_after_start(scope):
            await entered.wait()
            scope.cancel()
        with anyio.CancelScope() as scope:
            canceller = asyncio.create_task(cancel_after_start(scope))
            try:
                await service.run(sid)
            finally:
                await asyncio.gather(canceller)

    asyncio.run(flow())
    assert closed == [True]
    assert service.details(sid)[0]["status"] == "uncertain"
    no_leases(store)


def test_transport_close_error_does_not_replace_saved_challenge(env):
    store, service, sid = env

    class Transport:
        async def close(self):
            raise OSError("synthetic close failure")

    class Adapter:
        def __init__(self, transport):
            pass

        async def start(self, material, mode):
            return {"challenge": "synthetic-challenge", "device": "synthetic"}

    service.registry.transport_factory = lambda **kwargs: Transport()
    service.factory = Adapter
    assert asyncio.run(service.run(sid))["status"] == "waiting_code"
    assert service.details(sid)[0]["status"] == "waiting_code"
    no_leases(store)
