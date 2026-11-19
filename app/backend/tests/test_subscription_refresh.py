import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Resource
from app.store import Store, DomainError
from app.subscriptions import parse_subscription, save_nodes, save_schedule, ScheduleInput
from app.subscription_scheduler import SubscriptionScheduler
from app.upstream import Transport


@pytest.fixture
def store(tmp_path):
    s = Store(f'sqlite:///{tmp_path / "subscriptions.db"}', Fernet.generate_key())
    yield s
    s.engine.dispose()


def source(store, name='source', **data):
    return store.resource_put('proxy_subscription', name, data, secret='https://synthetic.invalid/feed')


def refresh(store, sid, content):
    nodes, rejected = parse_subscription(content)
    return save_nodes(store, sid, nodes, rejected)


def registry(handler):
    def factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = Transport(client=client)
        transport.owned = True
        return transport
    return SimpleNamespace(enabled=True, transport_factory=factory)


def test_cross_source_dedup_and_one_missing_source_does_not_retire_shared_node(store):
    a, b = source(store, 'a'), source(store, 'b')
    refresh(store, a, 'http://USER:pass@HOST:8080/#first')
    summary = refresh(store, b, 'http://USER:pass@host:8080#second')
    nodes = store.resources('proxy')
    assert len(nodes) == 1 and summary['shared'] == 1
    assert set(nodes[0]['data']['subscription_ids']) == {a,b}
    save_nodes(store,a,[],[])
    row = store.resources('proxy')[0]
    assert row['data']['subscription_ids'] == [b] and row['data']['status'] != 'retired'
    save_nodes(store,b,[],[])
    assert store.resources('proxy')[0]['data']['status'] == 'retired'


def test_different_auth_is_not_deduplicated_and_shared_rotation_does_not_override_other_source(store):
    a, b = source(store, 'a'), source(store, 'b')
    refresh(store,a,'http://user:old@host:8080')
    refresh(store,b,'http://user:old@host:8080')
    old_id = store.resources('proxy')[0]['id']
    refresh(store,a,'http://user:new@host:8080')
    assert len(store.resources('proxy')) == 2
    with store.read() as (s,_):
        old = s.get(Resource,old_id)
        assert 'old@' in store.cipher.decrypt(old.secret.encode()).decode()
        assert json.loads(old.data)['subscription_ids'] == [b]


@pytest.mark.parametrize('status',['disabled','retired'])
def test_refresh_never_revives_manual_status(store,status):
    a = source(store)
    refresh(store,a,'http://user:old@host:8080')
    node = store.resources('proxy')[0]
    with store.tx() as (s,_):
        row=s.get(Resource,node['id']);data=json.loads(row.data);data['status']=status;row.data=json.dumps(data)
    refresh(store,a,'http://user:new@host:8080')
    assert store.resources('proxy')[0]['id'] == node['id']
    assert store.resources('proxy')[0]['data']['status'] == status


def test_partial_parse_does_not_retire_previously_known_nodes(store):
    a=source(store)
    refresh(store,a,'http://host:8080\nhttp://host:8081')
    refresh(store,a,'http://host:8080\nvless://unhandled')
    assert all(n['data']['status']!='retired' for n in store.resources('proxy'))


def test_legacy_source_ownership_upgrades_without_duplicate(store):
    a,b=source(store,'a'),source(store,'b')
    original=store.resource_put('proxy','legacy',{'scheme':'http','status':'untested','subscription_id':a,'identity':'legacy-hash'},secret='http://host:8080')
    refresh(store,b,'http://host:8080')
    assert len(store.resources('proxy'))==1
    assert store.resources('proxy')[0]['id']==original
    assert set(store.resources('proxy')[0]['data']['subscription_ids'])=={a,b}


@pytest.mark.parametrize('body,status',[(b'',200),(b'<html>unavailable</html>',200),(b'http://changed:8080',502)])
def test_fetch_failure_or_empty_success_preserves_existing_nodes(store,body,status):
    a=source(store, auto_update=True, interval_minutes=5)
    refresh(store,a,'http://host:8080')
    before=store.resources('proxy')
    async def handle(request):return httpx.Response(status,content=body)
    service=SubscriptionScheduler(store,registry(handle))
    with pytest.raises(DomainError):asyncio.run(service.sync(a))
    assert store.resources('proxy')==before
    data=store.resources('proxy_subscription')[0]['data']
    assert data['last_error'] and 'sync_lease' not in data and data['auto_update'] is True


def test_due_claim_is_atomic_and_old_sources_not_scheduled(store):
    a=source(store,'a',auto_update=True,next_sync_at=0,interval_minutes=5)
    old=source(store,'old')
    async def handle(request):return httpx.Response(200,text='http://host:8080')
    service=SubscriptionScheduler(store,registry(handle))
    assert service.due_ids()==[a]
    token,_=service.claim(a,True)
    with pytest.raises(DomainError) as error:service.claim(a,True)
    assert error.value.code=='subscription_sync_in_progress'
    assert service.claim(old,True) is None
    service.fail(a,token,'synthetic_error')
    assert service.due_ids()==[]


