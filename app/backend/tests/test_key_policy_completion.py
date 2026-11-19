import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select,text
from app.store import Store,DomainError,uid
from app.models import Account,Resource,ModelState,RequestLog,Job,State
from app.key_policy import DEFAULT
from app.main import create_app
from test_gateway import real_accounts

@pytest.fixture
def context(tmp_path):
    now=[1794801600.0]
    key=Fernet.generate_key()
    store=Store(f'sqlite:///{tmp_path/"policy.db"}',key,clock=lambda:now[0])
    token=real_accounts(store,4)
    key_id=store.snapshot()['keys'][0]['id']
    yield store,token,key_id,now,key
    store.engine.dispose()

def put_policy(store,key_id,**values):
    with store.tx() as (s,_):
        r=s.scalar(select(Resource).where(Resource.kind=='key_policy',Resource.name==key_id))
        if r is None:
            r=Resource(id=uid(),kind='key_policy',name=key_id,data='{}',updated_at=store.clock());s.add(r)
        r.data=json.dumps({**DEFAULT,'models':['model'],**values})


@pytest.mark.parametrize('raw', ['broken', '[]', '{"rpm":-1}', '{"allowed_ips":[123]}', '{"models":null}', '{"expires_at":NaN}'])
def test_damaged_stored_policy_rejects_without_granting_defaults(context, raw):
    store,token,key_id,_,_=context
    put_policy(store,key_id)
    with store.tx() as (session,_):
        row=session.scalar(select(Resource).where(Resource.kind=='key_policy',Resource.name==key_id))
        row.data=raw
    with pytest.raises(DomainError,match='invalid_key_policy'):
        store.acquire(token,'model')
    with store.read() as (session,_):
        assert list(session.scalars(select(RequestLog)))==[]

def test_key_limits_atomic_and_opt_in(context):
    s,t,k,now,_=context
    put_policy(s,k,max_concurrency=2)
    def take(_):
        try:return s.acquire(t,'model')
        except DomainError as e:return e.code
    with ThreadPoolExecutor(max_workers=8) as pool:result=list(pool.map(take,range(20)))
    assert len([r for r in result if isinstance(r,dict)])==2
    assert result.count('api_key_concurrency')==18
    for r in result:
        if isinstance(r,dict):s.end_request(r['id'],'cancelled','cancelled')
    put_policy(s,k,rpm=2)
    with pytest.raises(DomainError,match='api_key_rate_limit'):s.acquire(t,'model')
    now[0]+=61
    assert s.acquire(t,'model')

def test_multi_model_alias_expiry_and_ip_scope(context):
    s,t,k,now,_=context
    put_policy(s,k,models=['model','other'],aliases={'friendly':'model'},allowed_ips=['192.0.2.0/24'])
    with pytest.raises(DomainError,match='api_key_ip_denied'):s.acquire(t,'friendly',client_ip='198.51.100.1')
    first=s.acquire(t,'friendly',client_ip='192.0.2.1')
    assert first['upstream_model']=='model'
    assert {v['id'] for v in s.key_models(t)}=={'model','other','friendly'}
    put_policy(s,k,expires_at=now[0]-1)
    with pytest.raises(DomainError,match='api_key_expired'):s.acquire(t,'model')

def test_expired_network_cannot_mask_identity_restriction(context):
    s,t,k,now,_=context
    with s.tx() as (db,_):
        account=db.scalar(select(Account))
        for model,reason,review_at in [('model','network',now[0]-1),('*','identity',now[0]+3600)]:
            db.add(ModelState(id=uid(),account_id=account.id,model=model,reason=reason,credential_version=1,review_at=review_at))
    row=next(a for a in s.snapshot()['accounts'] if a['id']==account.id)
    assert row['status']=='identity'

def test_manual_settings_wallet_and_policy_api(context):
    store,token,key_id,now,_=context
    client=TestClient(create_app(store,start_worker=False))
    h={'X-Console-Client':'unified-local'}
    aid=store.snapshot()['accounts'][0]['id']
    assert client.patch(f'/api/accounts/{aid}/settings',headers=h,json={'enabled':False,'concurrency':2}).status_code==200
    assert next(a for a in store.snapshot()['accounts'] if a['id']==aid)['enabled'] is False
    response=client.put(f'/api/keys/{key_id}/policy',headers=h,json={**DEFAULT,'models':['model','other']})
    assert response.status_code==200
    assert client.get(f'/api/keys/{key_id}/policy').json()['models']==['model','other']
    wallet=client.get(f'/api/accounts/{aid}/wallet').json()
    assert wallet['balance']==200 and wallet['observations']==[]

def test_usage_missing_not_zero_and_preserves_all_counters(context):
    s,t,k,now,_=context
    current=s.acquire(t,'model')
    s.record_usage(current['id'],{'completion_tokens':13,'total_tokens':113,'completion_tokens_details':{'reasoning_tokens':5}})
    log=s.snapshot()['requests'][0]
    assert log['input_tokens'] is None and log['cached_tokens'] is None
    assert (log['output_tokens'],log['total_tokens'],log['reasoning_tokens'],log['key_id'])==(13,113,5,k)

def test_existing_database_additive_migration(context):
    store,t,k,now,key=context
    with store.engine.begin() as c:
        c.execute(text('DROP INDEX IF EXISTS ix_request_logs_key_id'))
        for field in ('output_tokens','total_tokens','reasoning_tokens','key_id'):
            c.execute(text(f'ALTER TABLE request_logs DROP COLUMN {field}'))
    reopened=Store(store.engine.url.render_as_string(hide_password=False),key,clock=store.clock)
    try:
        assert len(reopened.snapshot()['accounts'])==4
        assert reopened.snapshot()['requests']==[]
    finally:reopened.engine.dispose()


def test_balance_failure_survives_midnight_and_matches_wallet(context):
    store,_,_,now,_=context
    aid=store.snapshot()['accounts'][0]['id']
    job=store.plan(aid,'balance')
    with store.tx() as (db,_):
        row=db.get(Job,job)
        row.status='failed';row.error_code='network';row.finished_at=now[0]
    now[0]+=86400
    assert next(a for a in store.snapshot()['accounts'] if a['id']==aid)['balance_error']=='network'
    client=TestClient(create_app(store,start_worker=False))
    assert client.get(f'/api/accounts/{aid}/wallet').json()['balance_status']=='query_failed'


def test_read_is_one_snapshot_across_concurrent_commits(context):
    store,_,_,_,_=context
    with store.read() as (db,state):
        old=state.revision
        # The writer commits while the reader is open; a second SELECT must
        # still belong to the revision exposed at the start of the response.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(store.create_account,'Loomy','new-identity','new account').result()
        db.expire_all()
        assert db.get(State,1).revision==old
        assert len(list(db.scalars(select(Account))))==4
    assert len(store.snapshot()['accounts'])==5


def test_long_input_accepted_without_trimming_and_configured_limit(context,monkeypatch):
    from test_gateway import FakeRegistry,delta
    store,token,_,_,_=context
    reg=FakeRegistry([[delta('ok'),{'_done':True}]])
    client=TestClient(create_app(store,start_worker=False,registry=reg))
    body={'model':'model','messages':[{'role':'user','content':'a'*(3*1024*1024)}]}
    headers={'Authorization':'Bearer '+token}
    response=client.post('/v1/chat/completions',headers=headers,json=body)
    assert response.status_code==200
    monkeypatch.setenv('UNIFIED_MAX_REQUEST_BYTES','1024')
    assert client.post('/v1/chat/completions',headers=headers,json=body).status_code==413
    assert len(reg.calls)==1
