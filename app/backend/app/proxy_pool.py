"""Persistent egress policy, account affinity and passive transport observations.

This module manages existing HTTP/SOCKS endpoints. It does not launch a proxy
kernel or turn a failed proxy configuration into an unrequested direct route.
"""
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, cast, func, or_, select, type_coerce

from .models import Account, Resource
from .store import DomainError, PLATFORMS, uid


# These report failed transport, not a judgement about account permissions.
EGRESS_FAILURES = {"network", "response_read_failed", "truncated_stream"}


def _row(session, kind, name):
    return session.scalar(select(Resource).where(Resource.kind == kind, Resource.name == name))


def _put(store, session, kind, name, data):
    row = _row(session, kind, name)
    if row is None:
        row = Resource(id=uid(), kind=kind, name=name, version=1, data="{}", updated_at=store.clock())
        session.add(row)
    else:
        row.version += 1
    row.data, row.updated_at = json.dumps(data), store.clock()
    return row


def _usable(row):
    return row is not None and row.kind == "proxy" and bool(row.secret) and json.loads(row.data).get("status") not in {"retired", "disabled"}


def _target_eligible(store, session, row, platform, checks=None, force=False):
    if not force and not json.loads(row.data).get("target_check_required"):
        return True
    name = f"{row.id}:{platform}"
    check = checks.get(name) if checks is not None else _row(session, "proxy_target_check", name)
    data = json.loads(check.data) if check else {}
    checked = data.get("checked_at")
    return (data.get("eligible") is True and data.get("status") == "completed"
            and data.get("node_version") == row.version and data.get("platform") == platform
            and type(checked) in {int, float} and 0 <= store.clock() - checked <= 86400)


def _target_checks(session, ids, platform):
    names = [f"{identity}:{platform}" for identity in ids]
    return {r.name: r for r in session.scalars(select(Resource).where(Resource.kind == "proxy_target_check", Resource.name.in_(names)))}


def _pool_nodes(session, pool):
    """Resolve dynamic subscription ownership without copying a stale ID list."""
    data=json.loads(pool.data)
    explicit=set(data.get("proxy_ids") or [])
    selected=set(data.get("subscription_ids") or [])
    sources={r.id for r in session.scalars(select(Resource).where(Resource.kind=="proxy_subscription",Resource.id.in_(selected)))
             if json.loads(r.data).get("enabled") is not False}
    filters=[Resource.id.in_(explicit)]
    filters.extend(Resource.data.contains(json.dumps(source),autoescape=True) for source in sources)
    candidates=session.scalars(select(Resource).where(Resource.kind=="proxy",or_(*filters)))
    result=[]
    for row in candidates:
        meta=json.loads(row.data)
        owners=set(meta.get("subscription_ids") or ([meta["subscription_id"]] if meta.get("subscription_id") else []))
        if row.id in explicit or owners & sources:
            result.append(row)
    return result


def _dynamic_member(pool,node):
    data=json.loads(node.data)
    owners=set(data.get("subscription_ids") or ([data["subscription_id"]] if data.get("subscription_id") else []))
    return bool(owners & set(json.loads(pool.data).get("subscription_ids") or []))


def _fixed_egress_held(store, session, row, platform, health=None):
    """Use connection evidence for this endpoint version, never account errors."""
    name = f"{platform}:{row.id}"
    if health is None:
        observation = _row(session, "proxy_health", name)
        value = json.loads(observation.data) if observation else {}
    else:
        value = health.get(name, {})
    return (value.get("node_version") == row.version
            and value.get("failure_phase") in {"connect", "proxy_connect"}
            and value.get("retry_at", 0) > store.clock())


def _endpoint(store, row, session=None, platform=None, *, fixed=False):
    if row is None or not row.secret or row.kind != "proxy":
        raise DomainError("missing_proxy", 503)
    if json.loads(row.data).get("status") == "retired":
        raise DomainError("proxy_retired", 503)
    if not _usable(row):
        raise DomainError("proxy_unavailable", 503)
    if session is not None and platform is not None and not _target_eligible(store, session, row, platform):
        raise DomainError("proxy_target_unverified", 503)
    if fixed and session is not None and platform is not None and _fixed_egress_held(store, session, row, platform):
        raise DomainError("proxy_unavailable", 503)
    return store.cipher.decrypt(row.secret.encode()).decode()


