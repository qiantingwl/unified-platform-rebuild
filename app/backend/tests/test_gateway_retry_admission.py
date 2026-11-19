import asyncio
import time

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.gateway import Gateway
from app.models import Lease, RequestAttempt
from app.store import DomainError, Store
from app.upstream import UpstreamError
from test_gateway import FakeRegistry, real_accounts


@pytest.fixture
def store(tmp_path):
    result = Store(f"sqlite:///{tmp_path/'retry.db'}", Fernet.generate_key())
    yield result
    result.engine.dispose()


def run(store, token, current, registry, **options):
    async def stream():
        return [item async for item in Gateway(store, registry, **options).frames(token, {"model": "model", "messages": [{"role": "user", "content": "synthetic"}]}, current)]
    return asyncio.run(stream())


@pytest.mark.parametrize("code,status,count", [("network", 502, 1), ("quota", 402, 2)])
def test_candidate_exhaustion_preserves_last_upstream_error_and_attempts(store, code, status, count):
    token = real_accounts(store, count)
    current = store.acquire(token, "model")
    registry = FakeRegistry([[UpstreamError(code, status, True)]] * count)
    result = run(store, token, current, registry)
    assert result[-1]["error"]["code"] == code and result[-1]["error"]["status"] == status
    assert len(registry.calls) == count
    log = store.snapshot()["requests"][0]
    assert log["status"] == "failed" and log["error_code"] == code
    with store.read() as (session, _):
        attempts = list(session.scalars(select(RequestAttempt)))
        assert len(attempts) == count and all(a.code == code and a.ended_at for a in attempts)
        assert not list(session.scalars(select(Lease)))


@pytest.mark.parametrize("code,status", [("invalid_api_key", 401), ("api_key_expired", 401), ("api_key_daily_limit", 429)])
def test_retry_admission_policy_errors_are_not_hidden(store, code, status):
    token = real_accounts(store, 1)
    current = store.acquire(token, "model")
    def fail(*args, **kwargs): raise DomainError(code, status)
    store.acquire = fail
    result = run(store, token, current, FakeRegistry([[UpstreamError("network", 502, True)]]))
    assert result[-1]["error"]["code"] == code and result[-1]["error"]["status"] == status


def test_deadline_exhausted_during_retry_admission_has_priority(store):
    token = real_accounts(store, 1)
    current = store.acquire(token, "model")
    def fail(*args, **kwargs):
        time.sleep(.12)
        raise DomainError("no_available_account", 503)
    store.acquire = fail
    result = run(store, token, current, FakeRegistry([[UpstreamError("network", 502, True)]]), startup=.08)
    assert result[-1]["error"]["code"] == "startup_timeout"


def test_initial_admission_without_candidates_stays_no_available_account(store):
    token = real_accounts(store, 0)
    with pytest.raises(DomainError) as failure:
        store.acquire(token, "model")
    assert failure.value.code == "no_available_account" and failure.value.status == 503


def test_max_attempts_keeps_last_failure_without_extra_admission(store):
    token = real_accounts(store, 3)
    current = store.acquire(token, "model")
    registry = FakeRegistry([[UpstreamError("quota", 402, True)], [UpstreamError("network", 502, True)]])
    result = run(store, token, current, registry, max_attempts=2)
    assert result[-1]["error"]["code"] == "network" and len(registry.calls) == 2


def test_account_quota_requires_rejected_model_answer_after_positive_balance(store, tmp_path):
    from sqlalchemy import delete
    from types import SimpleNamespace
    from app.models import Job, ModelState
    from app.operations import Supervisor, DEFAULT_SETTINGS
    token=real_accounts(store,1)
    store.real_capabilities['Loomy'].add('verify')
    with store.tx() as (s,_):s.execute(delete(Job))
    current=store.acquire(token,'model')
    aid=current['account_id']
    run(store,token,current,FakeRegistry([[UpstreamError('quota',402,True)]]))
    # Account scheduling pause is separate from the last actual balance.
    assert store.snapshot()['accounts'][0]['balance']==200
    store.plan(aid,'balance')
    job=store.claim('balance',1)
    store.finish(job['id'],job['owner'],job['attempt'],True,balance=99)
    with pytest.raises(DomainError,match='no_available_account'):
        store.acquire(token,'model')
    with store.read() as (s,_):
        account=store.account(s,aid)
        _,blocked=store.routing_index([],s.scalars(select(ModelState)),store.clock())
        from collections import Counter
        assert store.reason(account,Counter(),blocked,'other')=='quota'
        assert account.enabled
    other=store.create_key('other','Loomy','other')['token']
    with pytest.raises(DomainError,match='no_available_account'):
        store.acquire(other,'other')
    snapshot=store.snapshot()['accounts'][0]
    assert snapshot['status']=='quota' and snapshot['model_blocks'][0]['scope']=='account'
    # A different model's success is not evidence the rejected model recovered.
    store.plan(aid,'verify','other')
    job=store.claim('other-review',1)
    store.finish(job['id'],job['owner'],job['attempt'],True,effective_answer=True)
    with pytest.raises(DomainError,match='no_available_account'):
        store.acquire(other,'other')
    # Existing recovery scheduler must pick up the quota block.
    with store.tx() as (s,_):
        s.scalar(select(ModelState).where(ModelState.account_id==aid)).review_at=store.clock()-1
    supervisor=Supervisor(store,SimpleNamespace(tasks=[],capacity=64),SimpleNamespace(),tmp_path)
    supervisor.schedule({**DEFAULT_SETTINGS,'recovery_tests':True})
    job=store.claim('review',1)
    assert job['kind']=='verify' and job['model']=='model'
    store.finish(job['id'],job['owner'],job['attempt'],True,effective_answer=False)
    with pytest.raises(DomainError,match='no_available_account'):
        store.acquire(token,'model')
    store.plan(aid,'verify','model')
    job=store.claim('review',1)
    store.finish(job['id'],job['owner'],job['attempt'],True,effective_answer=True)
    assert store.acquire(token,'model')['account_id']==aid


