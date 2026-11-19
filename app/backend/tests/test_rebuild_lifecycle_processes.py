"""P4 PostgreSQL evidence using separate interpreters and database connections."""
import json
import os
import selectors
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.models import Account, Attempt, Job, Lease, Observation, Receipt, Resource
from app.store import Store, day_at, uid
from app.native_lifecycle import record_native_login


WORKER = r'''
import json, os, sys
from app.store import Store, DomainError
config = json.loads(sys.stdin.readline())
now = [config['now']]
store = Store(config['url'], config['key'].encode(), clock=lambda:now[0])
store.real_capabilities = {'Loomy':{'chat','balance','verify','checkin','relogin','renew'}}
print(json.dumps({'ready':os.getpid()}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    now[0] = request.get('now', now[0])
    try:
        action = request['action']
        job = request.get('job')
        if action == 'claim':
            result = store.claim(request['owner'], capacity=request.get('capacity',8), lease_seconds=30)
        elif action == 'finish':
            result = store.finish(job['id'],job['owner'],job['attempt'],True,balance=request.get('balance'))
        elif action == 'ledger':
            result = store.operation_ledger(job['account_id'],'p4-receipt',{'confirmed':True},fence=job)
        elif action == 'candidate':
            result = bool(store.renewal_candidate(job,{'session':'isolated-candidate'}))
        elif action == 'candidate_read':
            result = bool(store.renewal_candidate(job))
        elif action == 'heartbeat':
            result = store.heartbeat_job(job)
        elif action == 'acquire':
            result = store.acquire(request['token'],'p4-model',excluded=request.get('excluded',[]))
        elif action == 'end_request':
            result = store.end_request(request['id'],'cancelled')
        elif action == 'hold_account':
            with store.tx() as (session,state):
                account = store.account(session,request['account_id'])
                account.label = 'P4 pending transaction'
                session.flush()
                print(json.dumps({'held':True}),flush=True)
                assert json.loads(sys.stdin.readline())['action']=='release'
                store.emit(session,state,'account','p4_checkpoint',account.id)
            result = 'released'
        else:
            raise AssertionError('unknown worker action')
        print(json.dumps({'result':result}),flush=True)
    except DomainError as error:
        print(json.dumps({'error':error.code}),flush=True)
store.engine.dispose()
'''


class Process:
    def __init__(self, config):
        self.process = subprocess.Popen([sys.executable, '-B', '-u', '-c', WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1',
                'PYTHONPATH':os.pathsep.join(str(path) for path in sys.path)})
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.send(config)
        try:
            self.pid = self.receive()['ready']
        except BaseException:
            self.close()
            raise

    def send(self, request):
        self.process.stdin.write(json.dumps(request)+'\n')
        self.process.stdin.flush()

    def receive(self):
        assert self.selector.select(40), 'Isolated worker did not respond within 40 seconds'
        line = self.process.stdout.readline()
        assert line, f'Isolated worker exited unexpectedly: {self.process.poll()}'
        return json.loads(line)

    def call(self, action, **request):
        self.send({'action':action, **request})
        return self.receive()

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.selector.close()


@pytest.fixture
def process_env(tmp_path):
    key = Fernet.generate_key()
    now = [1800000000.0]
    store = Store('sqlite:///'+str(tmp_path/'process.db'),key,clock=lambda:now[0])
    if store.engine.dialect.name != 'postgresql':
        store.engine.dispose()
        pytest.skip('Requires real PostgreSQL and separate OS processes')
    store.real_capabilities = {'Loomy':{'chat','balance','verify','checkin','relogin','renew'}}
    processes = []
    def spawn():
        process = Process({'url':store.engine.url.render_as_string(hide_password=False),
            'key':key.decode(),'now':now[0]})
        processes.append(process)
        assert process.pid != os.getpid()
        assert len({p.pid for p in processes}) == len(processes)
        return process
    try:
        yield store, now, spawn
    finally:
        for process in processes:
            process.close()
        store.engine.dispose()