def test_two_workers_only_fetch_once_and_restart_keeps_due_time(store):
    a=source(store,auto_update=True,next_sync_at=0,interval_minutes=5)
    calls=[]
    async def handle(request):
        calls.append(True)
        await asyncio.sleep(.02)
        return httpx.Response(200,text='http://host:8080')
    first,second=SubscriptionScheduler(store,registry(handle)),SubscriptionScheduler(store,registry(handle))
    async def run():await asyncio.gather(first.tick(),second.tick())
    asyncio.run(run())
    assert calls==[True]
    assert SubscriptionScheduler(store,registry(handle)).due_ids()==[]


def test_schedule_cas_and_paste_cannot_auto_refresh(store):
    a=store.resource_put('proxy_subscription','paste',{'source':'paste'})
    with pytest.raises(DomainError):save_schedule(store,a,ScheduleInput(expected_version=1,auto_update=True))
    b=source(store)
    saved=save_schedule(store,b,ScheduleInput(expected_version=1,auto_update=True,interval_minutes=5))
    assert saved['data']['next_sync_at']>store.clock()
    with pytest.raises(DomainError):save_schedule(store,b,ScheduleInput(expected_version=1,auto_update=False))


def test_new_url_defaults_auto_old_and_paste_do_not(store):
    app=create_app(store,start_worker=False)
    with TestClient(app,headers={'X-Console-Client':'unified-local'}) as client:
        assert client.post('/api/proxy-subscriptions',json={'name':'url','url':'https://synthetic.invalid/feed'}).status_code==201
        assert client.post('/api/proxy-subscriptions',json={'name':'paste','content':'http://host:8080'}).status_code==201
    rows={r['name']:r['data'] for r in store.resources('proxy_subscription')}
    assert rows['url']['auto_update'] is True and rows['paste']['auto_update'] is False
    assert store.resources('proxy')[0]['data']['target_check_required'] is True


def test_cancelled_fetch_releases_lease_without_retiring_nodes(store):
    a=source(store,auto_update=True,interval_minutes=5)
    refresh(store,a,'http://host:8080')
    before=store.resources('proxy')
    async def run():
        started=asyncio.Event()
        async def handle(request):
            started.set();await asyncio.sleep(10)
        service=SubscriptionScheduler(store,registry(handle))
        task=asyncio.create_task(service.sync(a))
        await started.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert store.resources('proxy')==before
    assert 'sync_lease' not in store.resources('proxy_subscription')[0]['data']


def test_vmess_cross_source_dedup_encrypted_material_and_no_http_endpoint(store):
    value={'v':'2','ps':'synthetic-a','add':'proxy.example','port':'443','id':'11111111-2222-3333-4444-555555555555','aid':'0','net':'ws','path':'/edge','tls':'tls','sni':'proxy.example'}
    uri='vmess://'+base64.b64encode(json.dumps(value).encode()).decode()
    a,b=source(store,'a'),source(store,'b')
    refresh(store,a,uri)
    value['ps']='synthetic-b'
    refresh(store,b,'vmess://'+base64.b64encode(json.dumps(value).encode()).decode())
    nodes=store.resources('proxy')
    assert len(nodes)==1 and nodes[0]['has_secret'] is False
    assert nodes[0]['data']['status']=='pending_kernel'
    assert set(nodes[0]['data']['subscription_ids'])=={a,b}
    sources=store.resources('proxy_kernel_source')
    assert len(sources)==1 and sources[0]['name']==nodes[0]['id']
    assert '11111111-2222-3333-4444-555555555555' not in json.dumps(nodes+sources)
    with store.read() as (s,_):
        material=s.get(Resource,sources[0]['id'])
        assert json.loads(store.cipher.decrypt(material.secret.encode()))['network']=='ws'


def test_target_config_validation_and_schedule_persistence(store):
    app=create_app(store,start_worker=False)
    with TestClient(app,headers={'X-Console-Client':'unified-local'}) as client:
        bad=client.post('/api/proxy-subscriptions',json={'name':'bad','url':'https://synthetic.invalid/feed','target_platforms':['unknown']})
        assert bad.status_code==422
        bad=client.post('/api/proxy-subscriptions',json={'name':'bad','url':'https://synthetic.invalid/feed','auto_target_check':True})
        assert bad.status_code==422
        added=client.post('/api/proxy-subscriptions',json={'name':'good','url':'https://synthetic.invalid/feed','auto_target_check':True,'target_platforms':['Loomy','Loomy']}).json()
        row=store.resources('proxy_subscription')[0]
        assert row['data']['target_platforms']==['Loomy']
        saved=client.put('/api/proxy-subscriptions/'+added['id']+'/schedule',json={'expected_version':row['version'],'auto_update':False,'interval_minutes':60})
        assert saved.status_code==200
        assert saved.json()['data']['auto_target_check'] is True
        assert saved.json()['data']['target_platforms']==['Loomy']
