"""Native Responses continuation. All message material is encrypted at rest."""
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select, delete

from .models import ApiKey, Resource
from .store import DomainError
from .protocols import converted, normalize

KIND = 'response_history'
TTL = 30 * 86400


@dataclass
class HistoryContext:
    owner: str
    store: bool
    messages: list
    conversation: str | None = None
    previous_response_id: str | None = None
    instruction_index: int | None = None


def owner(session, token, now, client_ip=None):
    from .key_policy import read
    import ipaddress
    key = session.scalar(select(ApiKey).where(ApiKey.digest == hashlib.sha256(token.encode()).hexdigest(), ApiKey.enabled.is_(True)))
    if key is None:
        raise DomainError('invalid_api_key', 401)
    policy = read(session, key)
    if policy['expires_at'] is not None and now >= policy['expires_at']:
        raise DomainError('api_key_expired', 401)
    if policy['allowed_ips']:
        try:
            address = ipaddress.ip_address(client_ip or '')
            allowed = any(address in ipaddress.ip_network(n, strict=False) for n in policy['allowed_ips'])
        except ValueError:
            allowed = False
        if not allowed:
            raise DomainError('api_key_ip_denied', 403)
    return key.id


def load(store, session, identity, key_id):
    row = session.get(Resource, identity)
    if row is None or row.kind != KIND or row.name != key_id or row.updated_at + TTL <= store.clock():
        raise DomainError('response_not_found', 404)
    try:
        return json.loads(store.cipher.decrypt(row.secret.encode()))
    except Exception:
        raise DomainError('response_history_unavailable', 503) from None


def prepare(store, token, raw, client_ip=None):
    """Return normalized source and context; invoke before account admission.

    Previous instructions are deliberately excluded from inherited messages.
    Every turn's supplied instructions still apply to that turn unchanged.
    """
    if not isinstance(raw, dict) or type(raw.get('store', True)) is not bool:
        raise DomainError('unsupported_protocol_field', 422)
    previous = raw.get('previous_response_id')
    if previous is not None and (not isinstance(previous, str) or not 0 < len(previous) <= 100):
        raise DomainError('unsupported_protocol_field', 422)
    with store.read() as (session, _):
        key_id = owner(session, token, store.clock(), client_ip)
        saved = load(store, session, previous, key_id) if previous else None
    source = deepcopy(raw)
    source.pop('store', None)
    source.pop('previous_response_id', None)
    # Normalize the new turn separately so invalid input is rejected normally.
    new_body = normalize(source, 'responses')
    inherited = deepcopy(saved['messages']) if saved else []
    history = inherited + new_body['messages']
    if len(history) > 1000:
        raise DomainError('response_context_too_large', 413)
    # A private normalized history is consumed by normalize only from trusted
    # main code, never accepted from a client JSON field.
    context = HistoryContext(key_id, raw.get('store', True), history,
                             saved.get('conversation') if saved else None, previous,
                             len(inherited) if source.get('instructions') else None)
    return source, context


def apply_history(body, context):
    body['messages'] = deepcopy(context.messages)
    return body


def complete(store, context, result, conversation, response=None):
    try:
        return _complete(store, context, result, conversation, response)
    except DomainError:
        raise
    except Exception:
        # Storage/crypto driver errors can contain SQL parameters, including
        # encrypted payloads. Expose only the stable category to the caller;
        # main records failed delivery even if upstream generation succeeded.
        raise DomainError('response_history_unavailable', 503) from None


def _complete(store, context, result, conversation, response=None):
    response = deepcopy(response) if response is not None else converted(result, 'responses')
    response['store'] = context.store
    response['previous_response_id'] = context.previous_response_id
    if not context.store:
        return response
    # Instructions are request-scoped; explicit system/developer input messages
    # remain part of input history. prepare records their exact distinction.
    messages = deepcopy(context.messages)
    if context.instruction_index is not None:
        messages.pop(context.instruction_index)
    message = deepcopy(result['choices'][0]['message'])
    messages.append(message)
    secret = json.dumps({'messages': messages, 'response': response,
                         'conversation': conversation}, ensure_ascii=False)
    if len(secret.encode()) > 32 * 1024 * 1024:
        raise DomainError('response_context_too_large', 413)
    with store.tx(scope=None) as (session, _):
        row = session.get(Resource, result['id'])
        if row is not None:
            if row.kind != KIND or row.name != context.owner:
                raise DomainError('response_history_conflict', 409)
            return load(store, session, row.id, context.owner)['response']
        session.add(Resource(id=result['id'], kind=KIND, name=context.owner,
                             version=1, data='{}', secret=store.cipher.encrypt(secret.encode()).decode(),
                             updated_at=store.clock()))
        # Bounded expiry cleanup, indexed by resource kind; no body decryption.
        expired = select(Resource.id).where(Resource.kind == KIND, Resource.updated_at <= store.clock() - TTL).limit(100)
        session.execute(delete(Resource).where(Resource.id.in_(expired)))
    return response


def mount_response_history(app, store):
    def token(request):
        auth = request.headers.get('authorization', '')
        return auth[7:] if auth.startswith('Bearer ') else request.headers.get('x-api-key', '')

    @app.get('/v1/responses/{response_id}')
    def retrieve(response_id: str, request: Request):
        with store.read() as (session, _):
            key_id = owner(session, token(request), store.clock(), request.client.host if request.client else None)
            return load(store, session, response_id, key_id)['response']

    @app.delete('/v1/responses/{response_id}')
    def remove(response_id: str, request: Request):
        with store.tx(scope=None) as (session, _):
            key_id = owner(session, token(request), store.clock(), request.client.host if request.client else None)
            load(store, session, response_id, key_id)
            session.delete(session.get(Resource, response_id))
        return {'id': response_id, 'object': 'response.deleted', 'deleted': True}