def resolve_account_proxy(store, account_id, model=None):
    """Return a secret URL or explicit/default direct None; never downgrade failures.

    Account fixed bindings outrank platform routes. Pool selection uses persisted
    account affinity; a stable hash balances new bindings without a central cursor.
    Only a pool is allowed to replace a failed node automatically.
    """
    return _resolve_proxy(store, account_id, model=model)


def resolve_explicit_proxy(store, account_id, route, *, affinity_scope):
    """Task-only opt-in route; keep task affinity separate from forwarding."""
    if not affinity_scope.startswith('maintenance:') or len(affinity_scope) > 60:
        raise DomainError('proxy_route_invalid', 503)
    route = RouteInput.model_validate(route).model_dump(exclude_none=True)
    return _resolve_proxy(store, account_id, explicit_route=route, affinity_scope=affinity_scope)


def _resolve_proxy(store, account_id, model=None, explicit_route=None, affinity_scope=None):
    affinity_update = None
    affinity_kind = 'maintenance_proxy_affinity' if affinity_scope else 'proxy_affinity'
    affinity_name = f'{affinity_scope}:{account_id}' if affinity_scope else account_id
    with store.read() as (s, _):
        account = store.account(s, account_id)
        binding = _row(s, "proxy_binding", account_id) if explicit_route is None else None
        if binding is not None:
            return _endpoint(store, s.get(Resource, json.loads(binding.data)["proxy_id"]), s, account.platform, fixed=True)
        route_row = _row(s, "proxy_route", account.platform) if explicit_route is None else None
        route = explicit_route if explicit_route is not None else json.loads(route_row.data) if route_row else {"mode": "direct"}
        mode = route.get("mode")
        if mode == "direct":
            return None
        if mode == "fixed":
            return _endpoint(store, s.get(Resource, route.get("proxy_id")), s, account.platform, fixed=True)
        if mode != "pool":
            raise DomainError("proxy_route_invalid", 503)
        pool = s.get(Resource, route.get("pool_id"))
        if not pool or pool.kind != "proxy_pool":
            raise DomainError("proxy_pool_missing", 503)
        nodes = _pool_nodes(s,pool)
        ids = [node.id for node in nodes]
        now = store.clock()
        held = set()
        for health in s.scalars(select(Resource).where(Resource.kind == "proxy_health")):
            item = json.loads(health.data)
            if item.get("platform") == account.platform and item.get("retry_at", 0) > now:
                # A model-specific failure does not exclude another model. A call
                # without model context (maintenance) only uses transport health.
                if not item.get("model") or (model and item.get("model") == model):
                    held.add(item.get("proxy_id"))
        checks = _target_checks(s, ids, account.platform)
        candidates = [node for node in nodes if _usable(node) and node.id not in held and _target_eligible(store, s, node, account.platform, checks, force=_dynamic_member(pool,node))]
        if not candidates:
            raise DomainError("proxy_pool_unavailable", 503)
        affinity = _row(s, affinity_kind, affinity_name)
        previous = json.loads(affinity.data) if affinity else {}
        selected = next((n for n in candidates if previous.get("pool_id") == pool.id and n.id == previous.get("proxy_id")), None)
        if selected is None:
            selected = max(candidates, key=lambda n: hashlib.sha256(f"{account_id}:{n.id}".encode()).digest())
            affinity_update = {"pool_id": pool.id, "proxy_id": selected.id, "platform": account.platform}
        endpoint = _endpoint(store, selected, s, account.platform)
    if affinity_update is not None:
        with store.tx() as (s, state):
            _put(store, s, affinity_kind, affinity_name, affinity_update)
            store.emit(s, state, "proxy", "egress_selected", account_id)
    return endpoint


