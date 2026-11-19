"""A post-checkin observation covers old import queries, not newer requested work."""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Account, Job, Observation
from app.store import Store, uid


@pytest.fixture
def env(tmp_path):
    now = [1794900000.]
    store = Store(f"sqlite:///{tmp_path/'balance.db'}", Fernet.generate_key(), clock=lambda: now[0])
    aid = store.create_account("Loomy", "synthetic", "synthetic", sandbox=True)
    with store.tx() as (s, _):
        account = s.get(Account, aid)
        account.credential_version, account.credential_status = 1, "valid"
    yield store, aid, now
    store.engine.dispose()


def queue(store, aid, suffix, **patch):
    with store.tx() as (s, _):
        job = store.new_job(s, s.get(Account, aid), "balance", suffix)
        for key, value in patch.items():
            setattr(job, key, value)
        return job.id


def states(store):
    with store.read() as (s, _):
        return {j.id: (j.status, j.result_code) for j in s.scalars(select(Job))}


def test_success_covers_only_older_same_version_queued_queries(env):
    store, aid, now = env
    migration = queue(store, aid, "migration:1")
    old_manual = queue(store, aid, "manual:old")
    auth_review = queue(store, aid, "auth_review:1:old")
    old_version = queue(store, aid, "migration:old-version", credential_version=0)
    delayed = queue(store, aid, "after_checkin:delayed", available_at=now[0]+300)
    now[0] += 10
    successful = queue(store, aid, "after_checkin:today", priority=1)
    claimed = store.claim("worker", 8)
    assert claimed["id"] == successful
    now[0] += 1
    new_manual = queue(store, aid, "manual:new")
    # Running rows are never rewritten by query deduplication.
    other_running = queue(store, aid, "manual:running", status="running", owner="other", lease_until=now[0]+60)
    now[0] += 1
    store.finish(claimed["id"], claimed["owner"], claimed["attempt"], True, balance=123)
    result = states(store)
    assert result[migration] == result[old_manual] == ("superseded", "balance_already_verified")
    for kept in (auth_review, old_version, delayed, new_manual):
        assert result[kept][0] == "queued"
    assert result[other_running][0] == "running"
    assert result[successful] == ("succeeded", "confirmed")


def test_stale_success_does_not_cover_any_queued_query(env):
    store, aid, now = env
    old = queue(store, aid, "migration:1")
    now[0] += 10
    queued = queue(store, aid, "after_checkin:today", priority=1)
    claimed = store.claim("worker", 8)
    with store.tx() as (s, _):
        s.get(Account, aid).credential_version = 2
    now[0] += 1
    store.finish(queued, claimed["owner"], claimed["attempt"], True, balance=123)
    assert states(store)[old] == ("queued", None)
    assert states(store)[queued] == ("superseded", "stale_credential_result")


def add_confirmed_observation(store, aid, now, amount=100):
    with store.tx() as (s, _):
        account = s.get(Account, aid)
        account.balance, account.balance_at, account.balance_version = amount, now, account.credential_version
        s.add(Observation(id=uid(), account_id=aid, job_id=uid(), credential_version=account.credential_version, amount=amount, created_at=now))


@pytest.mark.parametrize("status", ["pending", "suspect", "invalid"])
def test_reconcile_never_skips_credential_review_states(env, status):
    store, aid, now = env
    old = queue(store, aid, "migration:1")
    now[0] += 10
    add_confirmed_observation(store, aid, now[0])
    with store.tx() as (s, _):
        s.get(Account, aid).credential_status = status
    assert store.reconcile_migration_balances(apply=True)["superseded"] == 0
    assert states(store)[old][0] == "queued"


def test_reconcile_is_previewable_idempotent_and_migration_only(env):
    store, aid, now = env
    old = queue(store, aid, "migration:1")
    manual = queue(store, aid, "manual:earlier")
    auth = queue(store, aid, "auth_review:1:earlier")
    running = queue(store, aid, "migration:running", status="running", owner="worker", lease_until=now[0]+60)
    different_version = queue(store, aid, "migration:older-credential", credential_version=0)
    delayed = queue(store, aid, "migration:retry", available_at=now[0]+300)
    now[0] += 10
    add_confirmed_observation(store, aid, now[0], amount=0)
    now[0] += 1
    newer = queue(store, aid, "migration:newer")
    assert store.reconcile_migration_balances()["eligible"] == 1
    assert states(store)[old][0] == "queued"
    assert store.reconcile_migration_balances(apply=True)["superseded"] == 1
    assert store.reconcile_migration_balances(apply=True)["superseded"] == 0
    result = states(store)
    assert result[old] == ("superseded", "balance_already_verified")
    assert result[running][0] == "running"
    assert all(result[j][0] == "queued" for j in (manual, auth, different_version, delayed, newer))
    with store.read() as (s, _):
        account = s.get(Account, aid)
        assert account.enabled and account.concurrency == 1 and account.balance == 0


def test_source_balance_without_real_observation_cannot_cover_queue(env):
    store, aid, now = env
    old = queue(store, aid, "migration:1")
    now[0] += 10
    with store.tx() as (s, _):
        account = s.get(Account, aid)
        account.balance, account.balance_at, account.balance_version = 100, now[0], 1
    assert store.reconcile_migration_balances(apply=True)["superseded"] == 0
    assert states(store)[old][0] == "queued"


def test_different_observation_credential_cannot_cover_queue(env):
    store, aid, now = env
    old = queue(store, aid, "migration:1")
    now[0] += 10
    add_confirmed_observation(store, aid, now[0])
    with store.tx() as (s, _):
        s.scalar(select(Observation)).credential_version = 2
    assert store.reconcile_migration_balances(apply=True)["superseded"] == 0
    assert states(store)[old][0] == "queued"
