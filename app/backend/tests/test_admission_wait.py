import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import select, func

from app.dispatch_policy import PolicyInput, save
from app.gateway import Gateway
from app.models import Account, Affinity, Lease, RequestLog
from app.store import Store, DomainError
from app.upstream import UpstreamError
from test_gateway import real_accounts, FakeRegistry, delta


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'wait.db'}", Fernet.generate_key())
    token = real_accounts(store, 1)
    yield store, token
    store.engine.dispose()


def configure(store, seconds=1, **kwargs):
    save(store, 'Loomy', PolicyInput(expected_version=0, strategy='lru', queue_wait_seconds=seconds, **kwargs))


def test_default_full_returns_immediately_without_new_lease_or_request(env):
    store, token = env
    store.acquire(token, 'model')
    gateway = Gateway(store, FakeRegistry([]))
    started = time.monotonic()
    with pytest.raises(DomainError) as error:
        asyncio.run(gateway.acquire(token, 'model'))
    assert error.value.code == 'account_concurrency_busy'
    assert time.monotonic()-started < .4
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Lease)) == 1
        assert s.scalar(select(func.count()).select_from(RequestLog)) == 1


def test_ineligible_account_does_not_wait_or_cross_platform(env):
    store, token = env
    configure(store)
    with store.tx() as (s, _):
        s.scalar(select(Account)).balance = 0
    started = time.monotonic()
    with pytest.raises(DomainError) as error:
        asyncio.run(Gateway(store, FakeRegistry([])).acquire(token, 'model'))
    assert error.value.code == 'no_available_account'
    assert time.monotonic()-started < .4


def test_local_release_wakes_waiter_without_overbooking(env):
    store, token = env
    configure(store)
    held = store.acquire(token, 'model')
    gateway = Gateway(store, FakeRegistry([]))

    async def run():
        waiter = asyncio.create_task(gateway.acquire(token, 'model'))
        await asyncio.sleep(.05)
        assert not waiter.done()
        with store.read() as (s, _):
            assert s.scalar(select(func.count()).select_from(Lease)) == 1
        await asyncio.to_thread(store.end_request, held['id'], 'cancelled', 'cancelled')
        released_at = time.monotonic()
        gateway.slots_changed()
        selected = await waiter
        assert time.monotonic()-released_at < .3
        assert selected['platform'] == 'Loomy' and selected['account_id'] == held['account_id']
        assert selected['_queue_wait_remaining'] < 1
        return selected

    current = asyncio.run(run())
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Lease)) == 1
        assert s.get(RequestLog, current['id']).status == 'inflight'


def test_wait_cancel_does_not_take_slot_or_modify_existing_request(env):
    store, token = env
    configure(store)
    held = store.acquire(token, 'model')
    gateway = Gateway(store, FakeRegistry([]))

    async def run():
        waiter = asyncio.create_task(gateway.acquire(token, 'model'))
        await asyncio.sleep(.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    asyncio.run(run())
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(RequestLog)) == 1
        assert s.get(RequestLog, held['id']).status == 'inflight'


def test_other_process_release_is_found_by_bounded_timer(env):
    store, token = env
    configure(store)
    held = store.acquire(token, 'model')
    gateway = Gateway(store, FakeRegistry([]))

    async def run():
        waiter = asyncio.create_task(gateway.acquire(token, 'model'))
        await asyncio.sleep(.05)
        # No local wake event: equivalent to another process releasing in DB.
        await asyncio.to_thread(store.end_request, held['id'], 'cancelled', 'cancelled')
        return await waiter

    assert asyncio.run(run())['account_id'] == held['account_id']


def test_many_clients_wait_without_exceeding_single_account_concurrency(env):
    store, token = env
    configure(store, seconds=2)
    gateway = Gateway(store, FakeRegistry([]))

    async def client():
        current = await gateway.acquire(token, 'model')
        with store.read() as (s, _):
            assert s.scalar(select(func.count()).select_from(Lease)) == 1
        await asyncio.sleep(.005)
        await asyncio.to_thread(store.end_request, current['id'], 'cancelled', 'cancelled')
        gateway.slots_changed()
        return current['id']

    async def run():
        return await asyncio.gather(*(client() for _ in range(12)))

    assert len(set(asyncio.run(run()))) == 12
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Lease)) == 0


def test_cancel_during_late_atomic_acquisition_releases_its_lease(env, monkeypatch):
    store, token = env
    original = store.acquire
    began, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        began.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'acquire', delayed)
    gateway = Gateway(store, FakeRegistry([]))

    async def run():
        pending = asyncio.create_task(gateway.acquire(token, 'model'))
        assert await asyncio.to_thread(began.wait, 2)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(run())
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Lease)) == 0
        assert s.scalar(select(RequestLog)).status == 'cancelled'


