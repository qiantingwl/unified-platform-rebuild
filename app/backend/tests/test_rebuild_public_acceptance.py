import json
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.store import Store,uid
from app.main import create_app
from app.models import Account,Job,RequestLog,ModelState

def setup(tmp_path,n=121):
 s=Store('sqlite:///'+str(tmp_path/'state.db'),Fernet.generate_key(),clock=lambda:1800000000)
 app=create_app(store=s,start_worker=False,admin_token='')
 s.real_capabilities={'Loomy':{'chat','balance','checkin','verify'}}
 s.create_key('目录','Loomy','model-a')
 with s.tx() as (db,state):
  for i in range(n):db.add(Account(id=f'account-{i:04}',platform='Loomy',external_id=f'identity-{i:04}',label='同名账户',original_label='官方名称' if i==1 else None,sandbox=False,enabled=i%5!=0,credential_version=1,credential_status='valid',balance=None if i%7==0 else i,balance_at=s.clock(),balance_version=1,created_at=i,updated_at=s.clock()))
  s.emit(db,state,'account','seeded')
 return s,TestClient(app,headers={'x-console-client':'unified-local'})

def test_directory_pagination_filters_and_totals(tmp_path):
 s,c=setup(tmp_path);r=c.get('/api/accounts?page=2&page_size=25').json()
 assert r['total']==121 and len(r['items'])==25
 assert r['summary']['total']==sum(r['status_counts'].values())
 assert r['items'][0]['id']=='account-0025' and 'external_id' not in r['items'][0]
 assert c.get('/api/accounts?q=identity-0001').json()['total']==1
 assert c.get('/api/accounts?q=官方名称').json()['total']==1
 for status in ('ready','manually_disabled','balance_pending'):
  data=c.get('/api/accounts',params={'status':status}).json()
  assert data['total']==r['status_counts'].get(status,0) and all(x['status']==status for x in data['items'])
 for state in ('verified','unknown','stale','failed'):
  data=c.get('/api/accounts',params={'balance_state':state}).json();assert all(x['balance_state']==state for x in data['items'])
 s.engine.dispose()

def test_directory_clamps_outdated_page_and_preserves_matching_rows(tmp_path):
 s,c=setup(tmp_path)
 result=c.get('/api/accounts',params={'page':500,'page_size':25,'q':'官方名称'}).json()
 assert result['page']==1 and result['total']==1
 assert [row['id'] for row in result['items']]==['account-0001']
 empty=c.get('/api/accounts',params={'page':500,'q':'no-matching-account'}).json()
 assert empty['page']==1 and empty['total']==0 and empty['items']==[]
 last=c.get('/api/accounts',params={'page':500,'page_size':25}).json()
 assert last['page']==5 and len(last['items'])==21
 s.engine.dispose()


def test_balance_directory_fences_old_credential_results_and_orders_current_jobs(tmp_path):
 s,c=setup(tmp_path,2)
 with s.tx() as (db,state):
  a=s.account(db,'account-0001');a.credential_version=2;a.balance_version=2
  now=s.clock()
  for name,version,result,finished,rev in [('older-failure',2,'failed',now-1,1),('current-success',2,'succeeded',now,2),('late-old-credential',1,'failed',now+1,3)]:
   db.add(Job(id=name,dedupe_key=name,account_id=a.id,kind='balance',business_day='2027-01-15',
    credential_version=version,status=result,priority=1,available_at=now-2,created_at=now-2,
    finished_at=finished,finished_revision=rev,error_code='network' if result=='failed' else None))
 row=c.get('/api/accounts/account-0001').json()
 assert row['balance_state']=='verified' and row['balance_error'] is None
 with s.tx() as (db,state):
  db.add(Job(id='current-failure',dedupe_key='current-failure',account_id='account-0001',kind='balance',
   business_day='2027-01-15',credential_version=2,status='failed',priority=1,available_at=now-2,
   created_at=now-2,finished_at=now,finished_revision=3,error_code='timeout'))
 result=c.get('/api/accounts',params={'balance_state':'failed'}).json()
 assert result['total']==1 and result['items'][0]['balance_error']=='timeout'
 s.engine.dispose()


