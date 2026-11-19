"""Optional task-only egress; forwarding and login-session bindings stay separate."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from .models import Resource
from .store import DomainError, PLATFORMS, uid

KINDS = ('balance', 'checkin', 'renew', 'onboarding', 'invitation', 'benefits')
RESOURCE_KIND = 'maintenance_proxy_route'


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['inherit', 'direct', 'fixed', 'pool'] = 'inherit'
    proxy_id: str | None = None
    pool_id: str | None = None

    @model_validator(mode='after')
    def matching_target(self):
        if self.mode == 'fixed':
            valid = bool(self.proxy_id) and self.pool_id is None
        elif self.mode == 'pool':
            valid = bool(self.pool_id) and self.proxy_id is None
        else:
            valid = self.proxy_id is None and self.pool_id is None
        if not valid:
            raise ValueError('route_target_mismatch')
        return self


class RouteInput(RouteConfig):
    expected_version: int = Field(ge=0)


def _check(platform, kind):
    if platform not in PLATFORMS:
        raise DomainError('unsupported_platform', 422)
    if kind not in KINDS:
        raise DomainError('unsupported_maintenance_task', 422)


def _decode(row):
    if row is None:
        return RouteConfig().model_dump(exclude_none=True)
    try:
        return RouteConfig.model_validate(json.loads(row.data)).model_dump(exclude_none=True)
    except (ValueError, TypeError, ValidationError):
        raise DomainError('maintenance_proxy_route_invalid', 503) from None


def resolve_maintenance_proxy(store, account_id, kind):
    # A successful recovery chat must validate the forwarding route. A healthy
    # maintenance-only egress must never lift model restrictions on a broken one.
    if kind == 'verify':
        return store.account_proxy(account_id)
    if kind not in KINDS:
        raise DomainError('unsupported_maintenance_task', 422)
    with store.read() as (session, _):
        account = store.account(session, account_id)
        row = session.scalar(select(Resource).where(
            Resource.kind == RESOURCE_KIND,
            Resource.name == f'{account.platform}:{kind}',
        ))
        route = _decode(row)
    if route['mode'] == 'inherit':
        return store.account_proxy(account_id)
    from .proxy_pool import resolve_explicit_proxy
    return resolve_explicit_proxy(store, account_id, route, affinity_scope='maintenance:' + kind)


def routes(store):
    with store.read() as (session, _):
        rows = {r.name: r for r in session.scalars(select(Resource).where(Resource.kind == RESOURCE_KIND))}
        return {'routes': [dict(platform=platform, kind=kind, version=(row.version if row else 0), **_decode(row))
                           for platform in PLATFORMS for kind in KINDS
                           for row in [rows.get(f'{platform}:{kind}')]],
                'verify_uses_forwarding': True, 'login_uses_session_proxy': True}


def save_route(store, platform, kind, body):
    _check(platform, kind)
    config = body.model_dump(exclude={'expected_version'}, exclude_none=True)
    with store.tx() as (session, state):
        row = session.scalar(select(Resource).where(Resource.kind == RESOURCE_KIND, Resource.name == f'{platform}:{kind}'))
        if body.expected_version != (row.version if row else 0):
            raise DomainError('resource_version_conflict')
        if body.mode == 'fixed':
            from .proxy_pool import _usable
            if not _usable(session.get(Resource, body.proxy_id)):
                raise DomainError('proxy_unavailable', 422)
        elif body.mode == 'pool':
            pool = session.get(Resource, body.pool_id)
            if pool is None or pool.kind != 'proxy_pool':
                raise DomainError('proxy_pool_missing', 422)
        if row is None:
            row = Resource(id=uid(), kind=RESOURCE_KIND, name=f'{platform}:{kind}', version=0, data='{}', updated_at=store.clock())
            session.add(row)
        row.data, row.version, row.updated_at = json.dumps(config), row.version + 1, store.clock()
        store.emit(session, state, RESOURCE_KIND, 'configuration_saved')
        return {'id': row.id, 'platform': platform, 'kind': kind, 'version': row.version, **config}


def mount_maintenance_proxy(app, store):
    @app.get('/api/maintenance-proxy-routes')
    def get_routes():
        return routes(store)

    @app.put('/api/maintenance-proxy-routes/{platform}/{kind}')
    def put_route(platform: str, kind: str, body: RouteInput):
        return save_route(store, platform, kind, body)
