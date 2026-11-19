"""Isolated timing evidence: real httpx response boundary, never live upstream."""
import asyncio
import json
import threading

import httpx
import pytest
from sqlalchemy import select

from app.gateway import Gateway
from app.models import Event, Lease, RequestAttempt, RequestLog
from app.store import DomainError
from app.upstream import Transport, UpstreamError
from test_gateway import FakeRegistry, delta, real_accounts, store


class DelayedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        for frame in ({'choices': [{'delta': {'role': 'assistant'}}]}, delta('OK'), {'_done': True}):
            await asyncio.sleep(.015)
            yield ('data: '+json.dumps(frame)+'\n\n').encode()


class HttpRegistry:
    def __init__(self, client):
        self.client = client

    def make(self, *args):
        class Adapter:
            def __init__(self, client):
                self.t = Transport(client)

            async def chat(self, body, session):
                async for frame in self.t.sse('https://isolated.invalid/chat', {}, b'{}'):
                    yield frame
        return Adapter(self.client)


def test_http_response_first_frame_and_effective_content_have_distinct_boundaries(store):
    token = real_accounts(store, 1)
    current = store.acquire(token, 'model')
    writes = []
    original = store.mark_upstream_progress
    def observe(receipt, stage):
        writes.append((receipt['attempt_id'], stage))
        original(receipt, stage)
    store.mark_upstream_progress = observe
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=DelayedBody()))) as client:
            return [f async for f in Gateway(store, HttpRegistry(client)).frames(token, {'model': 'model'}, current)]
    assert asyncio.run(run())[-1] == {'finish': 'stop'}
    with store.read() as (session, _):
        attempt = session.get(RequestAttempt, current['attempt_id'])
        log = session.get(RequestLog, current['id'])
        assert log.placeholder_at <= attempt.upstream_response_at < attempt.first_frame_at < log.first_content_at
    assert writes == [(current['attempt_id'], 'upstream_response_at'), (current['attempt_id'], 'first_frame_at')]


def test_http_error_records_headers_but_no_first_frame(store):
    token = real_accounts(store, 1)
    current = store.acquire(token, 'model')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={'error': {'code': 'invalid_request_error'}}))) as client:
            return [f async for f in Gateway(store, HttpRegistry(client)).frames(token, {'model': 'model'}, current)]
    assert asyncio.run(run())[-1]['error']['code'] == 'request_rejected'
    with store.read() as (session, _):
        attempt = session.get(RequestAttempt, current['attempt_id'])
        assert attempt.upstream_response_at is not None and attempt.first_frame_at is None


def test_scripted_retry_records_each_first_frame_without_fabricated_headers(store):
    token = real_accounts(store, 2)
    current = store.acquire(token, 'model', client_source='codex')
    registry = FakeRegistry([[{'error': {'code': '6004'}}], [delta('OK'), {'_done': True}]])
    async def run():
        return [f async for f in Gateway(store, registry).frames(token, {'model': 'model'}, current)]
    assert asyncio.run(run())[-1] == {'finish': 'stop'}
    with store.read() as (session, _):
        attempts = list(session.scalars(select(RequestAttempt)))
        events = list(session.scalars(select(Event).where(Event.code == 'first_frame_at')))
        assert len(attempts) == len(events) == 2
        assert all(a.first_frame_at is not None and a.upstream_response_at is None for a in attempts)
        assert session.get(RequestLog, current['id']).client_source == 'codex'


@pytest.mark.parametrize('stage', ['upstream_response_at', 'first_frame_at'])
def test_progress_idempotence_and_stale_receipt_cannot_change_retry(store, stage):
    token = real_accounts(store, 2)
    current = store.acquire(token, 'model')
    store.mark_upstream_progress(current, stage)
    with store.read() as (session, _):
        timestamp = getattr(session.get(RequestAttempt, current['attempt_id']), stage)
    store.mark_upstream_progress(current, stage)
    store.fail_attempt(current, UpstreamError('network', 502, True))
    next_attempt = store.acquire(token, 'model', excluded={current['account_id']}, request_id=current['id'])
    with pytest.raises(DomainError, match='stale_request_receipt'):
        store.mark_upstream_progress(current, stage)
    with store.read() as (session, _):
        assert getattr(session.get(RequestAttempt, current['attempt_id']), stage) == timestamp
        assert getattr(session.get(RequestAttempt, next_attempt['attempt_id']), stage) is None
        assert len(list(session.scalars(select(Event).where(Event.code == stage)))) == 1
    store.end_request(current['id'], 'cancelled', 'cancelled')


def test_expired_progress_is_mapped_to_lease_lost_without_retry(store):
    token = real_accounts(store, 1)
    current = store.acquire(token, 'model')
    with store.tx() as (session, _):
        session.get(Lease, current['lease_id']).expires_at = store.clock()-1
    async def run():
        return [f async for f in Gateway(store, FakeRegistry([[delta('OK')]])).frames(token, {'model': 'model'}, current)]
    assert asyncio.run(run())[-1]['error']['code'] == 'request_lease_lost'
    with store.read() as (session, _):
        assert session.get(RequestAttempt, current['attempt_id']).first_frame_at is None


def test_cancel_waits_for_pending_progress_before_releasing_lease(store):
    token = real_accounts(store, 1)
    current = store.acquire(token, 'model')
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    original = store.mark_upstream_progress
    def delayed(receipt, stage):
        entered.set()
        assert release.wait(5)
        original(receipt, stage)
        committed.set()
    store.mark_upstream_progress = delayed
    async def run():
        async def consume():
            return [f async for f in Gateway(store, FakeRegistry([[delta('OK')]])).frames(token, {'model': 'model'}, current)]
        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            with store.read() as (session, _):
                assert session.get(Lease, current['lease_id']) is not None
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert committed.is_set()
    asyncio.run(run())
    with store.read() as (session, _):
        assert session.get(Lease, current['lease_id']) is None
        assert session.get(RequestLog, current['id']).status == 'cancelled'
