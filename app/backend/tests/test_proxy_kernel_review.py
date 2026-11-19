"""Independent review: kernel readiness must prove the bound authenticated listener."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app import proxy_kernel
from app.proxy_protocols import parse_node
from app.store import DomainError, Store


@pytest.fixture
def store(tmp_path):
    value=Store(f"sqlite:///{tmp_path/'kernel-review.db'}",Fernet.generate_key())
    yield value
    value.engine.dispose()


def nodes(store, suffix=0):
    material={'type':'vmess','name':'synthetic','server':'example.com','port':443,
              'uuid':f'12345678-1234-1234-1234-123456789ab{suffix}','alterId':0,'cipher':'auto','tls':True}
    parsed=parse_node(material)
    node=store.resource_put('proxy','synthetic-'+str(suffix),{'status':'pending_kernel','identity':parsed['identity'],'kernel_state':'pending'})
    store.resource_put('proxy_kernel_source',node,{},secret=json.dumps(material))
    return node


def test_node_material_and_listener_password_never_public(store):
    nodes(store)
    proxy_kernel.reserve_nodes(store)
    public=json.dumps(store.resources('proxy')+store.resources('proxy_kernel_source')+store.resources('proxy_kernel_slot'))
    assert '12345678-1234-1234-1234-123456789ab0' not in public
    assert 'example.com' not in public
    assert 'password' not in public


def secret(store,node_id):
    from app.models import Resource
    with store.tx() as (session,state):
        row=session.get(Resource,node_id)
        return store.cipher.decrypt(row.secret.encode()).decode() if row.secret else None


def fake_kernel(store,tmp_path,monkeypatch,owned):
    kernel=proxy_kernel.ProxyKernel(store,enabled=True,binary='synthetic',directory=tmp_path/'kernel')
    kernel.process=SimpleNamespace(returncode=None,pid=12345)
    monkeypatch.setattr(proxy_kernel.shutil,'which',lambda _: 'synthetic')
    async def validate(path):pass
    async def controller(method,path,payload=None):
        if path=='/proxies':return {'proxies':{'node-'+n['id']:{} for n in proxy_kernel.reserve_nodes(store)}}
        return {}
    kernel._validate=validate;kernel._controller=controller
    monkeypatch.setattr(proxy_kernel,'owned_listener_ports',lambda pid: owned())
    return kernel


def ports(store):
    return {('0100007F',19090)} | {('00000000',n['port']) for n in proxy_kernel.reserve_nodes(store)}


def test_occupied_unrelated_listener_cannot_be_published_ready(store,tmp_path,monkeypatch):
    node_id=nodes(store)
    kernel=fake_kernel(store,tmp_path,monkeypatch,lambda:{('0100007F',19090)})
    with pytest.raises(DomainError,match='kernel_listener_not_owned'):
        asyncio.run(kernel.tick())
    public=next(r for r in store.resources('proxy') if r['id']==node_id)
    assert public['data'].get('kernel_state')!='ready'


def test_owned_listener_publishes_and_fastpath_rechecks_bind(store,tmp_path,monkeypatch):
    node_id=nodes(store)
    owned=ports(store)
    kernel=fake_kernel(store,tmp_path,monkeypatch,lambda:owned)
    asyncio.run(kernel.tick())
    assert store.resources('proxy')[0]['data']['kernel_state']=='ready'
    previous=secret(store,node_id)
    owned.clear()
    with pytest.raises(DomainError,match='kernel_listener_not_owned'):
        asyncio.run(kernel.tick())
    assert secret(store,node_id)==previous
    assert kernel.status['listeners_ready'] is False
    assert kernel.status['checking'] is False


def test_routine_recheck_preserves_verified_readiness_until_outcome(store,tmp_path,monkeypatch):
    nodes(store)
    kernel=fake_kernel(store,tmp_path,monkeypatch,lambda:ports(store))
    asyncio.run(kernel.tick())
    verified=kernel.status['last_verified_at']
    original=kernel._verify_loaded

    async def scenario():
        entered,released=asyncio.Event(),asyncio.Event()
        async def delayed(items):
            entered.set()
            await released.wait()
            await original(items)
        kernel._verify_loaded=delayed
        task=asyncio.create_task(kernel.tick())
        await entered.wait()
        assert kernel.status['checking'] is True
        assert kernel.status['listeners_ready'] is True
        assert kernel.status['last_verified_at']==verified
        released.set()
        await task
        assert kernel.status['checking'] is False
        assert kernel.status['listeners_ready'] is True
    asyncio.run(scenario())


def test_rejected_config_retains_old_endpoint_and_does_not_publish_new(store,tmp_path,monkeypatch):
    first=nodes(store)
    kernel=fake_kernel(store,tmp_path,monkeypatch,lambda:ports(store))
    asyncio.run(kernel.tick())
    previous=secret(store,first)
    second=nodes(store,1)
    async def reject(path):raise DomainError('kernel_config_rejected',503)
    kernel._validate=reject
    with pytest.raises(DomainError,match='kernel_config_rejected'):
        asyncio.run(kernel.tick())
    assert secret(store,first)==previous
    assert not secret(store,second)
    assert next(x for x in store.resources('proxy') if x['id']==second)['data']['kernel_state']=='pending'
