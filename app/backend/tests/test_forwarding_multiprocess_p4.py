"""P4 gaps: spawned OS processes + isolated PostgreSQL, no live upstreams."""
import asyncio
import hashlib
import json
import multiprocessing
import os
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.adapters import Registry, WorkBuddy
from app.encrypted_session_cache import EncryptedSessionCache
from app.models import Lease, Resource
from app.protocols import normalize
from app.store import Store
from app.upstream import Transport
from app.vendor.mirai.wps_lingxi import endpoints as wps
from test_rebuild_forwarding_acceptance import ScriptedRegistry, api, d, done, events, headers, seed


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def deny_child_http():
    # pytest monkeypatches do not survive multiprocessing's fresh interpreter.
    def denied(*args, **kwargs):
        raise AssertionError('External HTTP forbidden in isolated child')
    async def denied_async(*args, **kwargs):
        denied()
    httpx.HTTPTransport.handle_request = denied
    httpx.AsyncHTTPTransport.handle_async_request = denied_async
    os.environ['UNIFIED_ENABLE_UPSTREAM'] = '0'
    os.environ['UNIFIED_SECURE_COOKIE'] = '0'
    os.environ['UNIFIED_ROLE'] = 'api'


@pytest.fixture
def p4_store(tmp_path):
    key = Fernet.generate_key()
    store = Store(f'sqlite:///{tmp_path / "p4.db"}', key)
    store.test_key = key
    store.real_capabilities = {'WorkBuddy 国内': {'chat', 'balance'}}
    yield store
    store.engine.dispose()


def pg_only(store):
    if store.engine.dialect.name != 'postgresql':
        pytest.skip('Requires real PostgreSQL across spawned OS processes')


def stop_children(children):
    for child in children:
        if child.is_alive():
            child.terminate()
    for child in children:
        child.join(timeout=5)


def results_from(output, count, phase='result'):
    rows = [output.get(timeout=30) for _ in range(count)]
    assert all(row['phase'] == phase for row in rows), rows
    return rows


