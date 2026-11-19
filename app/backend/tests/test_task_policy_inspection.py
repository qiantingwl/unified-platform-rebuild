"""Daily policy and official read-only benefit inspection, isolated from upstream."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import Account, Credential, Job, Receipt
from app.operations import DEFAULT_SETTINGS, SettingsInput, Supervisor
from app.store import Store, uid
from app.task_receipts import mount_task_receipts
from app.upstream import UpstreamError
from app.workbuddy_benefits import CST, inspect
from app.worker import MaintenanceWorker


@pytest.fixture
def env(tmp_path):
    now=[datetime(2026,11,18,8,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()]
    store=Store('sqlite:///'+str(tmp_path/'policies.db'),Fernet.generate_key(),clock=lambda:now[0])
    store.real_capabilities={'Loomy':{'balance','checkin'},'WorkBuddy 国内':{'benefits','balance'}}
    yield store,now
    store.engine.dispose()


def add_account(store, account_id='a', **values):
    with store.tx() as (session,_):
        session.add(Account(id=account_id,platform=values.pop('platform','Loomy'),external_id=account_id,
            label='isolated policy',sandbox=False,credential_version=1,credential_status='valid',
            balance=values.pop('balance',100),balance_version=1,balance_at=store.clock()-3600,
            created_at=store.clock(),updated_at=store.clock(),**values))


def schedule(store, **values):
    Supervisor(store,SimpleNamespace(capacity=8),SimpleNamespace(),'.').schedule({**DEFAULT_SETTINGS,**values})


def checkins(store):
    with store.read() as (session,_):
        return [(job.account_id,job.business_day) for job in session.scalars(select(Job).where(Job.kind=='checkin'))]


def test_automatic_checkin_respects_global_and_account_time_and_participation(env):
    store,now=env
    add_account(store,'inherited')
    add_account(store,'early',daily_checkin_minute=8*60)
    add_account(store,'late',daily_checkin_minute=10*60)
    add_account(store,'excluded',daily_checkin_enabled=False)
    schedule(store,daily_checkin=True,daily_checkin_minute=9*60)
    assert {aid for aid,_ in checkins(store)}=={'early'}
    now[0]+=60
    schedule(store,daily_checkin=True,daily_checkin_minute=9*60)
    assert {aid for aid,_ in checkins(store)}=={'early','inherited'}
    now[0]+=3600
    schedule(store,daily_checkin=True,daily_checkin_minute=9*60)
    schedule(store,daily_checkin=True,daily_checkin_minute=9*60)
    assert sorted(aid for aid,_ in checkins(store))==['early','inherited','late']


def test_manual_checkin_bypasses_daily_policy_without_changing_it(env):
    store,_=env
    add_account(store,daily_checkin_enabled=False,daily_checkin_minute=1439)
    assert store.schedule_today()==1
    assert store.schedule_today()==0
    with store.read() as (session,_):
        account=session.get(Account,'a')
        assert not account.daily_checkin_enabled and account.daily_checkin_minute==1439


def test_daily_policy_keeps_receipt_dedup_across_midnight(env):
    store,now=env
    add_account(store)
    now[0]+=60
    schedule(store,daily_checkin=True,daily_checkin_minute=540)
    job=store.claim('policy')
    store.finish(job['id'],job['owner'],job['attempt'],True)
    schedule(store,daily_checkin=True,daily_checkin_minute=540)
    assert len(checkins(store))==1
    now[0]+=15*3600  # Next Beijing day at 00:00, still before 09:00.
    schedule(store,daily_checkin=True,daily_checkin_minute=540)
    assert len(checkins(store))==1
    now[0]+=9*3600
    schedule(store,daily_checkin=True,daily_checkin_minute=540)
    assert sorted(day for _,day in checkins(store))==['2026-11-18','2026-11-19']
    with store.read() as (session,_):
        assert session.scalar(select(func.count()).select_from(Receipt))==1


def test_daily_optout_does_not_strand_periodic_balance(env):
    store,_=env
    add_account(store,daily_checkin_enabled=False,balance=0)
    schedule(store,daily_checkin=True,refresh_balances=True)
    with store.read() as (session,_):
        assert list(session.scalars(select(Job.kind)))==['balance']


@pytest.mark.parametrize('minute',[-1,1440])
def test_daily_time_rejects_out_of_range(minute):
    with pytest.raises(ValidationError):
        SettingsInput(expected_version=0,**{**DEFAULT_SETTINGS,'daily_checkin_minute':minute})


class InspectionAdapter:
    def __init__(self,unknown=False):
        self.c={'account':{}}
        self.calls=[]
        self.saved={'benefits':{'claimed':2,'job_id':'old-claim','needs_balance':True}}
        self.unknown=unknown
        self.t=self
        self.base='https://isolated.invalid'

    def headers(self):
        return {}

    async def close(self):
        pass

    async def ledger(self,key,value=None):
        if value is not None:
            self.saved[key]=value
        return self.saved.get(key)

    async def request(self,method,url,headers,body=None):
        self.calls.append((method,url))
        assert method=='GET', 'Read-only inspection must never send a mutation'
        if self.unknown:
            return {}
        if url.endswith('/streak'):
            return {'redemption_status':{'tier_7d_status':'available','tier_14d_status':'locked','tier_28d_status':'claimed'},'makeup_cards':{'balance':1}}
        if url.endswith('/heatmap'):
            yesterday=(datetime.now(CST)-timedelta(days=1)).date().isoformat()
            return {'cells':[{'date':yesterday,'score':0}]}
        if url.endswith('/buddy/info'):
            return {'buddy':{'id':1}}
        if url.endswith('/buddy/travel/status'):
            return {'state':'arrived','record_id':44}
        raise AssertionError('Unexpected official path')


def test_inspection_uses_only_get_and_keeps_claim_ledger_separate():
    adapter=InspectionAdapter()
    result=asyncio.run(inspect(adapter,'WorkBuddy 国内'))
    assert result['read_only'] and result['status']=='verified'
    assert all(method=='GET' for method,_ in adapter.calls)
    assert len(adapter.calls)==4
    assert adapter.saved['benefits']=={'claimed':2,'job_id':'old-claim','needs_balance':True}
    assert set(adapter.saved)=={'benefits','benefits_status'}
    assert [task['eligible'] for task in result['tasks']]==[True,False,False,True,True]


def test_inspection_unknown_fields_never_become_success_or_eligible():
    adapter=InspectionAdapter(unknown=True)
    result=asyncio.run(inspect(adapter,'WorkBuddy 国内'))
    assert result['status']=='unknown'
    assert all(task['eligible'] is None and task['status']=='unknown' for task in result['tasks'])
    assert result['error_code']=='benefits_incomplete'


@pytest.mark.parametrize('platform,profile',[('WorkBuddy 国际',{}),('WorkBuddy 国内',{'enterpriseId':'enterprise'})])
def test_inspection_rejects_unsupported_before_network(platform,profile):
    adapter=InspectionAdapter();adapter.c={'account':profile}
    with pytest.raises(UpstreamError):
        asyncio.run(inspect(adapter,platform))
    assert adapter.calls==[]


@pytest.mark.parametrize('unknown',[False,True])
def test_inspection_queue_worker_and_public_receipt_do_not_claim(env,monkeypatch,unknown):
    store,_=env
    add_account(store,platform='WorkBuddy 国内')
    with store.tx() as (session,_):
        session.add(Credential(id=uid(),account_id='a',version=1,ciphertext=store.cipher.encrypt(b'{"account":{},"session":"isolated"}').decode(),created_at=store.clock()))
    adapter=InspectionAdapter(unknown=unknown)
    app=FastAPI();mount_task_receipts(app,store,SimpleNamespace(enabled=True))
    client=TestClient(app)
    queued=client.post('/api/accounts/a/benefits/inspect')
    assert queued.status_code==201 and queued.json()['read_only']
    assert client.post('/api/accounts/a/benefits/inspect').json()['id']==queued.json()['id']
    job=store.claim('inspection')
    assert job['benefits_read_only'] and job['kind']=='benefits'
    monkeypatch.setattr('app.worker.resolve_maintenance_proxy',lambda *_:None)
    monkeypatch.setattr('app.proxy_pool.record_proxy_result',lambda *args,**kwargs:None)
    worker=MaintenanceWorker(store,registry=SimpleNamespace(make=lambda *args:adapter))
    asyncio.run(worker.execute_real(job))
    result=client.get('/api/accounts/a/benefits/status').json()
    assert result['read_only'] and result['status']==('unknown' if unknown else 'verified')
    assert result['credential_current'] and result['job_id']==job['id']
    assert client.get('/api/accounts/a/benefits').json()['status']=='not_started'
    with store.read() as (session,_):
        assert session.get(Job,job['id']).status==('failed' if unknown else 'succeeded')
        assert session.scalar(select(func.count()).select_from(Job))==1
    assert all(method=='GET' for method,_ in adapter.calls)
