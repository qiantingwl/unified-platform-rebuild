"""6000-account offline routing/queue behavior, with non-flaky measurements.

Run with -s to collect timings and ORM load counts. These aren't production
throughput claims or hard performance thresholds.
"""
import json
import time
from collections import Counter
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, insert, select

from app.models import Account,Job,Lease,ModelState
from app.store import DomainError,Store,day_at


@contextmanager
def measured(store,label):
    counts=Counter()
    def statement(*_):counts["sql_calls"]+=1
    def load(target,_):counts[type(target).__name__+"_loaded"]+=1
    event.listen(store.engine,"before_cursor_execute",statement)
    for model in (Account,Job,Lease,ModelState):event.listen(model,"load",load)
    started=time.perf_counter()
    try:yield counts
    finally:
        counts["elapsed_ms"]=round((time.perf_counter()-started)*1000,2)
        event.remove(store.engine,"before_cursor_execute",statement)
        for model in (Account,Job,Lease,ModelState):event.remove(model,"load",load)
        print(json.dumps({"measurement":label,**counts},sort_keys=True))


@pytest.fixture
def large_store(tmp_path):
    now=1794801600.0
    store=Store(f"sqlite:///{tmp_path/'large.db'}",Fernet.generate_key(),clock=lambda:now)
    rows=[{"id":f"a-{i:04d}","platform":"Loomy","external_id":f"synthetic-{i}","label":"synthetic","sandbox":True,"enabled":i>=5998,"credential_version":1,"credential_status":"valid","balance":100.0,"balance_at":now,"balance_version":1,"concurrency":1,"created_at":now,"updated_at":now} for i in range(6000)]
    with store.tx() as (s,_):s.execute(insert(Account),rows)
    yield store
    store.engine.dispose()


def test_6000_accounts_affinity_busy_exclusion_and_release(large_store):
    store=large_store
    token=store.create_key("synthetic","Loomy","sandbox-echo")["token"]
    with measured(store,"6000-acquire-first"):
        first=store.acquire(token,"sandbox-echo",conversation="synthetic-conversation")
    assert first["account_id"]=="a-5998"
    with measured(store,"6000-acquire-sticky-busy-fallback"):
        second=store.acquire(token,"sandbox-echo",conversation="synthetic-conversation")
    assert second["account_id"]=="a-5999"
    with pytest.raises(DomainError) as full:store.acquire(token,"sandbox-echo")
    assert full.value.code=="no_available_account"
    store.end_request(first["id"],"failed","network")
    with pytest.raises(DomainError):store.acquire(token,"sandbox-echo",excluded=["a-5998"])
    with measured(store,"6000-acquire-released-slot"):
        third=store.acquire(token,"sandbox-echo",conversation="synthetic-conversation")
    assert third["account_id"]=="a-5998"
    with store.read() as (s,_):
        counts=Counter(s.scalars(select(Lease.account_id)))
        assert counts=={"a-5998":1,"a-5999":1}


def test_6000_jobs_claim_preserves_stale_events_and_no_page_limit_starvation(large_store):
    store=large_store;now=store.clock()
    rows=[{"id":f"j-{i:04d}","dedupe_key":f"synthetic-{i}","account_id":f"a-{i:04d}","kind":"balance","business_day":day_at(now),"credential_version":0 if i<130 else 1,"status":"queued","priority":20,"available_at":now,"created_at":now} for i in range(6000)]
    with store.tx() as (s,_):s.execute(insert(Job),rows)
    with measured(store,"6000-claim-after-130-stale"):
        job=store.claim("synthetic-worker")
    assert job["id"]=="j-0130"
    with store.read() as (s,_):
        assert len(list(s.scalars(select(Job.id).where(Job.status=="superseded"))))==130
        assert s.get(Job,"j-0131").status=="queued"