def test_wait_consumes_original_startup_budget_in_frames(env, monkeypatch):
    store, token = env
    configure(store)
    held = store.acquire(token, 'model')
    registry = FakeRegistry([[delta('too late'), {'_done': True}]])
    gateway = Gateway(store, registry, startup=120)
    now = [100.0]
    monkeypatch.setattr('app.gateway.time', SimpleNamespace(monotonic=lambda: now[0]))
    original_acquire = store.acquire

    async def run():
        loop = asyncio.get_running_loop()
        saw_full_slot = asyncio.Event()

        def acquire(*args, **kwargs):
            try:
                return original_acquire(*args, **kwargs)
            except DomainError as failure:
                if failure.code == 'account_concurrency_busy':
                    loop.call_soon_threadsafe(saw_full_slot.set)
                raise

        monkeypatch.setattr(store, 'acquire', acquire)
        pending = asyncio.create_task(gateway.acquire(token, 'model'))
        try:
            await asyncio.wait_for(saw_full_slot.wait(), timeout=5)
            await asyncio.to_thread(store.end_request, held['id'], 'cancelled', 'cancelled')
            now[0] = 100.75  # The queue still has time left when the slot is released.
            gateway.slots_changed()
            current = await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        assert current['_startup_deadline'] == 220
        now[0] = 220  # Original deadline expired, but a fresh budget would still allow output.
        result = [item async for item in gateway.frames(token, {'model':'model','messages':[]}, current)]
        assert result[0]['placeholder']
        assert result[1:] == [{'error': {'code': 'startup_timeout', 'type': 'upstream_error', 'status': 504}}]
        return current['id']

    request_id = asyncio.run(run())
    with store.read() as (s, _):
        assert s.get(RequestLog, request_id).status == 'failed'
        assert s.scalar(select(func.count()).select_from(Lease)) == 0


def test_bounded_wait_does_not_poll_busy_and_does_not_create_request(env, monkeypatch):
    store, token = env
    configure(store)
    store.acquire(token, 'model')
    original, calls = store.acquire, []

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'acquire', counted)
    began = time.monotonic()
    with pytest.raises(DomainError) as error:
        asyncio.run(Gateway(store, FakeRegistry([])).acquire(token, 'model'))
    assert error.value.code == 'account_concurrency_busy'
    assert .9 < time.monotonic()-began < 1.4
    assert len(calls) <= 3


def test_startup_deadline_shorter_than_queue_bound_wins(env):
    store, token = env
    configure(store)
    store.acquire(token, 'model')
    with pytest.raises(UpstreamError) as error:
        asyncio.run(Gateway(store, FakeRegistry([]), startup=.05).acquire(token, 'model'))
    assert error.value.code == 'startup_timeout'


def test_disconnected_request_is_never_admitted(env):
    store, token = env

    async def disconnected():
        return True

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Gateway(store, FakeRegistry([])).acquire(token, 'model', is_disconnected=disconnected))
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Lease)) == 0


def test_disabled_affinity_ignores_but_does_not_delete_existing_bindings(env):
    store, token = env
    held = store.acquire(token, 'model', conversation='synthetic')
    store.end_request(held['id'], 'cancelled', 'cancelled')
    with store.read() as (s, _):
        old = s.scalar(select(Affinity))
        old_id, old_expiry = old.id, old.expires_at
    configure(store, affinity_ttl_seconds=0)
    current = store.acquire(token, 'model', conversation='synthetic')
    assert current['session'] != held['session']
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Affinity)) == 1
        assert s.get(Affinity, old_id).expires_at == old_expiry


def test_shorter_affinity_ttl_and_partial_policy_save_keep_configuration(env):
    store, token = env
    held = store.acquire(token, 'model', conversation='synthetic')
    store.end_request(held['id'], 'cancelled', 'cancelled')
    configure(store, affinity_ttl_seconds=60)
    save(store, 'Loomy', PolicyInput(expected_version=1, strategy='balance_high'))
    current = store.acquire(token, 'model', conversation='synthetic')
    assert current['session'] == held['session']
    assert current['queue_wait_seconds'] == 1
    with store.read() as (s, _):
        assert 0 < s.scalar(select(Affinity)).expires_at-store.clock() <= 60