def add_account(store, account_id):
    with store.tx() as (session,_):
        session.add(Account(id=account_id,platform='Loomy',external_id=account_id,
            label='P4 isolated',sandbox=False,enabled=True,credential_version=1,
            credential_status='valid',balance=100,balance_version=1,balance_at=store.clock(),
            concurrency=1,created_at=store.clock(),updated_at=store.clock()))


def add_job(store, account_id, kind='balance', priority=10):
    job_id = uid()
    with store.tx() as (session,_):
        session.add(Job(id=job_id,dedupe_key=job_id,account_id=account_id,kind=kind,
            business_day=day_at(store.clock()),credential_version=1,status='queued',
            priority=priority,available_at=store.clock(),created_at=store.clock()))
    return job_id


@pytest.mark.parametrize('same_account,capacity', [(True,8),(False,1)])
def test_process_claim_respects_account_and_global_capacity(process_env,same_account,capacity):
    store,_,spawn = process_env
    add_account(store,'a');add_account(store,'b')
    add_job(store,'a');add_job(store,'a' if same_account else 'b')
    processes = [spawn() for _ in range(3)]
    for number,process in enumerate(processes):
        process.send({'action':'claim','owner':f'process-{number}','capacity':capacity})
    results = [process.receive()['result'] for process in processes]
    assert len([result for result in results if result]) == 1, results
    with store.read() as (session,_):
        assert session.scalar(select(func.count()).select_from(Job).where(Job.status=='running')) == 1
        assert session.scalar(select(func.count()).select_from(Attempt)) == 1


def test_process_expired_read_reclaim_rejects_old_owner_and_attempt(process_env):
    store,now,spawn = process_env
    add_account(store,'a');job_id=add_job(store,'a')
    first,second=spawn(),spawn()
    old=first.call('claim',owner='old')['result']
    assert old['attempt']==1
    now[0]+=31
    current=second.call('claim',owner='new',now=now[0])['result']
    assert current['id']==job_id and current['attempt']==2
    for action in ('finish','ledger','heartbeat'):
        assert first.call(action,job=old,now=now[0],balance=999)=={'error':'stale_job_receipt'}
    assert second.call('finish',job=current,balance=42)=={'result':None}
    assert store.operation_ledger('a','p4-receipt') is None
    with store.read() as (session,_):
        assert session.get(Account,'a').balance==42
        assert session.get(Job,job_id).status=='succeeded'
        attempts=session.scalars(select(Attempt).where(Attempt.job_id==job_id).order_by(Attempt.number)).all()
        assert [(a.number,a.outcome) for a in attempts]==[(1,'executor_lost'),(2,'succeeded')]
        assert session.scalar(select(func.count()).select_from(Observation))==1


def test_process_expired_checkin_stays_uncertain_without_replay(process_env):
    store,now,spawn=process_env
    add_account(store,'a');job_id=add_job(store,'a','checkin')
    first,second=spawn(),spawn()
    old=first.call('claim',owner='old')['result']
    now[0]+=31
    assert second.call('claim',owner='new',now=now[0])=={'result':None}
    assert first.call('finish',job=old,now=now[0])=={'error':'stale_job_receipt'}
    with store.read() as (session,_):
        assert session.get(Job,job_id).status=='uncertain'
        assert session.get(Account,'a').checkin_day is None
        assert session.scalar(select(func.count()).select_from(Receipt))==0
        assert session.scalar(select(func.count()).select_from(Job))==1


@pytest.mark.parametrize('fence_change',['expired','credential'])
def test_process_renewal_candidate_rejects_late_results(process_env,fence_change):
    store,now,spawn=process_env
    add_account(store,'a')
    with store.tx() as (session,_):
        record_native_login(store,session,session.get(Account,'a'),'password',{'session':'isolated-official'})
    add_job(store,'a','renew')
    process=spawn();job=process.call('claim',owner='renewal')['result']
    if fence_change=='expired':
        now[0]+=31
    else:
        with store.tx() as (session,_):
            session.get(Account,'a').credential_version=2
    expected='stale_job_receipt' if fence_change=='expired' else 'credential_version_conflict'
    assert process.call('candidate',job=job,now=now[0])=={'error':expected}
    with store.read() as (session,_):
        assert session.scalar(select(func.count()).select_from(Resource).where(Resource.kind=='renewal_candidate'))==0


