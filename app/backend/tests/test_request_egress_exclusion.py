import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.gateway import Gateway
from app.models import Account, Lease, RequestAttempt
from app.proxy_pool import accounts_on_fixed_endpoint, record_proxy_result, RouteInput, save_route
from app.store import Store
from app.upstream import UpstreamError
from test_gateway import FakeRegistry, delta, real_accounts


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'egress.db'}", Fernet.generate_key())
    token = real_accounts(store, 4)
    with store.read() as (s, _):
        ids = list(s.scalars(select(Account.id).order_by(Account.id)))
    endpoints = ['http://synthetic:secret@bad:8080', 'http://synthetic:secret@bad:8080', 'http://good:8080']
    nodes = [store.resource_put('proxy', str(i), {'status': 'untested'}, secret=e) for i, e in enumerate(endpoints)]
    for index, aid in enumerate(ids):
        store.bind_proxy(aid, nodes[2] if index == 3 else nodes[index % 2])
    yield store, token, ids, nodes, endpoints
    store.engine.dispose()


def run(store, token, reg, first_excluded=()):
    async def invoke():
        gateway = Gateway(store, reg)
        current = await gateway.acquire(token, 'model', first_excluded)
        return [item async for item in gateway.frames(token, {'model': 'model', 'messages': []}, current)]
    return asyncio.run(invoke())


@pytest.mark.parametrize('phase', ['connect', 'proxy_connect'])
def test_connection_failure_skips_same_fixed_endpoint_without_rebinding(env, phase):
    store, token, ids, nodes, endpoints = env
    original = store.resources('proxy_binding')
    reg = FakeRegistry([[UpstreamError('upstream_timeout', 504, True, failure_phase=phase)], [delta('answer'), {'_done': True}]])
    result = run(store, token, reg, [ids[3]])
    assert len(reg.calls) == 2 and reg.calls[-1] == ids[3]
    assert result[0]['placeholder'] and result[-1]['finish'] == 'stop'
    assert store.resources('proxy_binding') == original
    with store.read() as (s, _):
        assert all(a.enabled for a in s.scalars(select(Account)))
        assert list(s.scalars(select(Lease))) == []
        assert len(list(s.scalars(select(RequestAttempt)))) == 2


def test_all_accounts_on_fixed_failed_connection_return_original_error_without_cross_request_ban(env):
    store, token, ids, nodes, endpoints = env
    store.bind_proxy(ids[3], nodes[0])
    reg = FakeRegistry([[UpstreamError('network', 502, True, failure_phase='connect')]])
    result = run(store, token, reg)
    assert len(reg.calls) == 1 and result[-1]['error']['code'] == 'network'
    # A new request still follows the fixed binding; it was never disabled.
    next_reg = FakeRegistry([[delta('recovered'), {'_done': True}]])
    assert run(store, token, next_reg)[-1]['finish'] == 'stop'


@pytest.mark.parametrize('code,phase', [('upstream_5xx','http_status'), ('upstream_timeout','read'), ('network','read'), ('upstream_timeout','local_pool'), ('upstream_timeout','operation_timeout')])
def test_nonconnection_failure_does_not_skip_same_proxy_or_mark_proxy_failed(env, code, phase):
    store, token, ids, nodes, endpoints = env
    store.bind_proxy(ids[3], nodes[0])
    reg = FakeRegistry([[UpstreamError(code, 502, True, failure_phase=phase)], [delta('answer'), {'_done': True}]])
    assert run(store, token, reg)[-1]['finish'] == 'stop'
    assert len(reg.calls) == 2
    # A successful final model records connectivity, never a failure from 5xx/read.
    assert all(r['data']['consecutive_transport_failures'] == 0 for r in store.resources('proxy_health'))


def test_no_retry_after_effective_output_even_for_connection_error(env):
    store, token, ids, nodes, endpoints = env
    reg = FakeRegistry([[delta('part'), UpstreamError('network',502,True,failure_phase='connect')]])
    result = run(store, token, reg)
    assert len(reg.calls) == 1 and result[-1]['error']['code'] == 'network'


def test_fixed_platform_route_excludes_only_matching_platform_and_preserves_overrides(env):
    store, token, ids, nodes, endpoints = env
    extra = store.create_account('Loomy','unbound','unbound')
    other = store.create_account('WPS 灵犀','other','other')
    store.bind_proxy(other,nodes[0])
    save_route(store,'Loomy',RouteInput(mode='fixed',proxy_id=nodes[0]))
    assert accounts_on_fixed_endpoint(store,'Loomy',endpoints[0]) == set(ids[:3]) | {extra}
    save_route(store,'Loomy',RouteInput(mode='direct'))
    assert accounts_on_fixed_endpoint(store,'Loomy',endpoints[0]) == set(ids[:3])


@pytest.mark.parametrize('code,phase', [('upstream_5xx','http_status'),('network','read'),('upstream_timeout','local_pool')])
def test_passive_health_does_not_turn_nonconnection_error_into_failed_exit(env, code, phase):
    store, token, ids, nodes, endpoints = env
    record_proxy_result(store,ids[0],endpoints[0],success=False,code=code,failure_phase=phase)
    assert store.resources('proxy_health') == []
