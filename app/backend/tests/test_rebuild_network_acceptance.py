"""P3 network acceptance: isolated data, mock HTTP, no production operations."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.adapters import Registry
from app.models import Resource
from app.proxy_management import mount_proxy_management
from app.proxy_pool import mount_proxy_pools, resolve_account_proxy, save_route, RouteInput
from app.proxy_target_health import ProxyTargetHealth, TARGET_URLS
from app.proxy_target_scheduler import register_routes
from app.registration import RegistrationService
from app.store import Store, DomainError, uid
from app.subscriptions import mount_subscriptions, parse_subscription, save_nodes
from app.subscription_scheduler import SubscriptionScheduler
from app.upstream import Transport


@pytest.fixture
def network(tmp_path):
    store=Store(f'sqlite:///{tmp_path / "network-p3.db"}',Fernet.generate_key())
    app=FastAPI()
    registry=SimpleNamespace(enabled=False)
    @app.exception_handler(DomainError)
    async def error(_,exc):
        return JSONResponse({'error':{'code':exc.code}},status_code=exc.status)
    mount_proxy_management(app,store,registry)
    mount_proxy_pools(app,store)
    mount_subscriptions(app,store,registry)
    checker=ProxyTargetHealth(store,enabled=True)
    scheduler=register_routes(app,store,checker)
    with TestClient(app) as client:
        yield store,client,scheduler
    store.engine.dispose()


def add(client,name='native node'):
    response=client.post('/api/proxies',json={'name':name,'url':'http://synthetic:offline-material@proxy.invalid:8080'})
    assert response.status_code==201,response.text
    return response.json()['id']


def node(client,identity,status=''):
    response=client.get('/api/proxy-directory',params={'q':identity,'status':status})
    assert response.status_code==200,response.text
    return response.json()['nodes'][0]


def refresh(store,source,content):
    nodes,rejected=parse_subscription(content)
    return save_nodes(store,source,nodes,rejected)


def source(store):
    return store.resource_put('proxy_subscription','native source',{'source':'url','auto_update':True,'interval_minutes':5},secret='https://feed.invalid/first')


def mocked_registry(handler):
    def factory(**_):
        result=Transport(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        result.owned=True
        return result
    return SimpleNamespace(enabled=True,transport_factory=factory)


def test_p3_crud_versions_bulk_and_sensitive_material(network):
    store,client,_=network
    identities=[add(client,f'node {n}') for n in range(3)]
    first=node(client,identities[0])
    assert first['address']=='http://proxy.invalid:8080'
    assert 'offline-material' not in json.dumps(first)
    update={'expected_version':first['version'],'name':'edited native'}
    assert client.put(f'/api/proxy-directory/{first["id"]}',json=update).status_code==200
    assert client.put(f'/api/proxy-directory/{first["id"]}',json=update).status_code==409
    assert node(client,first['id'])['name']=='edited native'
    for action,expected in [('disable','disabled'),('enable','untested')]:
        assert client.post('/api/proxy-directory/batch',json={'action':action,'ids':identities}).json()['affected']==3
        assert {node(client,i)['data']['status'] for i in identities}=={expected}
    assert client.post('/api/proxy-directory/batch',json={'action':'delete','ids':identities[:2]}).status_code==200
    assert client.get('/api/proxy-directory').json()['total']==1
    last=node(client,identities[-1])
    assert client.delete(f'/api/proxy-directory/{last["id"]}',params={'expected_version':last['version']}).status_code==200
    assert client.get('/api/proxy-directory').json()['total']==0


def test_p3_reference_protection_atomic_rollback_and_no_direct(network):
    store,client,_=network
    bound,free=add(client,'bound'),add(client,'free')
    account=store.create_account('Loomy','isolated-account','isolated-account')
    assert client.put(f'/api/accounts/{account}/proxy',json={'proxy_id':bound}).status_code==200
    assert node(client,bound)['account_bindings']==1
    result=client.get(f'/api/proxy-directory/{bound}/references').json()
    assert result['total']==1 and result['items'][0]['kind']=='proxy_binding'
    before=node(client,free)
    response=client.post('/api/proxy-directory/batch',json={'action':'delete','ids':[free,bound]})
    assert response.status_code==409
    assert node(client,free)==before
    assert client.put(f'/api/proxy-directory/{bound}',json={'expected_version':node(client,bound)['version'],'url':'http://changed.invalid:8080'}).status_code==409
    assert client.post('/api/proxy-directory/batch',json={'action':'disable','ids':[bound]}).status_code==200
    with pytest.raises(DomainError,match='proxy_unavailable'):
        resolve_account_proxy(store,account)
    assert client.put(f'/api/accounts/{account}/proxy',json={'proxy_id':bound}).status_code==422
    assert client.get(f'/api/accounts/{account}/proxy').json()['proxy_id']==bound


def test_p3_source_dedup_partial_refresh_name_and_deleted_tombstone(network):
    store,client,_=network
    a,b=source(store),source(store)
    content='http://source.invalid:8080#original\nhttp://second.invalid:8080#second'
    refresh(store,a,content)
    refresh(store,b,'http://source.invalid:8080#shared')
    shared=client.get('/api/proxy-directory',params={'q':'shared'}).json()['nodes'][0]
    assert set(shared['data']['subscription_ids'])=={a,b}
    assert client.put(f'/api/proxy-directory/{shared["id"]}',json={'expected_version':shared['version'],'name':'user name'}).status_code==200
    refresh(store,a,'http://source.invalid:8080#new label\nnot-a-supported-node')
    assert node(client,shared['id'])['name']=='user name'
    assert client.get('/api/proxy-directory',params={'source':a}).json()['total']==2
    current=node(client,shared['id'])
    assert client.delete(f'/api/proxy-directory/{shared["id"]}',params={'expected_version':current['version']}).status_code==200
    refresh(store,a,content)
    refresh(store,b,'http://source.invalid:8080#refreshed')
    assert not client.get('/api/proxy-directory',params={'q':shared['id']}).json()['nodes']
    retired=node(client,shared['id'],'retired')
    assert retired['data']['deleted'] is True
    assert client.post('/api/proxy-directory/batch',json={'action':'enable','ids':[shared['id']]}).status_code==200
    assert node(client,shared['id'])['data']['status']=='untested'


@pytest.mark.parametrize('body,status',[(b'',200),(b'not-a-node',200),(b'http://different.invalid:8080',503)])
def test_p3_subscription_failure_preserves_nodes(network,body,status):
    store,client,_=network
    sid=source(store)
    refresh(store,sid,'http://source.invalid:8080')
    before=client.get('/api/proxy-directory').json()['nodes']
    service=SubscriptionScheduler(store,mocked_registry(lambda request:httpx.Response(status,content=body)))
    with pytest.raises(DomainError):
        asyncio.run(service.sync(sid))
    assert client.get('/api/proxy-directory').json()['nodes']==before
    data=next(r for r in store.resources('proxy_subscription') if r['id']==sid)['data']
    assert data['status']=='refresh_failed' and data['last_error']
    assert data['next_sync_at']>store.clock() and 'sync_lease' not in data


def test_p3_inflight_old_url_cannot_overwrite_new_source(network):
    store,client,_=network
    sid=source(store)
    refresh(store,sid,'http://existing.invalid:8080')
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        async def upstream(request):
            assert str(request.url)=='https://feed.invalid/first'
            entered.set()
            await release.wait()
            return httpx.Response(200,text='http://late-old-url.invalid:8080')
        service=SubscriptionScheduler(store,mocked_registry(upstream))
        task=asyncio.create_task(service.sync(sid))
        await asyncio.wait_for(entered.wait(),3)
        before=next(r for r in store.resources('proxy_subscription') if r['id']==sid)
        response=await asyncio.to_thread(client.put,f'/api/proxy-subscriptions/{sid}',json={'expected_version':before['version'],'name':'new feed','url':'https://feed.invalid/second'})
        assert response.status_code==200
        release.set()
        with pytest.raises(DomainError,match='subscription_sync_superseded'):
            await task
    asyncio.run(scenario())
    assert client.get('/api/proxy-directory').json()['nodes'][0]['name']!='late-old-url.invalid'
    assert client.get('/api/proxy-directory').json()['total']==1
    row=next(r for r in store.resources('proxy_subscription') if r['id']==sid)
    assert row['name']=='new feed' and row['data']['status']=='not_synced'
    assert not row['data'].get('last_error')


def test_p3_disable_node_during_refresh_stays_disabled(network):
    store,client,_=network
    sid=source(store)
    refresh(store,sid,'http://source.invalid:8080')
    identity=client.get('/api/proxy-directory').json()['nodes'][0]['id']
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        async def upstream(request):
            entered.set();await release.wait()
            return httpx.Response(200,text='http://source.invalid:8080#new name')
        service=SubscriptionScheduler(store,mocked_registry(upstream))
        task=asyncio.create_task(service.sync(sid));await asyncio.wait_for(entered.wait(),3)
        response=await asyncio.to_thread(client.post,'/api/proxy-directory/batch',json={'action':'disable','ids':[identity]})
        assert response.status_code==200
        release.set();await task
    asyncio.run(scenario())
    assert node(client,identity)['data']['status']=='disabled'


def test_p3_registration_session_keeps_selected_ip_and_fails_closed(network):
    store,client,_=network
    first,second=add(client,'first exit'),add(client,'second exit')
    service=RegistrationService(store,Registry(enabled=False))
    def verified_factory(**kwargs):
        kwargs.pop('proxy')
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200)),**kwargs)
    asyncio.run(ProxyTargetHealth(store,client_factory=verified_factory).check(first,'Loomy'))
    asyncio.run(ProxyTargetHealth(store,client_factory=verified_factory).check(second,'Loomy'))
    save_route(store,'Loomy',RouteInput(mode='fixed',proxy_id=first))
    session_id=service.create('Loomy','isolated login','sms',{'phone':'13800000000'})
    data,material=service.details(session_id)
    endpoint=service.proxy(data,material)
    save_route(store,'Loomy',RouteInput(mode='fixed',proxy_id=second))
    assert service.proxy(data,material)==endpoint
    assert data['proxy_id']==first
    with pytest.raises(DomainError,match='proxy_unavailable'):
        service.proxy(data,{**material,'_proxy_url':None})
    assert client.post('/api/proxy-directory/batch',json={'action':'disable','ids':[first]}).status_code==200
    with pytest.raises(DomainError,match='proxy_unavailable'):
        service.proxy(data,material)


@pytest.mark.parametrize('status,code',[(403,'target_forbidden'),(429,'target_rate_limited'),(401,'target_auth_required')])
def test_p3_actual_platform_target_http_is_not_account_permission(network,status,code):
    store,client,_=network
    identity=add(client)
    requests=[]
    def upstream(request):
        requests.append(request)
        assert str(request.url)==TARGET_URLS['Loomy']
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        return httpx.Response(status)
    def factory(**kwargs):
        kwargs.pop('proxy')
        assert kwargs['verify'] and not kwargs['trust_env']
        return httpx.AsyncClient(transport=httpx.MockTransport(upstream),**kwargs)
    result=asyncio.run(ProxyTargetHealth(store,client_factory=factory).check(identity,'Loomy'))
    assert requests and result['code']==code and result['http_status']==status
    assert result['tcp_reachable'] and result['target_http_reachable'] and not result['eligible']
    assert not any(result[k] for k in ('account_verified','model_verified','business_verified'))
    assert node(client,identity)['data']['status']=='untested'


def test_p3_cancel_batch_fences_late_chunk_and_history_is_paged(network):
    store,client,scheduler=network
    identity=add(client)
    for _ in range(3):
        assert client.post('/api/proxy-target-checks',json={'proxy_ids':[identity],'platforms':['Loomy']}).status_code==202
    claim=scheduler._claim()
    assert claim
    assert client.post(f'/api/proxy-target-batches/{claim["id"]}/cancel').status_code==200
    assert scheduler._finish_chunk(claim,[{'code':'target_http_ok'}]) is False
    result=client.get('/api/proxy-target-batches',params={'page':2,'page_size':1}).json()
    assert result['total']==3 and len(result['batches'])==1
    assert all('items' not in r['data'] and 'lease_token' not in r['data'] for r in result['batches'])


def test_p3_thousand_node_sql_paging_filters_sort_and_counts(network,monkeypatch):
    store,client,_=network
    sid=uid()
    with store.tx() as (session,_):
        session.add_all([Resource(id=uid(),kind='proxy',name=f'native-{n:04}',version=1,updated_at=n,
            data=json.dumps({'scheme':'http' if n%2==0 else 'socks5','status':'disabled' if n%3==0 else 'untested','subscription_ids':[sid] if n%4==0 else []},separators=(',',':'))) for n in range(1200)])
    statements=[]
    def record(connection,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    event.listen(store.engine,'before_cursor_execute',record)
    original=store.resources
    def no_full_nodes(kind,*args,**kwargs):
        assert kind!='proxy','Directory must not call full resource snapshot'
        return original(kind,*args,**kwargs)
    monkeypatch.setattr(store,'resources',no_full_nodes)
    try:
        page=client.get('/api/proxy-directory',params={'source':sid,'protocol':'http','status':'disabled','page_size':25,'page':2,'sort':'updated'}).json()
        assert page['total']==100 and len(page['nodes'])==25
        assert page['nodes'][0]['name']=='native-0888'
        assert page['nodes'][-1]['name']=='native-0600'
        assert len({r['id'] for r in page['nodes']})==25
        assert client.get('/api/proxy-directory',params={'source':'manual'}).json()['total']==900
        assert client.get('/api/proxy-directory',params={'source':'subscription'}).json()['total']==300
        assert any('LIMIT' in sql.upper() and 'OFFSET' in sql.upper() and 'ORDER BY' in sql.upper() for sql in statements)
        assert any('COUNT(' in sql.upper() for sql in statements)
    finally:
        event.remove(store.engine,'before_cursor_execute',record)


def test_p3_kernel_readiness_owns_listener_and_reenable_republishes(network,tmp_path,monkeypatch):
    from app import proxy_kernel
    store,client,_=network
    material={'type':'vmess','name':'controlled vmess','server':'vmess.invalid','port':443,
              'uuid':'12345678-1234-1234-1234-123456789ab0','alterId':0,'cipher':'auto','tls':True}
    result=client.post('/api/proxy-subscriptions',json={'name':'native kernel source','content':json.dumps({'proxies':[material]})})
    assert result.status_code==201
    identity=client.get('/api/proxy-directory').json()['nodes'][0]['id']
    kernel=proxy_kernel.ProxyKernel(store,enabled=True,binary='controlled-kernel',directory=tmp_path/'kernel')
    monkeypatch.setattr(proxy_kernel.shutil,'which',lambda _: 'controlled-kernel')
    async def validate(_):
        return None
    async def spawn(*args,**kwargs):
        return SimpleNamespace(returncode=None,pid=4242)
    async def controller(method,path,payload=None):
        if path=='/version':
            return {'version':'controlled-contract-1'}
        if path=='/proxies':
            return {'proxies':{'node-'+r['id']:{} for r in proxy_kernel.reserve_nodes(store)}}
        return {}
    kernel._validate=validate
    kernel._controller=controller
    monkeypatch.setattr(proxy_kernel.asyncio,'create_subprocess_exec',spawn)
    owned={('0100007F',19090)}
    monkeypatch.setattr(proxy_kernel,'owned_listener_ports',lambda pid:owned)
    with pytest.raises(DomainError,match='kernel_listener_not_owned'):
        asyncio.run(kernel.tick())
    assert not node(client,identity)['has_secret']
    assert kernel.status['listeners_ready'] is False
    owned.update(('00000000',r['port']) for r in proxy_kernel.reserve_nodes(store))
    asyncio.run(kernel.tick())
    published=node(client,identity)
    assert published['data']['kernel_state']=='ready' and published['has_secret']
    assert kernel.status['version']=='controlled-contract-1'
    assert kernel.status['generation']==1 and kernel.status['listeners_ready']
    assert '12345678' not in json.dumps(published) and '@' not in published['address']
    assert client.post('/api/proxy-directory/batch',json={'action':'disable','ids':[identity]}).status_code==200
    asyncio.run(kernel.tick())
    assert kernel.status['nodes']==0
    assert client.post('/api/proxy-directory/batch',json={'action':'enable','ids':[identity]}).status_code==200
    pending=node(client,identity)
    assert pending['data']['status']=='pending_kernel' and not pending['has_secret']
    asyncio.run(kernel.tick())
    assert node(client,identity)['data']['kernel_state']=='ready'
    assert kernel.status['generation']==3 and kernel.status['nodes']==1


def test_p3_benefits_maintenance_route_stays_independent_and_fails_closed(network):
    from app.maintenance_proxy import save_route as save_maintenance_route, RouteInput as MaintenanceRoute, resolve_maintenance_proxy
    store,client,_=network
    business,maintenance=add(client,'business exit'),add(client,'benefits exit')
    account=store.create_account('WorkBuddy 国内','isolated-benefits','isolated-benefits')
    store.bind_proxy(account,business)
    save_maintenance_route(store,'WorkBuddy 国内','benefits',MaintenanceRoute(expected_version=0,mode='fixed',proxy_id=maintenance))
    # This fixture accepts direct endpoint material; target checks are a separate gate.
    with store.tx() as (session,_):
        row=session.get(Resource,maintenance)
        data=json.loads(row.data);data['target_check_required']=False;row.data=json.dumps(data)
    assert resolve_maintenance_proxy(store,account,'benefits') is not None
    assert client.get(f'/api/accounts/{account}/proxy').json()['proxy_id']==business
    assert client.post('/api/proxy-directory/batch',json={'action':'disable','ids':[maintenance]}).status_code==200
    with pytest.raises(DomainError,match='proxy_unavailable'):
        resolve_maintenance_proxy(store,account,'benefits')


def test_subscription_preview_is_bounded_read_only_and_never_returns_material(network):
    store,client,_=network
    content='\n'.join(f'http://private-user:private-password@127.0.0.1:{10000+i}#node-{i}' for i in range(105))+'\nunsupported://bad'
    response=client.post('/api/proxy-subscriptions/preview',json={'content':content})
    assert response.status_code==200
    result=response.json()
    assert result['total']==105 and len(result['nodes'])==100 and result['truncated']
    assert result['rejected']==1 and result['protocols']=={'http':105}
    assert all(set(n)=={'name','scheme'} for n in result['nodes'])
    assert 'private-password' not in response.text and '127.0.0.1' not in response.text
    assert not store.resources('proxy') and not store.resources('proxy_subscription')
    assert client.post('/api/proxy-subscriptions/preview',json={'url':'https://feed.invalid','content':content}).status_code==422
    assert client.post('/api/proxy-subscriptions/preview',json={'url':'https://feed.invalid'}).status_code==409


def test_local_subscription_replace_preserves_id_disabled_binding_and_shared_source(network):
    store,client,_=network
    first=client.post('/api/proxy-subscriptions',json={'name':'local source','content':'http://127.0.0.1:18001#first','source_kind':'file','file_name':'C:/private/nodes.yaml'})
    sid=first.json()['id'];original=store.resources('proxy')[0]
    other=client.post('/api/proxy-subscriptions',json={'name':'shared source','content':'http://127.0.0.1:18001#shared'}).json()['id']
    account=store.create_account('Loomy','replace-case','replace-case')
    assert client.put(f'/api/accounts/{account}/proxy',json={'proxy_id':original['id']}).status_code==200
    assert client.post('/api/proxy-directory/batch',json={'action':'disable','ids':[original['id']]}).status_code==200
    current=next(s for s in store.resources('proxy_subscription') if s['id']==sid)
    edited=client.put(f'/api/proxy-subscriptions/{sid}',json={'expected_version':current['version'],'name':'renamed local','content':'http://127.0.0.1:18001#same\nhttp://127.0.0.1:18002#new','source_kind':'file','file_name':'/private/new.yaml'})
    assert edited.status_code==200
    same=node(client,original['id']);assert same['data']['status']=='disabled'
    assert set(same['data']['subscription_ids'])=={sid,other}
    assert client.get(f'/api/accounts/{account}/proxy').json()['proxy_id']==original['id']
    current=next(s for s in store.resources('proxy_subscription') if s['id']==sid)
    assert current['name']=='renamed local' and current['data']['file_name']=='new.yaml' and current['data']['node_count']==2
    before=store.resources('proxy')
    bad=client.put(f'/api/proxy-subscriptions/{sid}',json={'expected_version':current['version'],'name':'must rollback','content':'unsupported://bad'})
    assert bad.status_code==422 and store.resources('proxy')==before
    assert next(s for s in store.resources('proxy_subscription') if s['id']==sid)['name']=='renamed local'


def test_manual_refresh_queue_progress_failure_and_public_lease_fence(tmp_path):
    store=Store(f'sqlite:///{tmp_path / "queue.db"}',Fernet.generate_key())
    sid=source(store);responses=[200,503]
    def handler(request):
        status=responses.pop(0)
        return httpx.Response(status,text='http://127.0.0.1:18001#queued')
    scheduler=SubscriptionScheduler(store,mocked_registry(handler))
    try:
        with store.tx() as (s,_):
            row=s.get(Resource,sid);data=json.loads(row.data);data['auto_update']=False;row.data=json.dumps(data)
        scheduler.request_sync(sid)
        assert scheduler.due_ids()==[sid]
        with pytest.raises(DomainError,match='subscription_sync_in_progress'):scheduler.request_sync(sid)
        assert asyncio.run(scheduler.tick())==1
        source_row=store.resources('proxy_subscription')[0]
        assert source_row['data']['sync_progress']['phase']=='completed'
        assert not source_row['data'].get('sync_requested') and source_row['data']['next_sync_at'] is None
        success_at=source_row['data']['last_sync_at']
        scheduler.request_sync(sid)
        assert asyncio.run(scheduler.tick())==0
        data=store.resources('proxy_subscription')[0]['data']
        assert data['status']=='refresh_failed' and data['last_sync_at']==success_at
        assert data['last_failure_code']=='subscription_fetch_failed' and data['sync_progress']['phase']=='failed'
        assert len(store.resources('proxy'))==1
    finally:store.engine.dispose()


def test_url_preview_uses_same_bounded_reader_and_hides_sync_token(tmp_path):
    store=Store(f'sqlite:///{tmp_path / "preview.db"}',Fernet.generate_key())
    app=FastAPI();registry=mocked_registry(lambda request:httpx.Response(200,text='http://private-user:private-pass@127.0.0.1:18001#node'))
    @app.exception_handler(DomainError)
    async def error(_,exc):return JSONResponse({'error':{'code':exc.code}},status_code=exc.status)
    mount_subscriptions(app,store,registry)
    try:
        with TestClient(app) as client:
            sid=client.post('/api/proxy-subscriptions',json={'name':'remote','url':'https://feed.invalid/path?secret=private-query','auto_update':False}).json()['id']
            response=client.post('/api/proxy-subscriptions/preview',json={'subscription_id':sid})
            assert response.status_code==200 and response.json()['total']==1
            assert 'private-pass' not in response.text and not store.resources('proxy')
            assert client.post(f'/api/proxy-subscriptions/{sid}/sync-start').status_code==202
            claim=app.state.subscription_scheduler.claim(sid)
            public=client.get('/api/proxy-subscriptions').json()['subscriptions'][0]
            assert public['data']['syncing'] is True and 'sync_lease' not in public['data']
            assert claim[0] not in json.dumps(public) and 'private-query' not in json.dumps(public)
    finally:store.engine.dispose()


def test_duplicate_source_create_or_edit_does_not_change_existing_content(network):
    store,client,_=network
    first=client.post('/api/proxy-subscriptions',json={'name':'source one','content':'http://127.0.0.1:18001#one'}).json()['id']
    second=client.post('/api/proxy-subscriptions',json={'name':'source two','content':'http://127.0.0.1:18002#two'}).json()['id']
    assert client.post('/api/proxy-subscriptions',json={'name':'duplicate','content':'http://127.0.0.1:18001#one'}).status_code==409
    before=store.resources('proxy')
    row=next(s for s in store.resources('proxy_subscription') if s['id']==second)
    assert client.put(f'/api/proxy-subscriptions/{second}',json={'expected_version':row['version'],'name':'must rollback','content':'http://127.0.0.1:18001#one'}).status_code==409
    assert store.resources('proxy')==before
    assert next(s for s in store.resources('proxy_subscription') if s['id']==second)['name']=='source two'
    remote={'name':'remote source','url':'https://feed.invalid/path?secret=private'}
    assert client.post('/api/proxy-subscriptions',json=remote).status_code==201
    assert client.post('/api/proxy-subscriptions',json=remote).status_code==409
    public=client.get('/api/proxy-subscriptions').text
    assert 'source_fingerprint' not in public and 'secret=private' not in public