def test_process_issued_candidate_read_requires_live_lease(process_env):
    store,now,spawn=process_env
    add_account(store,'a')
    with store.tx() as (session,_):
        record_native_login(store,session,session.get(Account,'a'),'password',{'session':'isolated-official'})
    add_job(store,'a','renew')
    process=spawn();job=process.call('claim',owner='renewal')['result']
    assert process.call('candidate',job=job)=={'result':True}
    assert process.call('candidate_read',job=job)=={'result':True}
    now[0]+=31
    assert process.call('candidate_read',job=job,now=now[0])=={'error':'stale_job_receipt'}
    with store.read() as (session,_):
        assert session.scalar(select(func.count()).select_from(Resource).where(Resource.kind=='renewal_candidate'))==1


def test_process_stale_balance_cannot_replace_current_version(process_env):
    store,_,spawn=process_env
    add_account(store,'a');job_id=add_job(store,'a')
    process=spawn();job=process.call('claim',owner='old-version')['result']
    with store.tx() as (session,_):
        account=session.get(Account,'a')
        account.credential_version=2;account.balance=70;account.balance_version=2
    assert process.call('ledger',job=job)=={'error':'stale_job_receipt'}
    assert process.call('finish',job=job,balance=999)=={'result':None}
    with store.read() as (session,_):
        account=session.get(Account,'a');result=session.get(Job,job_id)
        assert (account.balance,account.balance_version)==(70,2)
        assert (result.status,result.result_code)==('superseded','stale_credential_result')
        assert session.scalar(select(func.count()).select_from(Observation))==0


def test_process_verify_and_forward_share_only_their_account_slot(process_env):
    store,_,spawn=process_env
    add_account(store,'a');add_account(store,'b')
    token=store.create_key('P4 isolated','Loomy','p4-model')['token']
    forward,maintenance=spawn(),spawn()
    request=forward.call('acquire',token=token,excluded=['b'])['result']
    assert request['account_id']=='a'
    verify_id=add_job(store,'a','verify',priority=1)
    with store.tx() as (session,_):
        session.add(Resource(id=uid(),kind='job_target',name=verify_id,data='{"model":"p4-model"}',updated_at=store.clock()))
    other_id=add_job(store,'b',priority=10)
    other=maintenance.call('claim',owner='maintenance')['result']
    assert other['id']==other_id
    assert maintenance.call('finish',job=other,balance=55)=={'result':None}
    assert maintenance.call('claim',owner='waiting')=={'result':None}
    assert forward.call('end_request',id=request['id'])=={'result':None}
    verify=maintenance.call('claim',owner='verify')['result']
    assert verify['id']==verify_id
    assert forward.call('acquire',token=token,excluded=['b'])=={'error':'no_available_account'}
    other_request=forward.call('acquire',token=token)['result']
    assert other_request['account_id']=='b'
    with store.read() as (session,_):
        assert session.scalar(select(func.count()).select_from(Lease).where(Lease.account_id=='a'))==1
        assert session.scalar(select(func.count()).select_from(Lease).where(Lease.account_id=='b'))==1


def test_process_held_maintenance_account_does_not_block_other_forward(process_env):
    store,_,spawn=process_env
    add_account(store,'a');add_account(store,'b')
    token=store.create_key('P4 isolated','Loomy','p4-model')['token']
    holder,forward=spawn(),spawn()
    assert holder.call('hold_account',account_id='a')=={'held':True}
    try:
        # The maintenance transaction remains open while the other OS process
        # commits forwarding on b. No sleep or production transport is involved.
        result=forward.call('acquire',token=token)['result']
        assert result['account_id']=='b'
        assert holder.process.poll() is None
    finally:
        assert holder.call('release')=={'result':'released'}
