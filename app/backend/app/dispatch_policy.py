"""Platform-local selection preferences, following 3065 balance/added ordering.

Eligibility, atomic concurrency and conversation affinity stay authoritative.
"""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from .models import Account, Affinity, Resource
from .store import DomainError, PLATFORMS, uid

STRATEGIES = ('lru', 'balance_high', 'balance_low', 'added_new', 'added_old')
DEFAULTS = {'strategy': 'lru', 'queue_wait_seconds': 0, 'affinity_ttl_seconds': 3600}


def route_scope(session, platform, key_id, model, resolved_model):
    from .model_routes import route_scope as resolve
    return resolve(session, platform, key_id, model, resolved_model)


def selection_explanation(account, dispatch, scope, *, affinity=False, busy=0):
    """Snapshot only the actual selected candidate, never credentials/prompts.

    Invoke inside the same atomic admission transaction after slot recheck.
    This is evidence of a decision, not another eligibility implementation.
    """
    return {'strategy': dispatch['strategy'], 'reason': 'conversation_affinity' if affinity else dispatch['strategy'],
            'platform': account.platform, 'account_id': account.id,
            'group_id': scope.get('group_id'), 'route_version': scope.get('version', 0),
            'upstream_model': scope['upstream_model'], 'active_before': busy,
            'concurrency': account.concurrency, 'priority': account.priority, 'balance_observed_at': account.balance_at,
            'credential_version': account.credential_version}


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)
    strategy: Literal['lru', 'balance_high', 'balance_low', 'added_new', 'added_old']
    queue_wait_seconds: int = Field(default=0, ge=0, le=10, strict=True)
    affinity_ttl_seconds: int = Field(default=3600, ge=0, le=86400, strict=True)


def read(session, platform):
    row = session.scalar(select(Resource).where(Resource.kind == 'dispatch_policy', Resource.name == platform))
    return {**DEFAULTS, **(json.loads(row.data) if row else {})}


def ordering(session, platform):
    row = session.scalar(select(Resource).where(Resource.kind == 'dispatch_policy', Resource.name == platform))
    strategy = json.loads(row.data).get('strategy') if row else 'lru'
    primary = {
        # PostgreSQL puts NULL first for DESC, unlike SQLite. Unknown balances
        # remain eligible but must not outrank a measured balance in either mode.
        'balance_high': Account.balance.desc().nulls_last(), 'balance_low': Account.balance.asc().nulls_last(),
        'added_new': Account.created_at.desc(), 'added_old': Account.created_at.asc(),
    }.get(strategy)
    # Only callers' ready candidates enter this query. Unknown/stale balance
    # does not become an invented sort value or bypass credential verification.
    return [Account.priority.desc()] + ([primary] if primary is not None else []) + [Account.last_selected.asc(), Account.id.asc()]


def save(store, platform, body):
    if platform not in PLATFORMS:
        raise DomainError('unsupported_platform', 422)
    with store.tx() as (session, state):
        row = session.scalar(select(Resource).where(Resource.kind == 'dispatch_policy', Resource.name == platform))
        if body.expected_version != (row.version if row else 0):
            raise DomainError('resource_version_conflict')
        if row is None:
            row = Resource(id=uid(), kind='dispatch_policy', name=platform, version=0, data='{}', updated_at=store.clock())
            session.add(row)
        values = {**DEFAULTS, **json.loads(row.data)}
        previous_ttl = values['affinity_ttl_seconds']
        values.update(body.model_dump(exclude={'expected_version'}, exclude_unset=True))
        ttl = values['affinity_ttl_seconds']
        if ttl > 0 and (ttl < previous_ttl or previous_ttl == 0):
            # Apply the new cap at configuration time, not first reuse (which
            # could otherwise happen hours later and revive an old binding).
            # A shorter existing lifetime stays shorter; other platforms and
            # disabled-affinity history remain untouched.
            expiry_cap = store.clock() + ttl
            session.execute(update(Affinity).where(
                Affinity.account_id.in_(select(Account.id).where(Account.platform == platform)),
                Affinity.expires_at > expiry_cap,
            ).values(expires_at=expiry_cap))
        row.data = json.dumps(values)
        row.version += 1
        row.updated_at = store.clock()
        store.emit(session, state, 'dispatch_policy', 'configuration_saved')
        return row.id


def mount_dispatch_policies(app, store):
    @app.get('/api/dispatch-policies')
    def policies():
        rows = {r['name']: r for r in store.resources('dispatch_policy')}
        return {'policies': [{'platform': p, **DEFAULTS, **rows.get(p, {}).get('data', {}),
                              'version': rows.get(p, {}).get('version', 0)} for p in PLATFORMS]}

    @app.put('/api/dispatch-policies/{platform}')
    def put_policy(platform: str, body: PolicyInput):
        return {'id': save(store, platform, body)}