def wps_process(url, key, clock, start, output, counts, mode='normal'):
    deny_child_http()
    store = None
    try:
        store = Store(url, key, clock=lambda: clock)
        async def run():
            async def handler(request):
                path = request.url.path
                with counts.get_lock():
                    if path == wps.SESSIONS_PATH:
                        counts[0] += 1
                    elif path == wps.RUNS_PATH:
                        counts[1] += 1
                    else:
                        raise AssertionError('Unexpected native HTTP path')
                if mode == 'crash' and path == wps.SESSIONS_PATH:
                    output.put({'phase': 'creating', 'pid': os.getpid()})
                    await asyncio.sleep(600)
                await asyncio.sleep(.08)
                return httpx.Response(200, json={'data': {'session_id': 'shared-session', 'run_id': 'shared-run'}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                registry = Registry(enabled=True, transport_factory=lambda **kwargs: Transport(client), session_cache=EncryptedSessionCache(store))
                adapter = registry.make('WPS 灵犀', {'cookie': 'synthetic'}, 'process-account', 1)
                output.put({'phase': 'ready', 'pid': os.getpid()})
                assert await asyncio.to_thread(start.wait, 20)
                _, wire_headers, outgoing = await adapter.prepare({'model': 'm', 'messages': [
                    {'role': 'system', 'content': '  unchanged\n'}, {'role': 'user', 'content': 'q'}], 'prompt_cache_key': 'stable'}, 'conversation')
                output.put({'phase': 'result', 'pid': os.getpid(), 'session': wire_headers['x-cc-session-id'],
                            'run': wire_headers['x-cc-run-id'], 'body_hash': digest(outgoing)})
        asyncio.run(run())
    except BaseException as failure:
        output.put({'phase': 'error', 'pid': os.getpid(), 'type': type(failure).__name__, 'code': getattr(failure, 'code', None)})
        raise
    finally:
        if store is not None:
            store.engine.dispose()


def spawn_wps(ctx, store, clock, start, output, counts, mode='normal'):
    child = ctx.Process(target=wps_process, args=(store.engine.url.render_as_string(hide_password=False), store.test_key, clock, start, output, counts, mode))
    child.start()
    return child


def test_wps_four_os_processes_share_native_setup_and_fifth_reuses_after_exit(p4_store):
    pg_only(p4_store)
    ctx = multiprocessing.get_context('spawn')
    start, output, counts = ctx.Event(), ctx.Queue(), ctx.Array('i', [0, 0])
    children = []
    try:
        children = [spawn_wps(ctx, p4_store, time.time(), start, output, counts) for _ in range(4)]
        ready = results_from(output, 4, 'ready')
        assert len({row['pid'] for row in ready}) == 4
        start.set()
        result = results_from(output, 4)
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
        assert list(counts) == [1, 1]
        assert {(r['session'], r['run']) for r in result} == {('shared-session', 'shared-run')}
        assert len({r['body_hash'] for r in result}) == 1
        # All original Python processes are gone; this process has no local cache.
        restarted = spawn_wps(ctx, p4_store, time.time(), start, output, counts)
        children.append(restarted)
        results_from(output, 1, 'ready')
        again = results_from(output, 1)[0]
        restarted.join(timeout=10)
        assert restarted.exitcode == 0 and list(counts) == [1, 1]
        assert (again['session'], again['run'], again['body_hash']) == (result[0]['session'], result[0]['run'], result[0]['body_hash'])
        with p4_store.read() as (session, _):
            assert session.scalar(select(func.count()).select_from(Resource).where(Resource.kind == 'private_session_claim')) == 0
            rows = list(session.scalars(select(Resource).where(Resource.kind == 'private_session_cache')))
            assert len(rows) == 1 and 'shared-session' not in rows[0].secret
    finally:
        stop_children(children)
        output.close()


def test_wps_killed_creator_claim_recovers_after_expiry_in_new_process(p4_store):
    pg_only(p4_store)
    ctx = multiprocessing.get_context('spawn')
    start, output, counts = ctx.Event(), ctx.Queue(), ctx.Array('i', [0, 0])
    clock = time.time()
    children = []
    try:
        crashed = spawn_wps(ctx, p4_store, clock, start, output, counts, 'crash')
        children.append(crashed)
        results_from(output, 1, 'ready')
        start.set()
        results_from(output, 1, 'creating')
        crashed.terminate()
        crashed.join(timeout=10)
        assert crashed.exitcode != 0 and list(counts) == [1, 0]
        with p4_store.read() as (session, _):
            assert session.scalar(select(func.count()).select_from(Resource).where(Resource.kind == 'private_session_claim')) == 1
            assert session.scalar(select(func.count()).select_from(Resource).where(Resource.kind == 'private_session_cache')) == 0
        # Advance the injected application clock; do not sleep for the 90s TTL.
        recovered = spawn_wps(ctx, p4_store, clock+91, start, output, counts)
        children.append(recovered)
        results_from(output, 1, 'ready')
        result = results_from(output, 1)[0]
        recovered.join(timeout=10)
        assert recovered.exitcode == 0 and result['session'] == 'shared-session'
        assert list(counts) == [2, 1]
        with p4_store.read() as (session, _):
            assert session.scalar(select(func.count()).select_from(Resource).where(Resource.kind == 'private_session_claim')) == 0
            assert session.scalar(select(func.count()).select_from(Resource).where(Resource.kind == 'private_session_cache')) == 1
    finally:
        stop_children(children)
        output.close()


def long_messages():
    messages = [{'role': 'system', 'content': '  first system\n'}]
    for i in range(120):
        messages += [{'role': 'user', 'content': f'  user {i:03} ' + '上下文 xyz\n'*100},
                     {'role': 'assistant', 'content': f'assistant {i:03}\n' + 'retained '*35}]
        if i == 59:
            messages.append({'role': 'system', 'content': '\n system stays in the middle  '})
    messages += [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'prior-call', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"x":1}'}}]},
        {'role': 'tool', 'tool_call_id': 'prior-call', 'content': '  tool value\n'},
        {'role': 'user', 'content': '  last request\n'}]
    return messages


def text_of(content):
    if isinstance(content, list):
        return ''.join(part['text'] for part in content)
    return content


def semantic_messages(messages):
    return [{**m, 'content': text_of(m.get('content'))} for m in messages]


def long_payload(protocol, stream):
    canonical = long_messages()
    if protocol == 'chat':
        raw = {'messages': canonical}
    elif protocol == 'responses':
        items = []
        for message in canonical:
            if message.get('tool_calls'):
                call = message['tool_calls'][0]
                items.append({'type': 'function_call', 'call_id': call['id'], **call['function']})
            elif message['role'] == 'tool':
                items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message['content']})
            else:
                items.append(message)
        raw = {'input': items}
    else:
        items = []
        for message in canonical:
            if message.get('tool_calls'):
                call = message['tool_calls'][0]
                items.append({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': {'x': 1}}]})
            elif message['role'] == 'tool':
                items.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': message['content']}]})
            else:
                items.append(message)
        raw = {'messages': items, 'max_tokens': 4096}
    return {'model': 'm', 'stream': stream, 'prompt_cache_key': 'long-stable-key', 'prompt_cache_retention': '24h', **raw}


@pytest.mark.parametrize('protocol,stream', [(p, s) for p in ('chat', 'responses', 'messages') for s in (False, True)])
def test_long_context_three_protocols_keep_exact_order_text_tools_and_cache(p4_store, protocol, stream):
    token, _ = seed(p4_store)
    registry = ScriptedRegistry([[d(reasoning_content='reasoning'), d(content='answer'), done()]])
    path = {'chat': '/v1/chat/completions', 'responses': '/v1/responses', 'messages': '/v1/messages'}[protocol]
    response = api(p4_store, registry).post(path, headers=headers(token), json=long_payload(protocol, stream))
    assert response.status_code == 200, response.text[:200]
    if stream:
        rows = events(response)
        assert not any('error' in row for row in rows)
        assert ('[DONE]' in response.text if protocol == 'chat' else rows[-1]['type'] == ('response.completed' if protocol == 'responses' else 'message_stop'))
    body = registry.calls[0]['body']
    expected = semantic_messages(long_messages())
    actual = semantic_messages(body['messages'])
    # Messages tool-use input serializes object whitespace; compare its value.
    for messages in (actual, expected):
        for message in messages:
            for call in message.get('tool_calls', []):
                call['function']['arguments'] = json.loads(call['function']['arguments'])
    assert actual == expected
    native = WorkBuddy.outbound(body)
    assert semantic_messages(native['messages']) == actual
    assert native['prompt_cache_key'] == 'long-stable-key' and native['prompt_cache_retention'] == '24h'
    with p4_store.read() as (session, _):
        assert session.scalar(select(func.count()).select_from(Lease)) == 0


def history_process(url, key, token, output, phase, previous=None):
    deny_child_http()
    store = None
    try:
        store = Store(url, key)
        store.real_capabilities = {'WorkBuddy 国内': {'chat', 'balance'}}
        if phase == 'create':
            registry = ScriptedRegistry([[d(tool_calls=[{'index': 0, 'id': 'continue-call', 'function': {'name': 'next', 'arguments': '{}'}}]), done('tool_calls')]])
            payload = long_payload('responses', True)
            payload['instructions'] = 'instruction-for-first-turn-only'
            response = api(store, registry).post('/v1/responses', headers=headers(token), json=payload)
            assert response.status_code == 200
            terminal = events(response)[-1]['response']
            output.put({'phase': 'result', 'pid': os.getpid(), 'id': terminal['id'], 'response_hash': digest(terminal), 'session': registry.calls[0]['session']})
        elif phase == 'resume':
            registry = ScriptedRegistry([[d(content='continued'), done()]])
            client = api(store, registry)
            retrieved = client.get('/v1/responses/'+previous['id'], headers=headers(token))
            assert retrieved.status_code == 200 and digest(retrieved.json()) == previous['response_hash']
            response = client.post('/v1/responses', headers=headers(token), json={'model': 'm', 'previous_response_id': previous['id'], 'instructions': 'current-instruction',
                'input': [{'type': 'function_call_output', 'call_id': 'continue-call', 'output': 'continuation value'}]})
            assert response.status_code == 200
            messages = registry.calls[0]['body']['messages']
            assert messages[-2]['content'] == 'current-instruction'
            assert not any(m.get('content') == 'instruction-for-first-turn-only' for m in messages)
            inherited = semantic_messages(messages[:-3])
            assert inherited == semantic_messages(normalize(long_payload('responses', False), 'responses')['messages'])
            assert messages[-3]['tool_calls'][0]['id'] == 'continue-call'
            assert messages[-1]['tool_call_id'] == 'continue-call'
            assert registry.calls[0]['session'] == previous['session']
            assert client.delete('/v1/responses/'+previous['id'], headers=headers(token)).status_code == 200
            output.put({'phase': 'result', 'pid': os.getpid(), 'id': previous['id'], 'new_id': response.json()['id'], 'session': registry.calls[0]['session']})
        else:
            client = api(store, ScriptedRegistry([]))
            assert client.get('/v1/responses/'+previous['id'], headers=headers(token)).status_code == 404
            assert client.get('/v1/responses/'+previous['new_id'], headers=headers(token)).status_code == 200
            output.put({'phase': 'result', 'pid': os.getpid()})
    except BaseException as failure:
        output.put({'phase': 'error', 'pid': os.getpid(), 'type': type(failure).__name__, 'code': getattr(failure, 'code', None)})
        raise
    finally:
        if store is not None:
            store.engine.dispose()


def test_long_responses_history_survives_three_sequential_os_processes(p4_store):
    pg_only(p4_store)
    token, _ = seed(p4_store)
    ctx = multiprocessing.get_context('spawn')
    output, children = ctx.Queue(), []
    previous = None
    try:
        for phase in ('create', 'resume', 'check-deleted'):
            child = ctx.Process(target=history_process, args=(p4_store.engine.url.render_as_string(hide_password=False), p4_store.test_key, token, output, phase, previous))
            child.start()
            children.append(child)
            previous = results_from(output, 1)[0]
            child.join(timeout=10)
            assert child.exitcode == 0
        with p4_store.read() as (session, _):
            assert session.scalar(select(func.count()).select_from(Lease)) == 0
            rows = list(session.scalars(select(Resource).where(Resource.kind == 'response_history')))
            assert len(rows) == 1 and rows[0].data == '{}'
            assert 'continuation value' not in rows[0].secret and 'first system' not in rows[0].secret
    finally:
        stop_children(children)
        output.close()
