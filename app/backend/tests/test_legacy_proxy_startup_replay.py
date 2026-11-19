"""Sanitized structure replay from station 195; no production traffic."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from app.gateway import Gateway
from app.store import Store
from app.upstream import Transport, UpstreamError, frame_failure, payload
from test_gateway import FakeRegistry, delta, execute, real_accounts


FRAME = {'error': {'code': 'upstream_startup_timeout', 'type': 'proxy_error', 'message': 'sanitized'}}


@pytest.fixture
def store(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'replay.db'}", Fernet.generate_key())
    yield store
    store.engine.dispose()


@pytest.mark.parametrize('status,content_type', [(200,'application/json'), (504,'application/json'), (200,'text/event-stream')])
def test_real_transport_recognizes_exact_proxy_timeout(status, content_type):
    async def run():
        body = json.dumps(FRAME).encode()
        if content_type == 'text/event-stream':
            body = b'data: ' + body + b'\n\n'
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, headers={'content-type':content_type}, content=body)))
        try:
            async for frame in Transport(client).sse('https://example.test/chat', {}, b'{}'):
                failure = frame_failure(frame)
                if failure:
                    raise failure
        finally:
            await client.aclose()
    with pytest.raises(UpstreamError) as error:
        asyncio.run(run())
    assert (error.value.code, error.value.status, error.value.retryable) == ('startup_timeout',504,True)


def test_json_payload_same_structure_and_no_message_matching():
    with pytest.raises(UpstreamError) as error:
        payload(FRAME)
    assert error.value.code == 'startup_timeout'
    for frame in [
        {'error': {'code':'other','type':'proxy_error','message':'upstream_startup_timeout'}},
        {'error': {'code':'upstream_startup_timeout','type':'other'}},
    ]:
        assert frame_failure(frame).code != 'startup_timeout'
    assert frame_failure(delta(json.dumps(FRAME))) is None


def test_before_output_retries_another_account_in_same_platform(store):
    registry = FakeRegistry([[FRAME], [delta('synthetic answer'), {'_done': True}]])
    frames = execute(store, registry)
    assert frames[0]['placeholder']
    assert frames[-1] == {'finish':'stop'}
    assert len(set(registry.calls)) == 2
    assert {r['platform'] for r in store.snapshot()['accounts']} == {'Loomy'}
    assert store.snapshot()['requests'][0]['status'] == 'succeeded'


def test_retry_does_not_reset_shared_startup_deadline(store, monkeypatch):
    now = [100.0]
    monkeypatch.setattr('app.gateway.time', SimpleNamespace(monotonic=lambda: now[0]))
    token = real_accounts(store)

    class TimedRegistry(FakeRegistry):
        def make(self, *args):
            adapter = super().make(*args)
            # The original deadline is 220. A retry is still admissible at
            # 200, but the second adapter is only ready after 220.
            now[0] = 200.0 if len(self.calls) == 1 else 221.0
            return adapter

    registry = TimedRegistry([[FRAME], [delta('too late'), {'_done': True}]])
    gateway = Gateway(store, registry, startup=120)
    original_acquire = gateway.acquire
    deadlines = []

    async def trace_acquire(*args, **kwargs):
        current = await original_acquire(*args, **kwargs)
        deadlines.append(current['_startup_deadline'])
        return current

    monkeypatch.setattr(gateway, 'acquire', trace_acquire)

    async def run():
        current = await gateway.acquire(token, 'model')
        return [item async for item in gateway.frames(token, {'model': 'model', 'messages': []}, current)]

    frames = asyncio.run(run())
    assert deadlines == [220, 220]
    assert len(set(registry.calls)) == 2
    assert frames[0]['placeholder']
    assert frames[1:] == [{'error': {'code': 'startup_timeout', 'type': 'upstream_error', 'status': 504}}]


def test_no_replay_after_effective_output(store):
    registry = FakeRegistry([[delta('partial answer'), FRAME], [delta('must not run')]])
    frames = execute(store, registry)
    assert frames[-1]['error']['code'] == 'startup_timeout'
    assert len(registry.calls) == 1
    assert store.snapshot()['requests'][0]['status'] == 'failed'