@pytest.mark.parametrize('flow,recovered',[
    ([{'_done':True}],False),
    ([{'choices':[{'delta':{'content':'answer'}}]}],False),
    ([UpstreamError('quota',402,True)],False),
    ([{'choices':[{'delta':{'content':'answer'}}]},{'_done':True}],True),
])
def test_account_quota_worker_requires_answer_and_normal_end(store,flow,recovered):
    from sqlalchemy import delete
    from app.models import Job
    from app.worker import MaintenanceWorker
    token=real_accounts(store,1)
    store.real_capabilities['Loomy'].add('verify')
    with store.tx() as (s,_):s.execute(delete(Job))
    current=store.acquire(token,'model')
    run(store,token,current,FakeRegistry([[UpstreamError('quota',402,True)]]))
    aid=current['account_id']
    store.plan(aid,'verify','model')
    job=store.claim('quota-review',1)
    asyncio.run(MaintenanceWorker(store,registry=FakeRegistry([flow])).execute(job))
    account=store.snapshot()['accounts'][0]
    assert account['enabled'] and (account['status']=='ready') is recovered
    if not recovered:
        assert account['status']=='quota'
        with pytest.raises(DomainError,match='no_available_account'):
            store.acquire(token,'model')


def test_account_quota_reason_precedes_model_permission_in_any_row_order(store):
    from collections import Counter
    from app.models import ModelState
    real_accounts(store,1)
    aid=store.snapshot()['accounts'][0]['id']
    store.block_model(aid,'other','permission',1,store.clock()+60)
    store.block_model(aid,'model','quota',1,store.clock()+60)
    with store.read() as (s,_):
        account=store.account(s,aid)
        blocks=list(s.scalars(select(ModelState).where(ModelState.account_id==aid)))
        for order in (blocks,list(reversed(blocks))):
            assert store.reason(account,Counter(),{aid:order},'other')=='quota'


@pytest.mark.parametrize('later_code',['network','rate_limit','permission'])
def test_late_inflight_failure_cannot_downgrade_account_quota(store,later_code):
    from app.models import Account
    token=real_accounts(store,1)
    aid=store.snapshot()['accounts'][0]['id']
    with store.tx() as (s,_):s.get(Account,aid).concurrency=2
    first,second=store.acquire(token,'model'),store.acquire(token,'model')
    store.fail_attempt(first,UpstreamError('quota',402,True))
    store.fail_attempt(second,UpstreamError(later_code,502,True))
    account=store.snapshot()['accounts'][0]
    assert account['status']=='quota' and account['model_blocks'][0]['reason']=='quota'
    with pytest.raises(DomainError,match='no_available_account'):
        store.acquire(token,'model')


def test_verify_started_before_new_quota_cannot_clear_that_new_failure(store):
    from sqlalchemy import delete
    from app.models import Account,Job
    token=real_accounts(store,1)
    store.real_capabilities['Loomy'].add('verify')
    aid=store.snapshot()['accounts'][0]['id']
    with store.tx() as (s,_):
        s.get(Account,aid).concurrency=2
        s.execute(delete(Job))
    first,second=store.acquire(token,'model'),store.acquire(token,'model')
    store.fail_attempt(first,UpstreamError('quota',402,True))
    store.plan(aid,'verify','model')
    job=store.claim('review',1)
    store.fail_attempt(second,UpstreamError('quota',402,True))
    store.finish(job['id'],job['owner'],job['attempt'],True,effective_answer=True)
    assert store.snapshot()['accounts'][0]['status']=='quota'
    store.plan(aid,'verify','model')
    fresh=store.claim('fresh-review',1)
    store.finish(fresh['id'],fresh['owner'],fresh['attempt'],True,effective_answer=True)
    assert store.snapshot()['accounts'][0]['status']=='ready'
