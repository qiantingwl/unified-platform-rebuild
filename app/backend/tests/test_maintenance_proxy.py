"""Task routes must not rewrite forwarding or registration-session egress."""
import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete

from app.maintenance_proxy import KINDS, RouteInput, mount_maintenance_proxy, resolve_maintenance_proxy, routes, save_route
from app.models import Job, Resource
from app.proxy_pool import PoolInput, record_proxy_result, resolve_account_proxy, save_pool
from app.store import DomainError, Store
from app.worker import MaintenanceWorker
from test_gateway import real_accounts
from test_invitations_queue import Registry as InvitationRegistry
from app.invitations import Invitations


@pytest.fixture
def data(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'tasks-proxy.db'}", Fernet.generate_key())
    real_accounts(store, 2)
    store.real_capabilities['Loomy'].update({'verify', 'invitation'})
    accounts = [a['id'] for a in store.snapshot()['accounts']]
    with store.tx() as (db, _):
        db.execute(delete(Job))
    nodes = [store.resource_put('proxy', f'node-{n}', {'status': 'untested'}, secret=f'http://proxy{n}:8080') for n in range(3)]
    pool = save_pool(store, PoolInput(name='tasks-only', proxy_ids=nodes[1:]))['id']
    for account in accounts:
        store.bind_proxy(account, nodes[0])
    yield store, accounts, nodes, pool
    store.engine.dispose()


def configure(store, kind, mode, **values):
    return save_route(store, 'Loomy', kind, RouteInput(expected_version=0, mode=mode, **values))


@pytest.mark.parametrize('kind', (*KINDS, 'verify'))
def test_default_inherit_preserves_account_fixed_route(data, kind):
    store, accounts, _, _ = data
    assert resolve_maintenance_proxy(store, accounts[0], kind) == 'http://proxy0:8080'
    assert store.resources('maintenance_proxy_route') == []


def test_task_override_does_not_change_forwarding_or_other_tasks(data):
    store, accounts, nodes, pool = data
    configure(store, 'balance', 'fixed', proxy_id=nodes[1])
    configure(store, 'checkin', 'direct')
    assert resolve_maintenance_proxy(store, accounts[0], 'balance') == 'http://proxy1:8080'
    assert resolve_maintenance_proxy(store, accounts[0], 'checkin') is None
    assert resolve_maintenance_proxy(store, accounts[0], 'renew') == 'http://proxy0:8080'
    assert resolve_maintenance_proxy(store, accounts[0], 'verify') == 'http://proxy0:8080'
    assert resolve_account_proxy(store, accounts[0]) == 'http://proxy0:8080'
    assert len(store.resources('proxy_binding')) == 2


def test_pool_sticky_health_failover_and_exhaustion_never_direct(data):
    store, accounts, _, pool = data
    configure(store, 'balance', 'pool', pool_id=pool)
    first = resolve_maintenance_proxy(store, accounts[0], 'balance')
    assert first != 'http://proxy0:8080'
    assert resolve_maintenance_proxy(store, accounts[0], 'balance') == first
    assert store.resources('proxy_affinity') == []
    record_proxy_result(store, accounts[0], first, success=False, code='network')
    second = resolve_maintenance_proxy(store, accounts[0], 'balance')
    assert second != first and second != 'http://proxy0:8080'
    record_proxy_result(store, accounts[0], second, success=False, code='network')
    with pytest.raises(DomainError, match='proxy_pool_unavailable'):
        resolve_maintenance_proxy(store, accounts[0], 'balance')
    assert resolve_account_proxy(store, accounts[0]) == 'http://proxy0:8080'


def test_retired_task_node_does_not_fall_back_to_forwarding(data):
    store, accounts, nodes, _ = data
    configure(store, 'balance', 'fixed', proxy_id=nodes[1])
    with store.tx() as (db, _):
        db.get(Resource, nodes[1]).data = json.dumps({'status': 'retired'})
    with pytest.raises(DomainError, match='proxy_retired'):
        resolve_maintenance_proxy(store, accounts[0], 'balance')