def record_proxy_result(store, account_id, endpoint, *, success, code=None, model=None, model_verified=False, failure_phase=None):
    """Record a real operation; credentials/403/quota never ban an egress.

    A successful balance or HEAD request is connectivity evidence only. Only
    completed effective model output is allowed to set model_verified=True.
    URLs are matched privately and never persisted in public result metadata.
    """
    connection_failure = failure_phase in {'connect', 'proxy_connect'}
    if not endpoint or (not success and not connection_failure and (code not in EGRESS_FAILURES or failure_phase is not None)):
        return
    with store.tx() as (s, state):
        account = store.account(s, account_id)
        nodes = [n for n in s.scalars(select(Resource).where(Resource.kind == "proxy")) if n.secret and store.cipher.decrypt(n.secret.encode()).decode() == endpoint]
        for node in nodes:
            key = f"{account.platform}:{node.id}"
            old = _row(s, "proxy_health", key)
            result = json.loads(old.data) if old else {}
            if result.get("node_version") != node.version:
                result = {}
            failures = 0 if success else min(int(result.get("consecutive_transport_failures", 0)) + 1, 10)
            result.update({"proxy_id": node.id, "platform": account.platform, "last_code": "reachable" if success else code,
                           "last_observed_at": store.clock(), "consecutive_transport_failures": failures,
                           "node_version": node.version, "failure_phase": None if success else failure_phase,
                           "retry_at": 0 if success else store.clock() + min(30 * (2 ** (failures - 1)), 300)})
            if success:
                result["last_connectivity_success_at"] = store.clock()
            _put(store, s, "proxy_health", key, result)
            if success and model_verified and model:
                _put(store, s, "proxy_model_check", f"{key}:{model}", {"proxy_id": node.id, "platform": account.platform,
                     "model": model, "account_id": account_id, "proxy_version": node.version,
                     "verified_at": store.clock(), "effective_output": True})
        if nodes:
            store.emit(s, state, "proxy", "egress_observed", account_id)


def accounts_on_fixed_endpoint(store, platform, endpoint):
    """Read-only exclusion for this request after a connection-stage failure.

    Never rebind accounts or classify an HTTP/business/read error as a dead
    proxy. A pool can choose another exit; explicit bindings remain fixed.
    Match duplicate node IDs by private endpoint value without returning URLs.
    """
    if not endpoint:
        return set()
    with store.read() as (s, _):
        accounts = set(s.scalars(select(Account.id).where(Account.platform == platform)))
        if not accounts:
            return set()
        bindings = {r.name: json.loads(r.data).get('proxy_id') for r in s.scalars(
            select(Resource).where(Resource.kind == 'proxy_binding', Resource.name.in_(accounts)))}
        route_row = _row(s, 'proxy_route', platform)
        route = json.loads(route_row.data) if route_row else {'mode': 'direct'}
        fixed = route.get('proxy_id') if route.get('mode') == 'fixed' else None
        referenced = set(bindings.values()) | ({fixed} if fixed else set())
        matching = {r.id for r in s.scalars(select(Resource).where(Resource.kind == 'proxy', Resource.id.in_(referenced)))
                    if r.secret and store.cipher.decrypt(r.secret.encode()).decode() == endpoint}
        return {aid for aid in accounts if bindings.get(aid, fixed) in matching}


def select_registration_proxy(store, session, platform, session_id, explicit_proxy_id=None, account_id=None):
    """Select once inside the registration transaction, before any SMS side effect."""
    binding = _row(session, "proxy_binding", account_id) if account_id else None
    fixed = True
    if binding:
        identity = json.loads(binding.data)["proxy_id"]
    elif explicit_proxy_id and not account_id:
        identity = explicit_proxy_id
    else:
        route_row = _row(session, "proxy_route", platform)
        route = json.loads(route_row.data) if route_row else {"mode": "direct"}
        if route.get("mode") == "direct":
            return None, None
        if route.get("mode") == "fixed":
            identity = route.get("proxy_id")
        elif route.get("mode") == "pool":
            fixed = False
            pool = session.get(Resource, route.get("pool_id"))
            if not pool or pool.kind != "proxy_pool":
                raise DomainError("proxy_pool_missing", 503)
            held = {d.get("proxy_id") for row in session.scalars(select(Resource).where(Resource.kind == "proxy_health"))
                    if (d := json.loads(row.data)).get("platform") == platform and not d.get("model") and d.get("retry_at", 0) > store.clock()}
            nodes = _pool_nodes(session,pool)
            ids = [node.id for node in nodes]
            checks = _target_checks(session, ids, platform)
            candidates = [node for node in nodes
                          if _usable(node) and node.id not in held and _target_eligible(store, session, node, platform, checks, force=_dynamic_member(pool,node))]
            if not candidates:
                raise DomainError("proxy_pool_unavailable", 503)
            affinity = _row(session, "proxy_affinity", account_id) if account_id else None
            old = json.loads(affinity.data) if affinity else {}
            selected = next((n for n in candidates if old.get("pool_id") == pool.id and old.get("proxy_id") == n.id), None)
            selected = selected or max(candidates, key=lambda n: hashlib.sha256(f"{account_id or session_id}:{n.id}".encode()).digest())
            identity = selected.id
        else:
            raise DomainError("proxy_route_invalid", 503)
    return identity, _endpoint(store, session.get(Resource, identity), session, platform, fixed=fixed)


class PoolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    proxy_ids: list[str] = Field(default_factory=list, max_length=256)
    subscription_ids: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def membership(self):
        if not self.proxy_ids and not self.subscription_ids:
            raise ValueError("proxy_pool_members_required")
        return self


class RouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["direct", "fixed", "pool"]
    proxy_id: str | None = None
    pool_id: str | None = None

    @model_validator(mode="after")
    def target(self):
        if self.mode == "direct" and (self.proxy_id or self.pool_id):
            raise ValueError("direct route cannot include proxy or pool")
        if self.mode == "fixed" and (not self.proxy_id or self.pool_id):
            raise ValueError("fixed route requires only proxy_id")
        if self.mode == "pool" and (not self.pool_id or self.proxy_id):
            raise ValueError("pool route requires only pool_id")
        return self


def save_pool(store, body, pool_id=None):
    with store.tx() as (s, state):
        ids = list(dict.fromkeys(body.proxy_ids))
        sources = list(dict.fromkeys(body.subscription_ids))
        for identity in ids:
            if not _usable(s.get(Resource, identity)):
                raise DomainError("proxy_unavailable", 422)
        for identity in sources:
            source=s.get(Resource,identity)
            if not source or source.kind!="proxy_subscription":
                raise DomainError("subscription_not_found",404)
        payload={"proxy_ids":ids,"subscription_ids":sources}
        if pool_id:
            row = s.get(Resource, pool_id)
            if not row or row.kind != "proxy_pool":
                raise DomainError("proxy_pool_missing", 404)
            row.name, row.data = body.name, json.dumps(payload)
            row.version, row.updated_at = row.version + 1, store.clock()
        else:
            row = Resource(id=uid(), kind="proxy_pool", name=body.name, version=1, data=json.dumps(payload), updated_at=store.clock())
            s.add(row)
        store.emit(s, state, "proxy", "pool_saved")
        return {"id": row.id}


def save_route(store, platform, body):
    if platform not in PLATFORMS:
        raise DomainError("unsupported_platform", 422)
    with store.tx() as (s, state):
        if body.mode == "fixed" and not _usable(s.get(Resource, body.proxy_id)):
            raise DomainError("proxy_unavailable", 422)
        if body.mode == "pool":
            pool = s.get(Resource, body.pool_id)
            if not pool or pool.kind != "proxy_pool":
                raise DomainError("proxy_pool_missing", 422)
        _put(store, s, "proxy_route", platform, body.model_dump(exclude_none=True))
        store.emit(s, state, "proxy", "route_saved")
        return {"ok": True}


def mount_proxy_pools(app, store):
    @app.get("/api/proxy-pools")
    def pools():
        meta = cast(Resource.data, JSON) if store.engine.dialect.name == 'postgresql' else type_coerce(Resource.data, JSON)
        pool_id = meta['pool_id'].as_string()
        with store.read() as (s,_):
            counts = dict(s.execute(select(pool_id,func.count()).where(Resource.kind=='proxy_affinity').group_by(pool_id)).all())
        return {"pools": store.resources("proxy_pool"), "routes": store.resources("proxy_route"),
                "affinity_counts": counts}

    @app.post("/api/proxy-pools", status_code=201)
    def create(body: PoolInput):
        return save_pool(store, body)

    @app.put("/api/proxy-pools/{pool_id}")
    def update(pool_id: str, body: PoolInput):
        return save_pool(store, body, pool_id)

    @app.put("/api/proxy-routes/{platform}")
    def route(platform: str, body: RouteInput):
        return save_route(store, platform, body)