def test_snapshot_light_and_overview_agrees(tmp_path):
 s,c=setup(tmp_path);r=c.get('/api/snapshot');assert r.status_code==200,r.text
 data=r.json();assert data['accounts']==[] and data['keys']==[] and data['events']==[] and data['totals']['total']==121
 overview=c.get('/api/overview').json()['platform_summaries']
 assert sum(x['total'] for x in overview)==121 and sum(x['ready'] for x in overview)==data['totals']['ready']
 s.engine.dispose()

def test_key_disable_enable_and_permanent_revoke(tmp_path):
 s,c=setup(tmp_path,2);key=s.create_key('Key','Loomy','model-a')
 assert c.patch('/api/keys/'+key['id'],json={'enabled':False}).status_code==200
 assert c.get('/api/keys?status=disabled').json()['total']==1
 assert c.patch('/api/keys/'+key['id'],json={'enabled':True}).status_code==200
 assert c.delete('/api/keys/'+key['id']).status_code==200
 assert c.patch('/api/keys/'+key['id'],json={'enabled':True}).json()['error']['code']=='key_revoked'
 row=c.get('/api/keys?status=revoked').json()['items'][0]
 assert row['revoked_at'] is not None and 'digest' not in row and 'token' not in row
 s.engine.dispose()

def test_reporting_missing_usage_status_and_scope(tmp_path):
 s,c=setup(tmp_path,2)
 with s.tx() as (db,state):
  for i,status in enumerate(['succeeded','failed','cancelled','inflight']):db.add(RequestLog(id='request-'+str(i),platform='Loomy',model='model-a',account_id='account-0001',started_at=100+i,ended_at=None if status=='inflight' else 110+i,status=status,input_tokens=10 if i<2 else None,cached_tokens=0 if i==0 else None,output_tokens=2 if i<2 else None,total_tokens=12 if i<2 else None))
 summary=c.get('/api/analytics?start=90&end=120').json()['summary']
 assert summary['total']==4 and summary['inflight']==1 and summary['failed']==1 and summary['input_tokens']==20 and summary['total_tokens']==24
 assert summary['cache_reported_requests']==1 and summary['cache_missing_requests']==3 and summary['cached_tokens']==0 and summary['cache_input_tokens']==10
 assert c.get('/api/analytics?start=90&end=120&basis=end').json()['summary']['total']==3
 assert c.get('/api/analytics?start=90&end=120&account_id=missing').json()['summary']['input_tokens'] is None
 assert c.get('/api/requests?start=90&end=120&model=missing').json()['total']==0
 s.engine.dispose()

def test_model_specific_restriction_in_directory(tmp_path):
 s,c=setup(tmp_path,2)
 with s.tx() as (db,state):db.add(ModelState(id=uid(),account_id='account-0001',model='model-a',reason='permission',credential_version=1,review_at=s.clock()+100))
 r=c.get('/api/accounts/account-0001').json();assert r['status']=='permission' and len(r['model_blocks'])==1
 s.engine.dispose()

def test_safe_identity_and_account_settings(tmp_path):
 s,c=setup(tmp_path,2)
 with s.tx() as (db,state):a=s.account(db,'account-0001');a.label='迁入验收';a.original_label=None
 r=c.get('/api/accounts/account-0001').json();assert '迁入验收' not in r['label'] and r['original_label'] is None
 r=c.patch('/api/accounts/account-0001/settings',json={'priority':7,'balance_interval_seconds':180});assert r.status_code==200,r.text
 r=c.get('/api/accounts/account-0001').json();assert r['priority']==7 and r['balance_interval_seconds']==180
 c.patch('/api/accounts/account-0001/settings',json={'reset_balance_interval':True});assert c.get('/api/accounts/account-0001').json()['balance_interval_seconds'] is None
 s.engine.dispose()