def test_shorter_ttl_expires_old_binding_even_without_intervening_requests(tmp_path):
    now = [10000.0]
    store = Store(f"sqlite:///{tmp_path/'ttl.db'}", Fernet.generate_key(), clock=lambda: now[0])
    try:
        token = real_accounts(store, 2)
        original = store.acquire(token, 'model', conversation='synthetic')
        store.end_request(original['id'], 'cancelled', 'cancelled')
        configure(store, affinity_ttl_seconds=60)
        with store.read() as (s, _):
            assert s.scalar(select(Affinity)).expires_at == 10060
        now[0] += 61
        selected = store.acquire(token, 'model', conversation='synthetic')
        assert selected['account_id'] != original['account_id']
    finally:
        store.engine.dispose()


def test_retry_queue_has_no_heartbeat_on_released_lease(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path/'retry-wait.db'}", Fernet.generate_key())
    try:
        token = real_accounts(store, 2)
        configure(store)
        registry = FakeRegistry([[UpstreamError('network', 502, True)], [delta('answer'), {'_done': True}]])
        gateway = Gateway(store, registry, startup=2)
        gateway.heartbeat_interval = .01
        original_heartbeat, stale_heartbeats = store.heartbeat_request, []

        def checked_heartbeat(request_id):
            try:
                original_heartbeat(request_id)
            except DomainError:
                stale_heartbeats.append(True)
                raise

        monkeypatch.setattr(store, 'heartbeat_request', checked_heartbeat)

        async def run():
            current = await gateway.acquire(token, 'model')
            other = await asyncio.to_thread(store.acquire, token, 'model')
            frames = []

            async def collect():
                async for item in gateway.frames(token, {'model':'model','messages':[]}, current):
                    frames.append(item)

            collecting = asyncio.create_task(collect())
            await asyncio.sleep(.12)  # More than ten heartbeat intervals.
            assert not collecting.done()
            await asyncio.to_thread(store.end_request, other['id'], 'cancelled', 'cancelled')
            gateway.slots_changed()
            await collecting
            assert frames[-1] == {'finish':'stop'}
            assert len(registry.calls) == 2 and not stale_heartbeats
            with store.read() as (s, _):
                assert s.get(RequestLog, current['id']).status == 'succeeded'

        asyncio.run(run())
    finally:
        store.engine.dispose()


def test_retry_joins_blocked_heartbeat_transaction_before_releasing_lease(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path/'heartbeat-join.db'}", Fernet.generate_key())
    entered, allow_heartbeat, released = threading.Event(), threading.Event(), threading.Event()
    try:
        token = real_accounts(store, 2)
        current = store.acquire(token, 'model')
        original_heartbeat, original_release = store.heartbeat_request, store.fail_attempt
        stale = []

        def blocked_heartbeat(request_id):
            entered.set()
            assert allow_heartbeat.wait(3)
            try:
                original_heartbeat(request_id)
            except DomainError:
                stale.append(True)
                raise

        def observed_release(*args):
            released.set()
            return original_release(*args)

        monkeypatch.setattr(store, 'heartbeat_request', blocked_heartbeat)
        monkeypatch.setattr(store, 'fail_attempt', observed_release)

        async def run():
            failure_issued = asyncio.Event()
            registry = FakeRegistry([[], [delta('answer'), {'_done': True}]])
            original_make = registry.make

            def make(*args):
                adapter = original_make(*args)
                if len(registry.calls) == 1:
                    async def fail_with_heartbeat_in_thread(*unused):
                        assert await asyncio.to_thread(entered.wait, 2)
                        failure_issued.set()
                        raise UpstreamError('network', 502, True)
                        yield  # async generator shape
                    adapter.chat = fail_with_heartbeat_in_thread
                return adapter

            registry.make = make
            gateway = Gateway(store, registry)
            gateway.heartbeat_interval = .01

            async def collect():
                return [x async for x in gateway.frames(token, {'model':'model','messages':[]}, current)]

            running = asyncio.create_task(collect())
            try:
                await asyncio.wait_for(failure_issued.wait(), 2)
                # The upstream failed, but release MUST wait for the in-flight
                # heartbeat DB call, not merely its cancelled coroutine.
                assert not await asyncio.to_thread(released.wait, .08)
            finally:
                allow_heartbeat.set()
            frames = await running
            assert frames[-1] == {'finish':'stop'} and released.is_set()
            assert not stale

        asyncio.run(run())
    finally:
        allow_heartbeat.set()
        store.engine.dispose()


@pytest.mark.parametrize('values', [{'queue_wait_seconds':11}, {'queue_wait_seconds':-1}, {'queue_wait_seconds':True},
                                    {'affinity_ttl_seconds':86401}, {'affinity_ttl_seconds':-1}])
def test_policy_bounds(values):
    with pytest.raises(ValidationError):
        PolicyInput(expected_version=0, strategy='lru', **values)
