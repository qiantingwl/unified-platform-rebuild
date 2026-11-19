"""Explicit, opt-in per-key scope and limits. Defaults do not limit traffic."""
import ipaddress
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from .models import Resource, RequestLog

DEFAULT = {"models": [], "aliases": {}, "expires_at": None, "max_concurrency": 0, "rpm": 0, "daily_requests": 0, "allowed_ips": []}

def read(session, key):
    row = session.scalar(select(Resource).where(Resource.kind == "key_policy", Resource.name == key.id))
    return decode(key, row.data if row else None)

def decode(key, raw):
    from .store import DomainError
    try:
        saved = json.loads(raw) if raw is not None else {}
        if not isinstance(saved, dict):
            raise ValueError()
        policy = {**DEFAULT, "models": [key.model], **saved}
        validate(policy)
        return policy
    except (ValueError, TypeError, DomainError):
        # A damaged persisted scope must never silently grant the default scope.
        raise DomainError("invalid_key_policy", 503) from None

def validate(policy):
    from .store import DomainError
    models = policy.get('models', [])
    aliases = policy.get('aliases', {})
    ips = policy.get('allowed_ips', [])
    if not isinstance(models, list) or not isinstance(aliases, dict) or not isinstance(ips, list):
        raise DomainError('invalid_key_policy',422)
    if not models or len(models)>200 or len(aliases)>200 or len(ips)>200:
        raise DomainError('invalid_key_policy',422)
    for name, maximum in [('max_concurrency',100000),('rpm',1000000),('daily_requests',1000000000)]:
        value = policy.get(name, 0)
        if type(value) is not int or not 0 <= value <= maximum:
            raise DomainError('invalid_key_policy',422)
    expiry = policy.get('expires_at')
    if expiry is not None and (type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry < 0):
        raise DomainError('invalid_key_policy',422)
    for model in models + list(aliases) + list(aliases.values()):
        if not isinstance(model,str) or not 0<len(model)<=100 or any(c.isspace() for c in model):
            raise DomainError('invalid_key_policy',422)
    if any(target not in models for target in aliases.values()):
        raise DomainError('invalid_key_alias',422)
    try:
        for network in ips:
            if not isinstance(network,str):raise ValueError()
            ipaddress.ip_network(network,strict=False)
    except (ValueError, TypeError):
        raise DomainError('invalid_ip_rule',422) from None

def admit(session, key, policy, model, now, client_ip, request_id):
    from .store import DomainError
    if policy['expires_at'] is not None and now >= policy['expires_at']:
        raise DomainError('api_key_expired',401)
    if policy['allowed_ips']:
        try:
            address=ipaddress.ip_address(client_ip or '')
            allowed=any(address in ipaddress.ip_network(n,strict=False) for n in policy['allowed_ips'])
        except ValueError:allowed=False
        if not allowed:raise DomainError('api_key_ip_denied',403)
    resolved=policy['aliases'].get(model,model)
    if resolved not in policy['models']:raise DomainError('model_not_found',404)
    if request_id:
        old=session.get(RequestLog,request_id)
        if not old or old.key_id!=key.id or (old.authorized_model or old.model)!=resolved:raise DomainError('request_not_retryable')
        return resolved
    clauses=[RequestLog.key_id==key.id]
    if policy['max_concurrency'] and session.scalar(select(func.count()).select_from(RequestLog).where(*clauses,RequestLog.status=='inflight'))>=policy['max_concurrency']:
        raise DomainError('api_key_concurrency',429)
    if policy['rpm'] and session.scalar(select(func.count()).select_from(RequestLog).where(*clauses,RequestLog.started_at>now-60))>=policy['rpm']:
        raise DomainError('api_key_rate_limit',429)
    midnight=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    if policy['daily_requests'] and session.scalar(select(func.count()).select_from(RequestLog).where(*clauses,RequestLog.started_at>=midnight))>=policy['daily_requests']:
        raise DomainError('api_key_daily_limit',429)
    return resolved