def test_revision_and_request_event_correlation(tmp_path):
 s,c=setup(tmp_path,2);before=s.revision()
 with s.tx(scope=None) as (db,state):
  s.emit(db,state,'request','started','account-0001',request_id='request-id');s.emit(db,state,'request','ended','account-0001',request_id='request-id')
 assert s.revision()==before+2
 data=c.get('/api/system-events?component=request').json();assert data['total']==2 and all(x['request_id']=='request-id' and x['level'] is None for x in data['items'])
 s.engine.dispose()

def test_postgres_account_slot_is_shared_across_api_keys(tmp_path):
 import pytest,threading
 from concurrent.futures import ThreadPoolExecutor
 s,c=setup(tmp_path,2)
 if s.engine.dialect.name!='postgresql':pytest.skip('PostgreSQL row-lock evidence only')
 keys=[s.create_key('parallel-'+str(i),'Loomy','model-a')['token'] for i in range(12)]
 barrier=threading.Barrier(len(keys))
 def run(token):
  from app.store import DomainError
  barrier.wait()
  try:return s.acquire(token,'model-a')
  except DomainError as e:return e.code
 with ThreadPoolExecutor(max_workers=len(keys)) as pool:results=list(pool.map(run,keys))
 successes=[r for r in results if isinstance(r,dict)]
 assert len(successes)==1,results
 from sqlalchemy import select,func
 from app.models import Lease
 with s.read() as (db,_):assert db.scalar(select(func.count()).select_from(Lease))==1
 s.engine.dispose()


def test_postgres_maintenance_scan_does_not_hold_global_state_lock(tmp_path):
 import pytest,threading,time
 from concurrent.futures import ThreadPoolExecutor
 s,c=setup(tmp_path,3)
 if s.engine.dialect.name!='postgresql':pytest.skip('PostgreSQL independent writer evidence only')
 token=s.create_key('parallel','Loomy','model-a')['token']
 held=threading.Event();release=threading.Event()
 def maintenance():
  with s.tx() as (db,state):
   a=s.account(db,'account-0001');a.updated_at=s.clock()+1
   held.set();assert release.wait(5)
   s.emit(db,state,'account','maintenance_checkpoint',a.id)
 with ThreadPoolExecutor(max_workers=2) as pool:
  m=pool.submit(maintenance);assert held.wait(2)
  try:
   request=pool.submit(s.acquire,token,'model-a');current=request.result(timeout=2)
   assert current['account_id']=='account-0002'
  finally:release.set()
  m.result(timeout=3)
 s.engine.dispose()

def test_password_admin_login_is_real_and_session_revocable(tmp_path,monkeypatch):
 monkeypatch.setenv('UNIFIED_ADMIN_USERNAME','fixture-admin')
 monkeypatch.setenv('UNIFIED_ADMIN_PASSWORD','fixture-password-private')
 s=Store('sqlite:///'+str(tmp_path/'auth.db'),Fernet.generate_key())
 c=TestClient(create_app(store=s,start_worker=False,admin_token='fixture-management-token'),headers={'x-console-client':'unified-local'})
 assert c.get('/api/session/config').json()=={'mode':'password'}
 assert c.post('/api/session',json={'username':'wrong','password':'fixture-password-private'}).status_code==401
 assert c.post('/api/session',json={'token':'fixture-management-token'}).status_code==401
 assert c.post('/api/session',json={'username':'fixture-admin','password':'fixture-password-private'}).status_code==200
 assert c.get('/api/accounts').status_code==200
 assert c.delete('/api/session').status_code==200
 assert c.get('/api/accounts').status_code==401
 s.engine.dispose()