def test_cas_and_revert_to_inherit(data):
    store, accounts, nodes, _ = data
    configured = configure(store, 'balance', 'fixed', proxy_id=nodes[1])
    with pytest.raises(DomainError, match='resource_version_conflict'):
        configure(store, 'balance', 'direct')
    save_route(store, 'Loomy', 'balance', RouteInput(expected_version=configured['version'], mode='inherit'))
    assert resolve_maintenance_proxy(store, accounts[0], 'balance') == 'http://proxy0:8080'


@pytest.mark.parametrize('kind', ['verify', 'relogin', 'registration', 'not-a-task'])
def test_cannot_configure_model_probe_or_login_session_route(data, kind):
    with pytest.raises(DomainError, match='unsupported_maintenance_task'):
        configure(data[0], kind, 'direct')


@pytest.mark.parametrize('values', [
    {'mode': 'fixed'}, {'mode': 'pool'}, {'mode': 'direct', 'proxy_id': 'x'},
    {'mode': 'inherit', 'pool_id': 'x'}, {'mode': 'fixed', 'proxy_id': 'x', 'pool_id': 'p'},
])
def test_config_rejects_ambiguous_targets(values):
    with pytest.raises(ValidationError):
        RouteInput(expected_version=0, **values)


def test_corrupt_saved_route_fails_closed(data):
    store, accounts, _, _ = data
    store.resource_put('maintenance_proxy_route', 'Loomy:balance', {'mode': 'pool'})
    with pytest.raises(DomainError, match='maintenance_proxy_route_invalid'):
        resolve_maintenance_proxy(store, accounts[0], 'balance')


def test_api_uses_only_public_route_ids(data):
    store, _, nodes, _ = data
    app = FastAPI()
    mount_maintenance_proxy(app, store)
    with TestClient(app) as client:
        initial = client.get('/api/maintenance-proxy-routes').json()
        assert initial['verify_uses_forwarding'] and all(r['mode'] == 'inherit' for r in initial['routes'])
        response = client.put('/api/maintenance-proxy-routes/Loomy/balance', json={'expected_version': 0, 'mode': 'fixed', 'proxy_id': nodes[1]})
        assert response.status_code == 200 and response.json()['version'] == 1
        public = client.get('/api/maintenance-proxy-routes').text
        assert 'http://' not in public and 'proxy1:8080' not in public


def test_worker_checkin_then_balance_use_separate_task_routes(data):
    store, accounts, nodes, _ = data
    configure(store, 'checkin', 'fixed', proxy_id=nodes[1])
    configure(store, 'balance', 'fixed', proxy_id=nodes[2])
    calls = []
    class Registry:
        def make(self, platform, credential, account, version, proxy):
            class Adapter:
                def __init__(self): self.t = self
                async def close(self): pass
                async def checkin(self, day): calls.append(('checkin', proxy)); return {'day': day}
                async def balance(self): calls.append(('balance', proxy)); return 42
            return Adapter()
    worker = MaintenanceWorker(store, registry=Registry())
    jid = store.plan(accounts[0], 'checkin')
    first = store.claim('owner', 1)
    assert first['id'] == jid and first['kind'] == 'checkin'
    asyncio.run(worker.execute(first))
    second = store.claim('owner', 1)
    assert second['kind'] == 'balance'
    asyncio.run(worker.execute(second))
    assert calls == [('checkin', 'http://proxy1:8080'), ('balance', 'http://proxy2:8080')]
    assert resolve_account_proxy(store, accounts[0]) == 'http://proxy0:8080'


def test_invitation_recipient_and_donor_use_invitation_route(data):
    store, accounts, nodes, _ = data
    configure(store, 'invitation', 'fixed', proxy_id=nodes[2])
    class Registry(InvitationRegistry):
        def __init__(self): super().__init__(); self.proxies = []
        def make(self, platform, credential, account, version, proxy):
            self.proxies.append((account, proxy))
            return super().make(platform, credential, account, version, proxy)
    registry = Registry()
    service = Invitations(store, registry)
    service.queue(accounts[0], auto=True)
    job = store.claim('owner', 1)
    worker = MaintenanceWorker(store, registry=registry)
    worker.invitations = service
    asyncio.run(worker.execute(job))
    assert {p[0] for p in registry.proxies} == set(accounts)
    assert all(p[1] == 'http://proxy2:8080' for p in registry.proxies)
    assert registry.submits == [(accounts[0], 'synthetic-code')]
