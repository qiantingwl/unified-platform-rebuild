"""P3 forwarding acceptance: isolated DB + controllable upstream, no live traffic."""
import asyncio
import json
import time
import threading
import hashlib
from copy import deepcopy
from types import SimpleNamespace
from contextlib import aclosing
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, func, event

from app.adapters import WorkBuddy, WPS, Registry, encode
from app.vendor.mirai.wps_lingxi import endpoints as wps
from app.gateway import Gateway
from app.main import create_app
from app.models import Account, Lease, RequestAttempt, RequestLog, Resource
from app.store import Store, DomainError, day_at
from app.upstream import UpstreamError, Transport, frame_failure, classify
from app.encrypted_session_cache import EncryptedSessionCache
from app.model_routes import save, RoutesInput
from app.wps_sessions import resolve
from app.wps_request import build as wps_body


def d(**delta):
    return {'choices': [{'index': 0, 'delta': delta}]}


def done(reason='stop'):
    return {'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]}


def answer(text='answer'):
    return [d(content=text), done(), {'_done': True}]


class ScriptedRegistry:
    enabled = True

    def __init__(self, flows):
        self.flows = list(flows)
        self.calls = []
        self.closed = 0
        self.entered = None

    def make(self, platform, credential, identity, version, proxy):
        index = len(self.calls)
        record = {'identity': identity, 'version': version, 'platform': platform}
        self.calls.append(record)
        flow = self.flows[index]
        registry = self

        class Adapter:
            supports_thinking_control = True

            def __init__(self):
                self.t = self

            async def close(self):
                registry.closed += 1

            async def chat(self, body, session):
                record.update(body=deepcopy(body), session=session)
                if registry.entered:
                    registry.entered.set()
                for item in flow:
                    if type(item) is float:
                        await asyncio.sleep(item)
                    elif isinstance(item, Exception):
                        raise item
                    else:
                        yield deepcopy(item)
        return Adapter()


@pytest.fixture
def store(tmp_path):
    now = [time.time()]
    key = Fernet.generate_key()
    result = Store(f'sqlite:///{tmp_path / "forwarding.db"}', key, clock=lambda: now[0])
    result.test_key, result.test_now = key, now
    result.real_capabilities = {'WorkBuddy 国内': {'chat', 'balance', 'checkin'}, 'Loomy': {'chat', 'balance'}}
    yield result
    result.engine.dispose()


def seed(store, n=2, platform='WorkBuddy 国内', model='m'):
    ids = []
    for index in range(n):
        identity = store.create_account(platform, f'p3-{platform}-{index}', f'P3 account {index}')
        store.put_credential(identity, json.dumps({'auth': {'accessToken': 'synthetic'}, 'account': {'uid': str(index)}, 'session': 'synthetic'}), 0)
        with store.tx() as (session, _):
            a = store.account(session, identity)
            a.credential_status, a.balance, a.balance_at, a.balance_version = 'valid', 100, store.clock(), a.credential_version
        ids.append(identity)
    return store.create_key('P3 key', platform, model)['token'], ids


def api(store, registry):
    return TestClient(create_app(store, start_worker=False, registry=registry, role='api'))


def headers(token):
    return {'Authorization': 'Bearer '+token}


def events(response):
    result = []
    for block in response.text.replace('\r\n', '\n').split('\n\n'):
        lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith('data:')]
        if lines and '\n'.join(lines) != '[DONE]':
            result.append(json.loads('\n'.join(lines)))
    return result


def assert_released(store, status=None):
    with store.read() as (session, _):
        assert session.scalar(select(func.count()).select_from(Lease)) == 0
        logs = list(session.scalars(select(RequestLog)))
        if status:
            assert logs and all(log.status == status for log in logs)


