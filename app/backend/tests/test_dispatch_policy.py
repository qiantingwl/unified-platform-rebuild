import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.dispatch_policy import PolicyInput, save
from app.models import Account
from app.store import DomainError, Store
from test_gateway import real_accounts


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'dispatch.db'}", Fernet.generate_key())
    token = real_accounts(store, 3)
    with store.tx() as (s, _):
        accounts = list(s.scalars(select(Account).order_by(Account.external_id)))
        for i, account in enumerate(accounts):
            account.balance = [100, 900, 500][i]
            account.created_at = 100 + i
            account.last_selected = [3, 2, 1][i]
        ids = [a.id for a in accounts]
    yield store, token, ids
    store.engine.dispose()


@pytest.mark.parametrize('strategy,index', [('lru',2),('balance_high',1),('balance_low',0),('added_new',2),('added_old',0)])
def test_platform_order_is_used_for_actual_selection(env, strategy, index):
    store, token, ids = env
    save(store, 'Loomy', PolicyInput(expected_version=0, strategy=strategy))
    selected = store.acquire(token, 'model')
    assert selected['account_id'] == ids[index]


def test_affinity_wins_and_busy_account_falls_back_without_overbooking(env):
    store, token, ids = env
    original = store.acquire(token, 'model', conversation='synthetic')
    assert original['account_id'] == ids[2]
    store.end_request(original['id'], 'cancelled', 'cancelled')
    save(store, 'Loomy', PolicyInput(expected_version=0, strategy='balance_high'))
    sticky = store.acquire(token, 'model', conversation='synthetic')
    assert sticky['account_id'] == ids[2]
    next_request = store.acquire(token, 'model', conversation='synthetic')
    assert next_request['account_id'] == ids[1]
    assert store.snapshot()['totals']['busy'] == 2


def test_unknown_balance_and_other_platform_configuration_do_not_override_eligibility(env):
    store, token, ids = env
    save(store, 'WPS 灵犀', PolicyInput(expected_version=0, strategy='balance_high'))
    assert store.acquire(token, 'model')['account_id'] == ids[2]
    save(store, 'Loomy', PolicyInput(expected_version=0, strategy='balance_high'))
    with store.tx() as (s, _):
        s.get(Account, ids[1]).balance_version = 0
    assert store.acquire(token, 'model')['account_id'] == ids[0]


def test_policy_update_cas_and_default_do_not_change_account_switches_or_capacity(env):
    store, _, _ = env
    before = [(r['id'],r['enabled'],r['concurrency']) for r in store.snapshot()['accounts']]
    save(store, 'Loomy', PolicyInput(expected_version=0, strategy='balance_high'))
    with pytest.raises(DomainError) as error:
        save(store, 'Loomy', PolicyInput(expected_version=0, strategy='added_old'))
    assert error.value.code == 'resource_version_conflict'
    save(store, 'Loomy', PolicyInput(expected_version=1, strategy='lru'))
    assert [(r['id'],r['enabled'],r['concurrency']) for r in store.snapshot()['accounts']] == before


@pytest.mark.parametrize('strategy,indices', [('balance_high',[1,2,0]),('balance_low',[2,1,0])])
def test_unknown_balance_sorts_last_on_sqlite_and_postgresql(env,strategy,indices):
    from app.dispatch_policy import ordering
    from sqlalchemy.dialects import postgresql
    store,_,ids=env
    save(store,'Loomy',PolicyInput(expected_version=0,strategy=strategy))
    with store.tx() as (session,_):
        session.get(Account,ids[0]).balance=None
    with store.read() as (session,_):
        statement=select(Account.id).where(Account.platform=='Loomy').order_by(*ordering(session,'Loomy'))
        assert list(session.scalars(statement))==[ids[i] for i in indices]
        assert 'NULLS LAST' in str(statement.compile(dialect=postgresql.dialect()))