def test_all_time_reports_token_ranking_and_bounded_trend(tmp_path):
 s,c=setup(tmp_path,3)
 with s.tx() as (db,state):
  for i,(account,tokens,stamp) in enumerate([('account-0001',2,100),('account-0001',3,200),('account-0002',900,s.clock()-86400),('account-0002',100,s.clock()-1)]):
   db.add(RequestLog(id='all-'+str(i),platform='Loomy',model='model-a',account_id=account,started_at=stamp,ended_at=stamp+1,status='succeeded',input_tokens=tokens,output_tokens=0))
 data=c.get('/api/analytics',params={'start':0,'end':s.clock()+1,'all_time':'true','top_limit':1}).json()
 assert data['summary']['total_tokens']==1005 and data['top_accounts'][0]['account_id']=='account-0002'
 assert len(data['top_accounts'])==1 and len(data['trend'])<=400
 assert data['summary']['current_streak']==2 and data['summary']['top_model']['name']=='model-a'
 assert c.get('/api/analytics',params={'start':0,'end':s.clock()+1}).status_code==422
 s.engine.dispose()

def test_report_source_paging_cache_windows_and_key_drilldown(tmp_path):
 s,c=setup(tmp_path,2)
 keys=[s.create_key('source-key-'+str(i),'Loomy','model-a')['id'] for i in range(31)]
 with s.tx() as (db,state):
  for i,key in enumerate(keys):
   db.add(RequestLog(id='source-'+str(i),platform='Loomy',model='model-a',account_id='account-0001',key_id=key,client_source='codex' if i%2 else None,started_at=s.clock()-i*60,ended_at=s.clock()-i*60+1,status='succeeded',input_tokens=100,output_tokens=10,cached_tokens=0 if i==0 else 50,total_tokens=110))
 params={'start':s.clock()-86400,'end':s.clock()+1}
 data=c.get('/api/analytics',params=params).json()
 assert len(data['key_distribution'])==20 and data['summary']['total']==31
 assert data['cache_windows'][0]['reported_requests']==31 and data['cache_windows'][0]['cached_tokens']==1500
 assert sum(r['total'] for r in data['activity'])==31
 source=c.get('/api/analytics/sources',params={**params,'kind':'keys','page':2,'page_size':25}).json()
 assert source['total']==31 and len(source['items'])==6 and all('name' in r and 'token' not in r for r in source['items'])
 detail=c.get('/api/analytics',params={**params,'key_id':keys[1]}).json()
 assert detail['summary']['total']==1 and detail['client_distribution'][0]['name']=='codex'
 clients=c.get('/api/analytics/sources',params={**params,'kind':'clients'}).json()
 assert clients['total']==2 and {r['name'] for r in clients['items']}=={'codex','unknown'}
 s.engine.dispose()

def test_metadata_backfill_preserves_business_and_never_invents_identity(tmp_path):
 from app.metadata_backfill import backfill
 from app.models import Credential,Resource
 from sqlalchemy import select
 s,c=setup(tmp_path,3)
 with s.tx() as (db,state):
  a=s.account(db,'account-0002');a.original_label=None
  db.add(Credential(id=uid(),account_id=a.id,version=1,ciphertext=s.cipher.encrypt(json.dumps({'profile':{'displayName':'官方账户'}}).encode()).decode(),created_at=s.clock()))
  db.add(Resource(id=uid(),kind='migration_source',name=a.id,data=json.dumps({'credential_owner':'legacy'}),updated_at=s.clock()))
 before=c.get('/api/accounts/account-0002').json()
 dry=backfill(s);assert dry['identity_updated']==1
 with s.read() as (db,_):assert db.get(Account,'account-0002').original_label is None
 applied=backfill(s,True);assert applied['identity_updated']==1
 after=c.get('/api/accounts/account-0002').json()
 assert after['original_label']=='官方账户' and after['enabled']==before['enabled'] and after['concurrency']==before['concurrency']
 with s.read() as (db,_):
  control=json.loads(db.scalar(select(Resource).where(Resource.kind=='lifecycle_control',Resource.name=='account-0002')).data)
  assert control['state']=='blocked_shared_session'
 assert backfill(s,True)['identity_updated']==0
 s.engine.dispose()