@pytest.mark.parametrize('protocol,stream', [('chat', False), ('chat', True), ('responses', False), ('responses', True), ('messages', False), ('messages', True)])
def test_three_protocols_reasoning_parallel_tools_and_terminal(store, protocol, stream):
    token, _ = seed(store)
    flow = [d(reasoning_content='thinking'), d(content='body'),
            d(tool_calls=[{'index': 0, 'id': 'call0', 'function': {'name': 'first', 'arguments': '{"a":'}}]),
            d(tool_calls=[{'index': 1, 'id': 'call1', 'function': {'name': 'second', 'arguments': '{"b":2}'}}]),
            d(tool_calls=[{'index': 0, 'function': {'arguments': '1}'}}]),
            {'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 0}}}, done('tool_calls'), {'_done': True}]
    registry = ScriptedRegistry([flow])
    client = api(store, registry)
    payload = {'model': 'm', 'stream': stream}
    payload.update({'input': 'question'} if protocol == 'responses' else {'messages': [{'role': 'user', 'content': 'question'}]})
    if protocol == 'messages':
        payload['max_tokens'] = 2048
    path = {'chat': '/v1/chat/completions', 'responses': '/v1/responses', 'messages': '/v1/messages'}[protocol]
    response = client.post(path, headers=headers(token), json=payload)
    assert response.status_code == 200, response.text
    if stream:
        rows = events(response)
        assert not any('error' in row for row in rows)
        if protocol == 'chat':
            assert response.text.endswith('data: [DONE]\n\n')
            assert any(x['choices'] and x['choices'][0]['finish_reason'] == 'tool_calls' for x in rows)
        elif protocol == 'responses':
            terminal = rows[-1]['response']
            assert rows[-1]['type'] == 'response.completed'
            calls = [x for x in terminal['output'] if x['type'] == 'function_call']
            assert [(x['name'], json.loads(x['arguments'])) for x in calls] == [('first', {'a': 1}), ('second', {'b': 2})]
            assert client.get('/v1/responses/'+terminal['id'], headers=headers(token)).json() == terminal
        else:
            assert rows[-1]['type'] == 'message_stop'
            opened = None
            for row in rows:
                if row['type'] == 'content_block_start':
                    assert opened is None
                    opened = row['index']
                    if row['content_block']['type'] == 'thinking':
                        assert row['content_block']['signature'] == ''
                elif row['type'] == 'content_block_delta':
                    assert opened == row['index']
                elif row['type'] == 'content_block_stop':
                    assert opened == row['index']
                    opened = None
            assert opened is None
            assert sum(x['type'] == 'content_block_start' and x['content_block']['type'] == 'tool_use' for x in rows) == 2
    else:
        body = response.json()
        if protocol == 'chat':
            assert body['choices'][0]['message']['reasoning_content'] == 'thinking'
            assert len(body['choices'][0]['message']['tool_calls']) == 2
        elif protocol == 'responses':
            assert body['status'] == 'completed' and body['store'] is True
        else:
            assert body['stop_reason'] == 'tool_use'
            assert [x['input'] for x in body['content'] if x['type'] == 'tool_use'] == [{'a': 1}, {'b': 2}]
    assert_released(store, 'succeeded')


@pytest.mark.parametrize('stream', [False, True])
def test_thinking_omitted_has_no_redacted_or_visible_thinking(store, stream):
    token, _ = seed(store)
    registry = ScriptedRegistry([[d(reasoning_content='private thought'), d(content='visible'), done()]])
    response = api(store, registry).post('/v1/messages', headers=headers(token), json={
        'model': 'm', 'messages': [{'role': 'user', 'content': 'Q'}], 'max_tokens': 2048,
        'thinking': {'type': 'adaptive', 'display': 'omitted'}, 'stream': stream})
    assert response.status_code == 200, response.text
    assert 'private thought' not in response.text and 'redacted_thinking' not in response.text
    assert 'thinking_delta' not in response.text and 'visible' in response.text
    outbound = WorkBuddy.outbound(registry.calls[0]['body'])
    assert outbound['thinking'] == {'type': 'enabled'}
    assert_released(store, 'succeeded')


def test_responses_multiturn_tool_result_key_isolation_false_delete_restart(store):
    token, _ = seed(store)
    registry = ScriptedRegistry([[d(tool_calls=[{'index': 0, 'id': 'call0', 'function': {'name': 'lookup', 'arguments': '{}'}}]), done('tool_calls')], answer('followup'), answer('not stored')])
    client = api(store, registry)
    first = client.post('/v1/responses', headers=headers(token), json={'model': 'm', 'input': 'secret input marker', 'instructions': 'first-only instruction'}).json()
    rid = first['id']
    other = store.create_key('other', 'WorkBuddy 国内', 'm')['token']
    assert client.get('/v1/responses/'+rid, headers=headers(other)).status_code == 404
    foreign = client.post('/v1/responses', headers=headers(other), json={'model': 'm', 'input': 'x', 'previous_response_id': rid})
    assert foreign.status_code == 404 and len(registry.calls) == 1
    resumed = Store(store.engine.url.render_as_string(hide_password=False), store.test_key, clock=store.clock)
    try:
        restarted = api(resumed, registry)
        assert restarted.get('/v1/responses/'+rid, headers=headers(token)).json() == first
        second = restarted.post('/v1/responses', headers=headers(token), json={'model': 'm', 'previous_response_id': rid, 'instructions': 'second instruction', 'input': [{'type': 'function_call_output', 'call_id': 'call0', 'output': 'tool value'}]}).json()
        assert second['previous_response_id'] == rid
        messages = registry.calls[1]['body']['messages']
        assert not any(m.get('content') == 'first-only instruction' for m in messages)
        assert any(m.get('content') == 'second instruction' for m in messages)
        assert any(m.get('tool_call_id') == 'call0' and m['content'] == 'tool value' for m in messages)
        assert registry.calls[1]['session'] == registry.calls[0]['session']
        no_store = restarted.post('/v1/responses', headers=headers(token), json={'model': 'm', 'input': 'x', 'store': False}).json()
        assert no_store['store'] is False
        assert restarted.get('/v1/responses/'+no_store['id'], headers=headers(token)).status_code == 404
        assert restarted.delete('/v1/responses/'+rid, headers=headers(token)).json()['deleted'] is True
        assert restarted.get('/v1/responses/'+rid, headers=headers(token)).status_code == 404
    finally:
        resumed.engine.dispose()
    with store.read() as (session, _):
        for row in session.scalars(select(Resource).where(Resource.kind == 'response_history')):
            assert row.data == '{}'
            assert 'secret input marker' not in row.secret and 'tool value' not in row.secret
    assert_released(store, 'succeeded')


def test_history_expiry_and_storage_failure_is_failed_delivery(store, monkeypatch):
    token, _ = seed(store)
    client = api(store, ScriptedRegistry([answer(), answer()]))
    first = client.post('/v1/responses', headers=headers(token), json={'model': 'm', 'input': 'q'}).json()
    store.test_now[0] += 30*86400+1
    assert client.get('/v1/responses/'+first['id'], headers=headers(token)).status_code == 404
    store.test_now[0] -= 30*86400+1
    import app.response_history as history
    def broken(*args, **kwargs):
        raise RuntimeError('synthetic-private-error-never-public')
    monkeypatch.setattr(history, '_complete', broken)
    response = client.post('/v1/responses', headers=headers(token), json={'model': 'm', 'input': 'q'})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'response_history_unavailable'
    assert 'synthetic-private-error' not in response.text
    with store.read() as (session, _):
        logs = list(session.scalars(select(RequestLog).order_by(RequestLog.started_at, RequestLog.id)))
        assert any(r.error_code == 'response_history_unavailable' and r.status == 'failed' for r in logs)


def test_group_priority_main_auto_retry_and_route_change_guard(store):
    token, ids = seed(store, 3, model='main-auto')
    with store.tx() as (session, _):
        store.account(session, ids[0]).priority = 5
        store.account(session, ids[1]).priority = 10
        store.account(session, ids[2]).priority = 100
    config = RoutesInput(expected_version=0, groups=[{'id': 'own', 'name': 'Own group', 'account_ids': ids[:2]}], mappings=[{'model': 'main-auto', 'upstream_model': 'real-model', 'group_id': 'own'}])
    save(store, 'WorkBuddy 国内', config)
    registry = ScriptedRegistry([[{'error': {'code': '6004', 'message': 'model rate limit'}}], answer()])
    response = api(store, registry).post('/v1/chat/completions', headers=headers(token), json={'model': 'main-auto', 'messages': [{'role': 'user', 'content': 'q'}]})
    assert response.status_code == 200, response.text
    assert [c['identity'] for c in registry.calls] == [ids[1], ids[0]]
    assert all(c['body']['model'] == 'real-model' for c in registry.calls)
    with store.read() as (session, _):
        attempts = list(session.scalars(select(RequestAttempt).order_by(RequestAttempt.started_at)))
        assert [json.loads(x.selection_data)['priority'] for x in attempts] == [10, 5]
        log = session.scalar(select(RequestLog))
        assert log.authorized_model == 'main-auto' and log.model == 'real-model'
    current = store.acquire(token, 'main-auto', conversation='route-change')
    store.fail_attempt(current, UpstreamError('network', 502, True))
    config.expected_version = 1
    save(store, 'WorkBuddy 国内', config)
    with pytest.raises(DomainError) as caught:
        store.acquire(token, 'main-auto', request_id=current['id'], excluded=[current['account_id']])
    assert caught.value.code == 'request_route_changed'
    store.end_request(current['id'], 'failed', 'request_route_changed')
    assert_released(store)


def test_model_directory_sql_page_version_and_cross_platform_group_rejection(store):
    token, ids = seed(store, 1)
    _, foreign = seed(store, 1, platform='Loomy')
    client = api(store, ScriptedRegistry([]))
    client.headers['x-console-client'] = 'unified-local'
    body = {'expected_version': 0, 'groups': [{'id': 'x', 'name': 'x', 'account_ids': foreign}], 'mappings': []}
    denied = client.put('/api/model-routes/WorkBuddy 国内', json=body)
    assert denied.status_code == 422
    body['groups'][0]['account_ids'] = ids
    assert client.put('/api/model-routes/WorkBuddy 国内', json=body).status_code == 200
    assert client.put('/api/model-routes/WorkBuddy 国内', json=body).status_code == 409
    store.resource_put('model_catalog', 'WorkBuddy 国内', {'models': [f'model-{i:03}' for i in range(123)], 'observed_at': store.clock()})
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(store.engine, 'before_cursor_execute', capture)
    try:
        result = client.get('/api/model-directory', params={'platform': 'WorkBuddy 国内', 'q': 'model-0', 'page': 3, 'page_size': 10}).json()
    finally:
        event.remove(store.engine, 'before_cursor_execute', capture)
    assert result['total'] == 100 and len(result['items']) == 10
    assert result['items'][0]['id'] == 'model-020'
    assert any('forward_model_catalog' in sql and 'LIMIT' in sql.upper() and 'OFFSET' in sql.upper() for sql in statements)


@pytest.mark.parametrize('business,want', [('6004', 'rate_limit'), ('6020', 'upstream_queue_wait'), ('11102', 'permission'), ('11140', 'permission')])
def test_http200_structured_codes_and_no_body_text_classification(business, want):
    assert frame_failure({'data': {'error': {'code': business}}}).code == want
    assert frame_failure(d(content='error 6004 11140 rate limit')) is None
    assert frame_failure({'error': {'code': '11140', 'message': 'rate-limiting'}}).code == 'rate_limit'


@pytest.mark.parametrize('flow,want,calls', [([d(content='partial')], 'truncated_stream', 1), ([{'_done': True}], 'no_effective_output', 2), ([{'error': {'code': '6004'}}], 'rate_limit', 2)])
def test_stream_errors_are_failed_and_effective_output_not_replayed(store, flow, want, calls):
    token, _ = seed(store)
    registry = ScriptedRegistry([flow, flow])
    response = api(store, registry).post('/v1/chat/completions', headers=headers(token), json={'model': 'm', 'messages': [{'role': 'user', 'content': 'q'}], 'stream': True})
    rows = events(response)
    assert rows[-1]['error']['code'] == want and len(registry.calls) == calls
    assert_released(store, 'failed')


def test_one_startup_budget_and_long_valid_output_not_cut_by_it(store):
    token, _ = seed(store)
    async def run():
        gateway = Gateway(store, ScriptedRegistry([[.18, UpstreamError('network', 502, True)], [.18, d(content='too late')]]), startup=.25, placeholder_enabled=False)
        current = await gateway.acquire(token, 'm')
        started = time.monotonic()
        result = [x async for x in gateway.frames(token, {'model': 'm', 'messages': [{'role': 'user', 'content': 'q'}]}, current)]
        assert result[-1]['error']['code'] == 'startup_timeout'
        assert time.monotonic()-started < .8
        gateway = Gateway(store, ScriptedRegistry([[d(content='early'), .2, d(content='late'), done()]]), startup=.08, idle=.5, placeholder_enabled=False)
        current = await gateway.acquire(token, 'm')
        result = [x async for x in gateway.frames(token, {'model': 'm', 'messages': [{'role': 'user', 'content': 'q'}]}, current)]
        assert result[-1]['finish'] == 'stop' and not any('error' in x for x in result)
    asyncio.run(run())
    assert_released(store)


def test_cancel_silent_upstream_releases_slot_and_closes_transport(store):
    token, _ = seed(store, 1)
    async def run():
        registry = ScriptedRegistry([[30.0]])
        registry.entered = asyncio.Event()
        gateway = Gateway(store, registry, placeholder_enabled=False)
        current = await gateway.acquire(token, 'm')
        async def consume():
            async with aclosing(gateway.frames(token, {'model': 'm', 'messages': [{'role': 'user', 'content': 'q'}]}, current)) as source:
                return [x async for x in source]
        pending = asyncio.create_task(consume())
        await asyncio.wait_for(registry.entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert registry.closed == 1
    asyncio.run(run())
    assert_released(store, 'cancelled')


def test_wps_native_preserves_system_history_tools_cache_and_rejects_inner_marker():
    source = {'model': 'm', 'prompt_cache_key': 'stable', 'prompt_cache_retention': '24h', 'messages': [
        {'role': 'system', 'content': '  first\n'}, {'role': 'user', 'content': 'q'},
        {'role': 'system', 'content': '\nlast  '}, {'role': 'assistant', 'content': None, 'reasoning_content': 'trace', 'tool_calls': [{'id': 'c', 'type': 'function', 'function': {'name': 'f', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c', 'content': [{'type': 'text', 'text': 'first'}, {'type': 'text', 'text': 'last', 'cache_control': {'type': 'ephemeral'}}]}]}
    output = wps_body(source)
    assert output['messages'][:3] == source['messages'][:3]
    assert output['messages'][3]['reasoning_content'] == 'trace'
    assert output['messages'][4]['content'] == 'firstlast'
    assert output['messages'][4]['cache_control'] == {'type': 'ephemeral'}
    assert output['prompt_cache_key'] == 'stable' and output['prompt_cache_retention'] == '24h'
    source['messages'][4]['content'][0]['cache_control'] = {'type': 'ephemeral'}
    with pytest.raises(UpstreamError, match='unsupported_protocol_field'):
        wps_body(source)


def test_wps_cross_instance_claim_single_creation_restart_and_expired_publish(store):
    other = Store(store.engine.url.render_as_string(hide_password=False), store.test_key, clock=store.clock)
    caches = [EncryptedSessionCache(store), EncryptedSessionCache(other)]
    count = []
    async def run():
        async def create():
            count.append(1)
            await asyncio.sleep(.04)
            return {'session_id': 'sid', 'run_id': 'rid', 'created': time.time()}
        adapters = [SimpleNamespace(cache={}, shared_cache=c) for c in caches]
        result = await asyncio.gather(*(resolve(a, ('a', 1, 'm', 'c'), 'a'*64, create) for a in adapters))
        assert result[0] == result[1] and len(count) == 1
        assert await resolve(SimpleNamespace(cache={}, shared_cache=caches[1]), (), 'a'*64, create) == result[0]
        assert len(count) == 1
    try:
        asyncio.run(run())
        assert caches[0].claim('b'*64, ttl_seconds=1)
        claim = caches[0].claim('c'*64, ttl_seconds=1)
        store.test_now[0] += 2
        assert caches[1].publish('c'*64, claim, {'session_id': 'late'}, ttl_seconds=3600) is False
        with store.read() as (session, _):
            assert not list(session.scalars(select(Resource).where(Resource.kind == 'private_session_claim', Resource.name == 'a'*64)))
            row = session.scalar(select(Resource).where(Resource.kind == 'private_session_cache', Resource.name == 'a'*64))
            assert 'sid' not in row.secret
    finally:
        other.engine.dispose()


def test_wps_cancel_claim_releases_even_when_db_claim_commits_late(store):
    cache = EncryptedSessionCache(store)
    original = cache.claim
    claimed = []
    started = threading.Event()
    def slow(*args, **kwargs):
        started.set()
        time.sleep(.05)
        token = original(*args, **kwargs)
        claimed.append(token)
        return token
    cache.claim = slow
    async def run():
        async def create():
            raise AssertionError('cancelled creator must not issue HTTP')
        pending = asyncio.create_task(resolve(SimpleNamespace(cache={}, shared_cache=cache), (), 'd'*64, create))
        assert await asyncio.to_thread(started.wait, 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert claimed and claimed[0]
    asyncio.run(run())
    with store.read() as (session, _):
        assert not list(session.scalars(select(Resource).where(Resource.kind == 'private_session_claim', Resource.name == 'd'*64)))


def test_platform_header_mismatch_and_affinity_cannot_escape_changed_group(store):
    token, ids = seed(store)
    registry = ScriptedRegistry([answer(), answer()])
    client = api(store, registry)
    payload = {'model': 'm', 'conversation_id': 'stable', 'messages': [{'role': 'user', 'content': 'q'}]}
    denied = client.post('/v1/chat/completions', headers={**headers(token), 'X-Platform': 'Loomy'}, json=payload)
    assert denied.status_code == 422 and denied.json()['error']['code'] == 'platform_mismatch'
    assert not registry.calls
    assert client.post('/v1/chat/completions', headers=headers(token), json=payload).status_code == 200
    first = registry.calls[0]['identity']
    remaining = next(identity for identity in ids if identity != first)
    save(store, 'WorkBuddy 国内', RoutesInput(expected_version=0, groups=[{'id': 'new', 'name': 'New', 'account_ids': [remaining]}], mappings=[{'model': 'm', 'upstream_model': 'm', 'group_id': 'new'}]))
    assert client.post('/v1/chat/completions', headers=headers(token), json=payload).status_code == 200
    assert registry.calls[1]['identity'] == remaining
    with store.read() as (session, _):
        attempt = session.scalar(select(RequestAttempt).where(RequestAttempt.account_id == remaining))
        selection = json.loads(attempt.selection_data)
        assert selection['group_id'] == 'new' and selection['reason'] != 'conversation_affinity'
    assert_released(store, 'succeeded')


@pytest.mark.parametrize('extra', [
    {'thinking': {'type': 'enabled', 'budget_tokens': 1023}},
    {'thinking': {'type': 'enabled', 'budget_tokens': 2048}},
    {'thinking': {'type': 'disabled', 'budget_tokens': 1024}},
    {'messages': [{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'opaque', 'signature': 'external-signed-value'}]}, {'role': 'user', 'content': 'q'}]},
])
def test_invalid_thinking_contract_rejected_before_account_admission(store, extra):
    token, _ = seed(store)
    registry = ScriptedRegistry([])
    response = api(store, registry).post('/v1/messages', headers=headers(token), json={
        'model': 'm', 'max_tokens': 2048, 'messages': [{'role': 'user', 'content': 'q'}], **extra})
    assert response.status_code == 422 and response.json()['error']['code'] == 'unsupported_protocol_field'
    assert not registry.calls
    with store.read() as (session, _):
        assert session.scalar(select(func.count()).select_from(RequestLog)) == 0
    assert_released(store)


def test_stream_history_storage_failure_emits_error_and_failed_delivery(store, monkeypatch):
    import app.response_history as history
    token, _ = seed(store)
    def broken(*args, **kwargs):
        raise RuntimeError('private-fault-detail')
    monkeypatch.setattr(history, '_complete', broken)
    response = api(store, ScriptedRegistry([answer()])).post('/v1/responses', headers=headers(token), json={'model': 'm', 'input': 'q', 'stream': True})
    rows = events(response)
    assert response.status_code == 200 and rows[-1]['error']['code'] == 'response_history_unavailable'
    assert not any(row.get('type') == 'response.completed' for row in rows)
    assert 'private-fault-detail' not in response.text
    assert_released(store, 'failed')


@pytest.mark.parametrize('enabled', [False, True])
def test_responses_placeholder_switch_preserves_stored_terminal_text(store, enabled):
    token, _ = seed(store)
    client = api(store, ScriptedRegistry([answer('exact')]))
    client.app.state.gateway.placeholder_enabled = enabled
    response = client.post('/v1/responses', headers=headers(token), json={'model': 'm', 'input': 'q', 'stream': True})
    rows = events(response)
    deltas = [row['delta'] for row in rows if row.get('type') == 'response.output_text.delta']
    assert deltas == ([' ', 'exact'] if enabled else ['exact'])
    terminal = rows[-1]['response']
    assert terminal['output'][0]['content'][0]['text'] == 'exact'
    assert client.get('/v1/responses/'+terminal['id'], headers=headers(token)).json() == terminal
    with store.read() as (session, _):
        log = session.scalar(select(RequestLog))
        assert bool(log.placeholder_at) == enabled and log.first_content_at is not None
    assert_released(store, 'succeeded')


def test_wps_two_registries_prepare_only_one_native_session(store):
    other = Store(store.engine.url.render_as_string(hide_password=False), store.test_key, clock=store.clock)
    calls = []
    async def run():
        async def handler(request):
            calls.append(request.url.path)
            await asyncio.sleep(.02)
            return httpx.Response(200, json={'data': {'session_id': 'session-one', 'run_id': 'run-one'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            registries = [Registry(enabled=True, transport_factory=lambda **kwargs: Transport(client), session_cache=EncryptedSessionCache(s)) for s in (store, other)]
            adapters = [r.make('WPS 灵犀', {'cookie': 'synthetic'}, 'same-account', 1) for r in registries]
            body = {'model': 'm', 'messages': [{'role': 'system', 'content': '  exact\n'}, {'role': 'user', 'content': 'q'}], 'prompt_cache_key': 'stable'}
            prepared = await asyncio.gather(*(a.prepare(body, 'same-conversation') for a in adapters))
            assert calls.count(wps.SESSIONS_PATH) == 1 and calls.count(wps.RUNS_PATH) == 1
            for _, outbound_headers, outbound in prepared:
                assert outbound_headers['x-cc-session-id'] == 'session-one'
                assert outbound_headers['x-cc-run-id'] == 'run-one'
                assert outbound['messages'] == body['messages'] and outbound['prompt_cache_key'] == 'stable'
    try:
        asyncio.run(run())
    finally:
        other.engine.dispose()


def test_wps_late_creator_cannot_invalidate_winning_native_session(store):
    shared = EncryptedSessionCache(store)
    cache_id = hashlib.sha256(encode(('account', 1, 'm', 'conversation'))).hexdigest()
    winner = {'session_id': 'winning-session', 'run_id': 'winning-run', 'created': time.time()}
    async def run():
        async def handler(request):
            if request.url.path == wps.RUNS_PATH:
                store.test_now[0] += 91
                claim = await asyncio.to_thread(shared.claim, cache_id)
                assert claim and await asyncio.to_thread(shared.publish, cache_id, claim, winner)
            return httpx.Response(200, json={'data': {'session_id': 'stale-session', 'run_id': 'stale-run'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            registry = Registry(enabled=True, transport_factory=lambda **kwargs: Transport(client), session_cache=shared)
            adapter = registry.make('WPS 灵犀', {'cookie': 'synthetic'}, 'account', 1)
            with pytest.raises(UpstreamError, match='request_lease_lost'):
                async with aclosing(adapter.chat({'model': 'm', 'messages': [{'role': 'user', 'content': 'q'}]}, 'conversation')) as source:
                    await anext(source)
            assert shared.get(cache_id) == winner and not registry.wps_cache
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['network', 'permission', 'invalid_catalog'])
def test_model_sync_failure_preserves_observed_catalog_and_closes_transport(store, failure):
    _, ids = seed(store, 1)
    store.resource_put('model_catalog', 'WorkBuddy 国内', {'models': ['observed'], 'observed_at': store.clock()})
    before = store.resources('model_catalog')
    closed = []
    class Adapter:
        async def models(self):
            if failure == 'invalid_catalog':
                return []
            raise UpstreamError(failure, 503 if failure == 'network' else 403)
        async def close(self):
            closed.append(True)
    adapter = Adapter()
    adapter.t = adapter
    registry = SimpleNamespace(enabled=True, make=lambda *args: adapter)
    client = api(store, registry)
    response = client.post('/api/accounts/'+ids[0]+'/models/sync', headers={'x-console-client': 'unified-local'})
    assert response.status_code == (503 if failure == 'network' else 403 if failure == 'permission' else 502)
    assert response.json()['error']['code'] == ('invalid_model_catalog' if failure == 'invalid_catalog' else failure)
    assert store.resources('model_catalog') == before and closed == [True]
    assert client.get('/api/model-directory', params={'platform': 'WorkBuddy 国内'}).json()['items'][0]['id'] == 'observed'


def test_chat_empty_tool_array_does_not_split_text_into_tool_events(store):
    token, _ = seed(store)
    registry = ScriptedRegistry([[d(content='first', tool_calls=[]), d(content='second', tool_calls=[]), done()]])
    response = api(store, registry).post('/v1/chat/completions', headers=headers(token), json={'model': 'm', 'messages': [{'role': 'user', 'content': 'q'}], 'stream': True})
    deltas = [row['choices'][0]['delta'] for row in events(response) if row.get('choices')]
    assert not any('tool_calls' in delta for delta in deltas)
    assert ''.join(delta.get('content', '') for delta in deltas).strip() == 'firstsecond'
    assert_released(store, 'succeeded')


def test_explicit_thinking_on_unsupported_platform_fails_without_upstream_http(store):
    token, _ = seed(store, 1, platform='Loomy')
    closed = []
    class NoHTTPTransport:
        async def close(self):
            closed.append(True)
    registry = Registry(enabled=True, transport_factory=lambda **kwargs: NoHTTPTransport())
    response = api(store, registry).post('/v1/messages', headers=headers(token), json={
        'model': 'm', 'messages': [{'role': 'user', 'content': 'q'}], 'max_tokens': 2048, 'thinking': {'type': 'disabled'}})
    assert response.status_code == 422 and response.json()['error']['code'] == 'unsupported_protocol_field'
    assert closed == [True]
    assert_released(store, 'failed')


def test_workbuddy_explicit_thinking_native_switch_does_not_auto_enable_or_reduce_budget():
    body = {'model': 'deepseek-test', 'messages': [{'role': 'user', 'content': 'q'}]}
    assert 'thinking' not in WorkBuddy.outbound(body) and 'reasoning_effort' not in WorkBuddy.outbound(body)
    enabled = WorkBuddy.outbound({**body, 'thinking': {'type': 'enabled', 'budget_tokens': 4096, 'display': 'omitted'}})
    assert enabled['thinking'] == {'type': 'enabled', 'budget_tokens': 4096} and enabled['reasoning_effort'] == 'high'
    disabled = WorkBuddy.outbound({**body, 'thinking': {'type': 'disabled'}, 'reasoning_effort': 'high'})
    assert disabled['thinking'] == {'type': 'disabled'} and 'reasoning_effort' not in disabled
    explicit = WorkBuddy.outbound({**body, 'thinking': {'type': 'adaptive'}, 'reasoning_effort': 'low'})
    assert explicit['thinking'] == {'type': 'enabled'} and explicit['reasoning_effort'] == 'low'


def test_wps_run_setup_text_is_exact_and_invalid_cache_boundary_does_not_create_session():
    async def run():
        calls = []
        def handler(request):
            calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={'data': {'session_id': 's', 'run_id': 'r'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WPS(Transport(client), {'cookie': 'synthetic'}, 'account', 1)
            body = {'model': 'm', 'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '  first\n', 'cache_control': {'type': 'ephemeral'}},
                {'type': 'text', 'text': 'last  '}]}]}
            with pytest.raises(UpstreamError, match='unsupported_protocol_field'):
                await adapter.prepare(body, 'invalid')
            assert not calls and not adapter.cache
            del body['messages'][0]['content'][0]['cache_control']
            _, _, outgoing = await adapter.prepare(body, 'valid')
            run = next(payload for path, payload in calls if path == wps.RUNS_PATH)
            assert run['agent_input']['text'] == outgoing['messages'][0]['content'] == '  first\nlast  '
            assert len(calls) == 2
    asyncio.run(run())


def test_route_group_member_directory_sql_page_identity_search_and_platform_scope(store):
    ids = [f'group-account-{i:04}' for i in range(1001)]
    with store.tx() as (session, _):
        for i, account_id in enumerate(ids):
            session.add(Account(id=account_id, platform='WorkBuddy 国内', external_id=f'private-original-identity-{i:04}',
                                label='同名账户', original_label='官方原名' if i == 30 else None,
                                created_at=i, updated_at=store.clock()))
        session.add(Account(id='foreign-member', platform='Loomy', external_id='private-foreign-identity',
                            label='同名账户', created_at=0, updated_at=store.clock()))
    save(store, 'WorkBuddy 国内', RoutesInput(expected_version=0, groups=[{'id': 'many', 'name': 'Many', 'account_ids': ids}], mappings=[]))
    # A stale/corrupt configuration must still never expose another platform.
    config = store.resources('model_routes')[0]
    body = config['data']
    body['groups'][0]['account_ids'].append('foreign-member')
    store.resource_put('model_routes', 'WorkBuddy 国内', body, resource_id=config['id'], expected_version=config['version'])
    client = api(store, ScriptedRegistry([]))
    url = '/api/model-routes/WorkBuddy 国内/groups/many/accounts'
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(store.engine, 'before_cursor_execute', capture)
    try:
        response = client.get(url, params={'page': 2, 'page_size': 25})
    finally:
        event.remove(store.engine, 'before_cursor_execute', capture)
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result['total'], result['page'], result['page_size']) == (1001, 2, 25)
    assert len(result['items']) == 25 and result['items'][0]['id'] == ids[25]
    assert all(set(row) == {'id', 'platform', 'display_name', 'identity_hint'} for row in result['items'])
    assert 'private-original-identity' not in response.text and 'private-foreign-identity' not in response.text
    assert any('accounts' in sql and 'LIMIT' in sql.upper() and 'OFFSET' in sql.upper() for sql in statements)
    assert client.get(url, params={'q': '官方原名'}).json()['items'][0]['id'] == ids[30]
    exact = client.get(url, params={'q': 'private-original-identity-0030'}).json()
    assert exact['total'] == 1 and exact['items'][0]['display_name'] == '官方原名'
    assert client.get(url, params={'page': 99}).json()['items'] == []


def test_route_group_member_directory_empty_missing_and_literal_search(store):
    _, ids = seed(store, 2)
    with store.tx() as (session, _):
        store.account(session, ids[0]).label = 'literal%_name'
        store.account(session, ids[1]).label = 'literalZZname'
    save(store, 'WorkBuddy 国内', RoutesInput(expected_version=0, groups=[
        {'id': 'literal', 'name': 'Literal', 'account_ids': ids}, {'id': 'empty', 'name': 'Empty'}], mappings=[]))
    client = api(store, ScriptedRegistry([]))
    base = '/api/model-routes/WorkBuddy 国内/groups/'
    result = client.get(base+'literal/accounts', params={'q': '%_'}).json()
    assert result['total'] == 1 and result['items'][0]['id'] == ids[0]
    assert client.get(base+'empty/accounts').json() == {'items': [], 'total': 0, 'page': 1, 'page_size': 25}
    missing = client.get(base+'missing/accounts')
    assert missing.status_code == 404 and missing.json()['error']['code'] == 'model_route_group_not_found'
    assert client.get('/api/model-routes/unknown/groups/literal/accounts').status_code == 422
    assert client.get(base+'literal/accounts', params={'page': 0}).status_code == 422
    assert client.get(base+'literal/accounts', params={'page_size': 201}).status_code == 422


def test_native_business_codes_do_not_expand_into_unobserved_http_statuses():
    for status, code, message, expected in [
        (400, '6004', None, 'upstream_rejected'), (403, '6004', None, 'unknown_403'),
        (400, '6020', None, 'upstream_rejected'), (403, '6020', None, 'unknown_403'),
        (400, '11140', 'rate-limiting', 'upstream_rejected'),
        (403, '11140', None, 'permission'), (403, '11140', 'rate-limiting', 'rate_limit'),
    ]:
        assert classify(status, code, message=message).code == expected


def test_gateway_client_source_is_stored_once_and_preserved_across_retry(store):
    token, _ = seed(store)
    registry = ScriptedRegistry([[{'error': {'code': '6004'}}], answer()])
    async def run():
        gateway = Gateway(store, registry)
        current = await gateway.acquire(token, 'm', client_source='codex')
        frames = [frame async for frame in gateway.frames(token, {'model': 'm', 'messages': [{'role': 'user', 'content': 'q'}]}, current)]
        assert frames[-1]['finish'] == 'stop'
    asyncio.run(run())
    with store.read() as (session, _):
        assert session.scalar(select(RequestLog)).client_source == 'codex'
        assert session.scalar(select(func.count()).select_from(RequestAttempt)) == 2
    assert_released(store, 'succeeded')