def test_admin_can_configure_own_password_without_copying_legacy_credentials(tmp_path):
 from app.models import Resource
 from sqlalchemy import select
 s=Store('sqlite:///'+str(tmp_path/'auth-config.db'),Fernet.generate_key())
 app=create_app(store=s,start_worker=False,admin_token='fixture-token-long-enough')
 c=TestClient(app,headers={'x-console-client':'unified-local'})
 assert c.put('/api/admin/credentials',json={'username':'new-admin','password':'new-password-private'}).status_code==401
 assert c.post('/api/session',json={'token':'fixture-token-long-enough'}).status_code==200
 assert c.put('/api/admin/credentials',json={'username':'new-admin','password':'new-password-private'}).status_code==200
 assert c.get('/api/accounts').status_code==401
 assert c.get('/api/session/config').json()['mode']=='password'
 assert c.post('/api/session',json={'username':'new-admin','password':'new-password-private'}).status_code==200
 other=TestClient(create_app(store=s,start_worker=False,admin_token='fixture-token-long-enough'),headers={'x-console-client':'unified-local'})
 assert other.post('/api/session',json={'username':'new-admin','password':'new-password-private'}).status_code==200
 with s.read() as (db,_):
  row=db.scalar(select(Resource).where(Resource.kind=='admin_identity'))
  assert 'new-password-private' not in row.data+row.secret and 'new-admin' not in row.data
 s.engine.dispose()

def test_public_models_metadata_uses_authorized_alias_and_upstream_only(tmp_path):
 from app.models import Resource
 from sqlalchemy import select
 s,c=setup(tmp_path,2)
 key=s.create_key('metadata','Loomy','model-a')
 with s.tx() as (db,state):
  db.add(Resource(id=uid(),kind='model_catalog',name='Loomy',data=json.dumps({'observed_at':s.clock(),'models':['model-a','private-model'],'model_details':[{'id':'model-a','capabilities':{'tools':True,'vision':None},'context_window':12000,'reasoning':{'supported_efforts':['low','high']},'secret':'never-publish'},{'id':'private-model','capabilities':{'vision':True}}]}),updated_at=s.clock()))
 models=s.key_models(key['token'])
 assert len(models)==1 and models[0]['id']=='model-a' and models[0]['capabilities']=={'tools':True,'vision':None}
 assert models[0]['context_window']==12000 and 'never-publish' not in json.dumps(models)
 s.engine.dispose()

def test_directory_multiple_model_routes_and_groups_match_actual_eligibility(tmp_path):
 from app.models import Resource
 s,c=setup(tmp_path,3)
 s.create_key('second-model','Loomy','model-b')
 with s.tx() as (db,state):
  db.add(ModelState(id=uid(),account_id='account-0001',model='model-a',reason='permission',credential_version=1,review_at=s.clock()+100))
 assert c.get('/api/accounts/account-0001').json()['status']=='ready'
 with s.tx() as (db,state):
  db.add(ModelState(id=uid(),account_id='account-0001',model='model-b',reason='permission',credential_version=1,review_at=s.clock()+100))
 assert c.get('/api/accounts/account-0001').json()['status']=='permission'
 s.create_key('scoped-model','Loomy','model-c')
 with s.tx() as (db,state):
  db.add(Resource(id=uid(),kind='model_routes',name='Loomy',data=json.dumps({'groups':[{'id':'restricted','name':'restricted','account_ids':['account-0002']}],'mappings':[{'model':'model-c','upstream_model':'model-c','group_id':'restricted','enabled':True}]}),updated_at=s.clock()))
 assert c.get('/api/accounts/account-0001').json()['status']=='permission'
 assert c.get('/api/accounts/account-0002').json()['status']=='ready'
 with s.tx() as (db,state):
  db.add(ModelState(id=uid(),account_id='account-0001',model='*',reason='quota',credential_version=1,review_at=s.clock()+100))
 assert c.get('/api/accounts/account-0001').json()['status']=='quota'
 s.engine.dispose()


def test_safe_name_removes_only_provenance_prefix_without_inventing_identity():
 from app.native_identity import safe_label
 assert safe_label('小浣熊 迁入 RaccoonMason')=='RaccoonMason'
 assert safe_label('Loomy 导入 Existing Name')=='Existing Name'
 assert safe_label('Existing Name')=='Existing Name'
 assert safe_label('迁入验收') is None
