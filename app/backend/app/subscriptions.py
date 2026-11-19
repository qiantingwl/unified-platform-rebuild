"""Import supported proxy endpoints; never silently turn unsupported nodes direct."""
import base64
import hashlib
import json
from contextlib import nullcontext
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import select

from .management import proxy_url
from .models import Resource
from .store import DomainError, uid, PLATFORMS
from .proxy_protocols import KERNEL_PROTOCOLS


def endpoint_identity(endpoint):
    """Canonical connection identity includes authentication, never the label."""
    parsed = proxy_url(endpoint)
    host = parsed.hostname.lower().rstrip('.')
    host = '[' + host + ']' if ':' in host else host
    user, password = unquote(parsed.username or ''), unquote(parsed.password or '')
    auth = quote(user, safe='') + ':' + quote(password, safe='') + '@' if parsed.username is not None else ''
    value = f'{parsed.scheme}://{auth}{host}:{parsed.port}'
    slot = f'{parsed.scheme}:{host}:{parsed.port}:{user}'
    return value, hashlib.sha256(value.encode()).hexdigest(), hashlib.sha256(slot.encode()).hexdigest()


def parse_subscription(text):
    if len(text.encode()) > 2 * 1024 * 1024:
        raise DomainError("subscription_too_large", 413)
    entries, rejected, protocol_nodes = [], [], []
    stripped = text.strip()
    if "://" not in stripped and not stripped.startswith(("{", "[")) and not stripped.startswith(("proxies:", "outbounds:")):
        try:
            stripped = base64.b64decode(stripped + "=" * (-len(stripped) % 4), validate=True).decode()
        except (ValueError, UnicodeError):
            pass
    # Alias expansion permits adversarial YAML to create cyclic structures;
    # proxy exports do not need YAML anchors.
    try:
        if stripped.startswith(("{", "[")):
            obj = json.loads(stripped)
        elif stripped.startswith(("proxies:", "outbounds:")) or "\nproxies:" in stripped:
            for token in yaml.scan(stripped):
                if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                    raise DomainError("subscription_yaml_alias_unsupported", 422)
            obj = yaml.safe_load(stripped)
        else:
            obj = None
    except (ValueError, yaml.YAMLError):
        raise DomainError("invalid_subscription", 422) from None
    if obj is not None:
        rows = obj.get("proxies", obj.get("outbounds")) if isinstance(obj, dict) else obj
        if not isinstance(rows, list):
            raise DomainError("invalid_subscription", 422)
        if len(rows) > 10000:
            raise DomainError("too_many_proxy_nodes", 422)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                rejected.append({"index": index, "reason": "invalid_node"})
                continue
            kind = row.get("type")
            if kind in KERNEL_PROTOCOLS:
                try:
                    from .proxy_protocols import parse_node
                    protocol_nodes.append(parse_node(row))
                except DomainError as error:
                    rejected.append({'index':index, 'reason':error.code})
                continue
            if kind not in {"http", "https", "socks", "socks5", "socks5h"}:
                rejected.append({"index": index, "reason": "proxy_protocol_unsupported"})
                continue
            # Endpoint consumers cannot implement arbitrary kernel options.
            # In particular TLS SOCKS must never silently become plaintext.
            allowed = {"type", "name", "tag", "server", "port", "server_port", "username", "password", "tls", "skip-cert-verify"}
            if set(row) - allowed or row.get('skip-cert-verify') not in (None, False):
                rejected.append({"index": index, "reason": "proxy_options_unsupported"})
                continue
            if not isinstance(row.get('tls', False), bool) or (kind in {'socks','socks5','socks5h'} and row.get('tls')):
                rejected.append({"index": index, "reason": "proxy_tls_unsupported"})
                continue
            scheme = "socks5" if kind == "socks" else "https" if row.get("tls") is True and kind == "http" else kind
            host, port = row.get("server"), row.get("port", row.get("server_port"))
            if not isinstance(host, str) or not isinstance(port, int) or not 0 < port < 65536:
                rejected.append({"index": index, "reason": "invalid_node"})
                continue
            if ":" in host and not host.startswith("["):
                host = "[" + host + "]"
            auth = ""
            if row.get("username"):
                auth = quote(str(row["username"]), safe="") + ":" + quote(str(row.get("password", "")), safe="") + "@"
            entries.append((str(row.get("name", row.get("tag", f"节点 {index + 1}")))[:100], f"{scheme}://{auth}{host}:{port}"))
    else:
        lines = [line.strip() for line in stripped.splitlines() if line.strip() and not line.startswith("#")]
        if len(lines) > 10000:
            raise DomainError("too_many_proxy_nodes", 422)
        for index, line in enumerate(lines):
            if line.split('://',1)[0].lower() in KERNEL_PROTOCOLS | {'hy2'}:
                try:
                    from .proxy_protocols import parse_node
                    protocol_nodes.append(parse_node(line))
                except DomainError as error:
                    rejected.append({'index':index, 'reason':error.code})
                continue
            try:
                parsed = urlsplit(line)
                label = unquote(parsed.fragment)[:100] or f"节点 {index + 1}"
                value = line.split("#", 1)[0]
                proxy_url(value)
                entries.append((label, value))
            except (ValueError, DomainError):
                rejected.append({"index": index, "reason": "proxy_protocol_unsupported"})
    valid, seen = [], set()
    for name, endpoint in entries:
        try:
            parsed = proxy_url(endpoint)
        except DomainError:
            rejected.append({"index": len(valid), "reason": "invalid_node"})
            continue
        endpoint, identity, slot = endpoint_identity(endpoint)
        if identity not in seen:
            valid.append({"name": name, "url": endpoint, "scheme": parsed.scheme, "identity": identity, "slot_identity": slot})
            seen.add(identity)
    for node in protocol_nodes:
        if node['identity'] not in seen:
            valid.append(node)
            seen.add(node['identity'])
    return valid, rejected


def source_ids(data):
    return set(data.get('subscription_ids') or ([data['subscription_id']] if data.get('subscription_id') else []))


def save_nodes(store, subscription_id, nodes, rejected, lease_token=None, source_metadata=None, *, transaction=None):
    with (nullcontext(transaction) if transaction else store.tx()) as (s, state):
        subscription = s.get(Resource, subscription_id)
        if not subscription or subscription.kind != "proxy_subscription":
            raise DomainError("subscription_not_found", 404)
        config = json.loads(subscription.data)
        if lease_token is not None and config.get('sync_lease', {}).get('token') != lease_token:
            raise DomainError('subscription_sync_superseded', 409)
        existing, identities, slots, metadata = {}, {}, {}, {}
        for row in s.scalars(select(Resource).where(Resource.kind == "proxy")):
            data = json.loads(row.data)
            owners = source_ids(data)
            if not owners and (data.get('manual_source') or not data.get('identity')):
                continue  # Never claim hand-created or trusted-account migration nodes.
            if row.secret and data.get('scheme') in {'http','https','socks5','socks5h'}:
                try:
                    _, identity, slot = endpoint_identity(store.cipher.decrypt(row.secret.encode()).decode())
                    data.update(identity=identity, slot_identity=slot)
                except DomainError:
                    continue
            metadata[row.id] = data
            identities.setdefault(data.get('identity'), []).append(row)
            if subscription_id in owners:
                existing[row.id] = row
                slots.setdefault(data.get('slot_identity'), []).append(row)
        # Recompute caller identities: stale parsed dictionaries must not collapse
        # different passwords, but a single-source password rotation keeps its ID.
        normalized = []
        seen = set()
        for node in nodes:
            node = dict(node)
            if node['scheme'] in KERNEL_PROTOCOLS:
                from .proxy_protocols import parse_node
                canonical = parse_node(node['kernel'])
                identity = canonical['identity']
                node.update(kernel=canonical['kernel'], identity=identity, slot_identity=identity)
                slot = identity
            else:
                endpoint, identity, slot = endpoint_identity(node['url'])
                node.update(url=endpoint, identity=identity, slot_identity=slot)
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append(node)
        slot_counts = {}
        for node in normalized:
            slot_counts[node['slot_identity']] = slot_counts.get(node['slot_identity'], 0) + 1
        added, updated, shared, retired = 0, 0, 0, 0
        retained = set()
        for node in normalized:
            matches = identities.get(node['identity'], [])
            # Keep already-bound historical duplicates stable; new sources reuse
            # an existing canonical node and never create another duplicate.
            row = next((r for r in matches if r.id in existing), matches[0] if matches else None)
            if row is None and slot_counts[node['slot_identity']] == 1:
                candidates = slots.get(node['slot_identity'], [])
                if len(candidates) == 1 and source_ids(metadata[candidates[0].id]) == {subscription_id}:
                    row = candidates[0]
            if row is None:
                row = Resource(id=uid(), kind="proxy", name=node["name"], version=1, data="{}", updated_at=store.clock())
                s.add(row)
                added += 1
                data = {'status':'pending_kernel' if node['scheme'] in KERNEL_PROTOCOLS else 'untested'}
            else:
                updated += 1
                data = metadata[row.id]
                if subscription_id not in source_ids(data):
                    shared += 1
                if data.get('identity') != node['identity']:
                    row.version += 1
            owners = source_ids(data) | {subscription_id}
            # Refresh is not permission to revive either manual disabled or
            # retired nodes. Missing-source retirement is also conservative.
            data.update(scheme=node['scheme'], subscription_ids=sorted(owners), identity=node['identity'], slot_identity=node['slot_identity'], target_check_required=True)
            if node.get('source_metrics'):
                metrics=dict(data.get('subscription_source_metrics') or {})
                metrics[subscription_id]=node['source_metrics']
                data['subscription_source_metrics']=metrics
            data.pop('subscription_id', None)
            row.name, row.updated_at = data.get("name_override") or node["name"], store.clock()
            row.data = json.dumps(data)
            if node['scheme'] in KERNEL_PROTOCOLS:
                material = s.scalar(select(Resource).where(Resource.kind=='proxy_kernel_source', Resource.name==row.id))
                if material is None:
                    material=Resource(id=uid(), kind='proxy_kernel_source', name=row.id, data='{}', updated_at=store.clock())
                    s.add(material)
                material.data=json.dumps({'protocol':node['scheme'], 'identity':node['identity']})
                material.secret=store.cipher.encrypt(json.dumps(node['kernel'], sort_keys=True).encode()).decode()
                material.updated_at=store.clock()
            else:
                row.secret = store.cipher.encrypt(node["url"].encode()).decode()
            metadata[row.id] = data
            retained.add(row.id)
            identities.setdefault(node['identity'], []).append(row)
        # Missing endpoints retain IDs/bindings, but forwarding refuses a retired
        # binding instead of unexpectedly using a different egress.
        for row in existing.values():
            if row.id in retained or (rejected and not (source_metadata or {}).get('selection_complete')):
                continue  # Partial parse cannot prove an old entry disappeared.
            data = metadata[row.id]
            owners = source_ids(data) - {subscription_id}
            data['subscription_ids'] = sorted(owners)
            if data.get('subscription_source_metrics'):
                data['subscription_source_metrics'].pop(subscription_id,None)
            data.pop('subscription_id', None)
            if not owners and data.get('status') not in {'disabled','retired'}:
                data.update(status='retired', retired_reason='subscription_missing')
                retired += 1
                row.version += 1
            row.data, row.updated_at = json.dumps(data), store.clock()
        partial_retained=bool(rejected) and not (source_metadata or {}).get('selection_complete',False)
        summary = {"added": added, "updated": updated, 'shared':shared, "retired": retired, 'node_count':len(retained), "rejected": len(rejected), "rejections": rejected[:100], 'partial_retained':partial_retained, "last_sync_at": store.clock()}
        config.update(summary, status='synced', last_error=None)
        if lease_token is None:
            config['last_attempt_at']=store.clock()
        config['sync_progress']={'phase':'completed','finished_at':store.clock(),'parsed':len(normalized),'rejected':len(rejected)}
        if source_metadata is not None:
            config['source_metadata']=source_metadata
            config['partial_retained']=bool(rejected) and not source_metadata.get('selection_complete',False)
        config.pop('sync_lease', None)
        config['next_sync_at'] = store.clock() + config.get('interval_minutes', 360) * 60 if config.get('auto_update', False) else None
        subscription.data, subscription.updated_at = json.dumps(config), store.clock()
        subscription.version += 1
        store.emit(s, state, "proxy_subscription", "subscription_imported")
        return summary


class TargetConfig(BaseModel):
    source_profile: Literal['standard','leidahub_fast'] = 'standard'
    recommendation_limit: int = Field(default=100,ge=1,le=500)
    target_platforms: list[str] = Field(default_factory=list, max_length=7)
    auto_target_check: bool = False

    @model_validator(mode='after')
    def targets(self):
        if any(p not in PLATFORMS for p in self.target_platforms):
            raise ValueError('unsupported_target_platform')
        object.__setattr__(self, 'target_platforms', list(dict.fromkeys(self.target_platforms)))
        if self.auto_target_check and not self.target_platforms:
            raise ValueError('target_platform_required')
        return self


class SubscriptionInput(TargetConfig):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    url: SecretStr | None = None
    content: SecretStr | None = None
    source_kind: Literal['paste','file'] = 'paste'
    file_name: str | None = Field(default=None,max_length=255)
    auto_update: bool | None = None
    interval_minutes: int = Field(default=360, ge=5, le=10080)


class ScheduleInput(TargetConfig):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)
    auto_update: bool
    interval_minutes: int = Field(default=360, ge=5, le=10080)


def save_schedule(store, identity, body):
    with store.tx() as (s, state):
        row = s.get(Resource, identity)
        if row is None or row.kind != 'proxy_subscription':
            raise DomainError('subscription_not_found', 404)
        if row.version != body.expected_version:
            raise DomainError('resource_version_conflict', 409)
        if body.auto_update and not row.secret:
            raise DomainError('subscription_url_missing', 422)
        data = json.loads(row.data)
        old_selection=(data.get('source_profile','standard'),data.get('recommendation_limit',100))
        data.update(auto_update=body.auto_update, interval_minutes=body.interval_minutes,
                    next_sync_at=store.clock() + body.interval_minutes * 60 if body.auto_update and data.get('enabled') is not False else None)
        # Older clients updating only refresh cadence retain target configuration.
        data.update(body.model_dump(include={'target_platforms','auto_target_check','source_profile','recommendation_limit'}, exclude_unset=True))
        if data.get('source_profile')=='leidahub_fast' and not row.secret:
            raise DomainError('subscription_url_missing',422)
        if old_selection!=(data.get('source_profile','standard'),data.get('recommendation_limit',100)):
            data.pop('sync_lease',None)
            data['next_sync_at']=store.clock() if data.get('auto_update') and data.get('enabled') is not False else None
        try:
            TargetConfig.model_validate(data)
        except ValueError:
            raise DomainError('target_platform_required', 422) from None
        row.data, row.updated_at, row.version = json.dumps(data), store.clock(), row.version + 1
        store.emit(s, state, 'proxy_subscription', 'schedule_saved')
        return {'id':row.id, 'version':row.version, 'data':{k:v for k,v in data.items() if k not in {'sync_lease','source_fingerprint'}}}


class SubscriptionEdit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: int=Field(ge=0)
    name: str=Field(min_length=1,max_length=100)
    url: SecretStr | None=None
    content: SecretStr | None=None
    file_name: str | None=Field(default=None,max_length=255)
    source_kind: Literal['paste','file'] | None=None
    enabled: bool | None=None


def validate_source_url(value):
    try:
        parsed=urlsplit(value)
        if len(value)>8192 or parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        parsed.port
    except ValueError:
        raise DomainError('invalid_subscription_url',422) from None
    return value


def safe_file_name(value):
    return (value or '').replace('\\','/').rsplit('/',1)[-1][:255]


def unique_source(store,session,kind,value,exclude=None):
    digest=hashlib.sha256((kind+'\0'+value.strip()).encode()).hexdigest()
    for row in session.scalars(select(Resource).where(Resource.kind=='proxy_subscription')):
        if row.id==exclude:
            continue
        data=json.loads(row.data)
        if data.get('source_fingerprint')==digest:
            raise DomainError('subscription_source_duplicate',409)
        if kind=='url' and row.secret and store.cipher.decrypt(row.secret.encode()).decode().strip()==value.strip():
            raise DomainError('subscription_source_duplicate',409)
    return digest


class SubscriptionPreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    url: SecretStr | None=None
    content: SecretStr | None=None
    subscription_id: str | None=None


def preview_nodes(nodes,rejected):
    counts={}
    for node in nodes:
        counts[node['scheme']]=counts.get(node['scheme'],0)+1
    # Connection addresses, credentials and normalized kernel material stay private.
    return {'total':len(nodes),'rejected':len(rejected),'protocols':counts,
            'nodes':[{'name':n['name'],'scheme':n['scheme']} for n in nodes[:100]],
            'rejections':rejected[:100],'truncated':len(nodes)>100 or len(rejected)>100}


def edit_subscription(store,identity,body):
    parsed_content=None
    if body.content is not None:
        if body.url is not None:
            raise DomainError('subscription_source_required',422)
        parsed_content=parse_subscription(body.content.get_secret_value())
        if not parsed_content[0]:
            raise DomainError('subscription_empty',422)
    with store.tx() as (s,state):
        row=s.get(Resource,identity)
        if row is None or row.kind!='proxy_subscription':
            raise DomainError('subscription_not_found',404)
        if row.version!=body.expected_version:
            raise DomainError('resource_version_conflict',409)
        data=json.loads(row.data)
        if parsed_content is not None and row.secret:
            raise DomainError('subscription_source_conflict',422)
        if parsed_content is not None:
            data['source_fingerprint']=unique_source(store,s,'local',body.content.get_secret_value(),identity)
        if body.url is not None:
            value=validate_source_url(body.url.get_secret_value())
            data['source_fingerprint']=unique_source(store,s,'url',value,identity)
            row.secret=store.cipher.encrypt(value.encode()).decode()
            data.update(source='url',source_host=urlsplit(value).hostname,status='not_synced',last_error=None)
            data.pop('sync_lease',None) # fence old URL's inflight response
            data.pop('sync_requested',None)
            data['sync_progress']={'phase':'idle'}
            data['next_sync_at']=store.clock() if data.get('auto_update') and data.get('enabled') is not False else None
        if body.enabled is not None:
            data['enabled']=body.enabled
            if not body.enabled:
                data.pop('sync_lease',None)
                data.pop('sync_requested',None)
                data['sync_progress']={'phase':'stopped'}
                data['next_sync_at']=None
            elif data.get('auto_update'):
                data['next_sync_at']=store.clock()
        row.name,row.version,row.data,row.updated_at=body.name,row.version+1,json.dumps(data),store.clock()
        if body.file_name is not None or body.source_kind is not None:
            if row.secret:
                raise DomainError('subscription_source_conflict',422)
            data.update(source=body.source_kind or 'file',file_name=safe_file_name(body.file_name))
            row.data=json.dumps(data)
        if parsed_content is not None:
            # Editing a local source and replacing its nodes is one transaction.
            save_nodes(store,identity,*parsed_content,transaction=(s,state))
        store.emit(s,state,'proxy_subscription','edited')
        return {'id':row.id,'version':row.version}


def delete_subscription(store,identity,expected_version):
    with store.tx() as (s,state):
        row=s.get(Resource,identity)
        if row is None or row.kind!='proxy_subscription':
            raise DomainError('subscription_not_found',404)
        if row.version!=expected_version:
            raise DomainError('resource_version_conflict',409)
        for pool in s.scalars(select(Resource).where(Resource.kind=='proxy_pool')):
            if identity in json.loads(pool.data).get('subscription_ids',[]):
                raise DomainError('subscription_in_use',409)
        for node in s.scalars(select(Resource).where(Resource.kind=='proxy',Resource.data.contains(json.dumps(identity),autoescape=True))):
            data=json.loads(node.data)
            owners=source_ids(data)
            if identity not in owners:
                continue
            owners.discard(identity)
            data['subscription_ids']=sorted(owners)
            data.pop('subscription_id',None)
            data.get('subscription_source_metrics',{}).pop(identity,None)
            if not owners and data.get('status')!='disabled':
                data.update(status='retired',retired_reason='subscription_deleted')
            node.data,node.version,node.updated_at=json.dumps(data),node.version+1,store.clock()
        s.delete(row)
        store.emit(s,state,'proxy_subscription','deleted')
        return {'deleted':identity}


def mount_subscriptions(app, store, registry):
    from .subscription_scheduler import SubscriptionScheduler
    scheduler = SubscriptionScheduler(store, registry)
    app.state.subscription_scheduler = scheduler
    @app.get("/api/proxy-subscriptions")
    def subscriptions():
        result=store.resources('proxy_subscription')
        for row in result:
            lease=row['data'].pop('sync_lease',{})
            row['data'].pop('source_fingerprint',None)
            row['data']['syncing']=lease.get('until',0)>store.clock()
        return {'subscriptions':result}

    @app.post('/api/proxy-subscriptions/preview')
    async def preview(body: SubscriptionPreview):
        if sum(bool(v) for v in (body.url,body.content,body.subscription_id))!=1:
            raise DomainError('subscription_source_required',422)
        if body.url or body.subscription_id:
            if not registry.enabled:
                raise DomainError('network_execution_disabled',409)
            if body.subscription_id:
                with store.read() as (s,_):
                    row=s.get(Resource,body.subscription_id)
                    if not row or row.kind!='proxy_subscription' or not row.secret:
                        raise DomainError('subscription_url_missing',404)
                    value=store.cipher.decrypt(row.secret.encode()).decode()
            else:
                value=validate_source_url(body.url.get_secret_value())
            transport=registry.transport_factory(proxy=None)
            try:
                from .subscription_scheduler import fetch_content
                text=await fetch_content(transport,value)
            finally:
                await transport.close()
        else:
            text=body.content.get_secret_value()
        import asyncio
        nodes,rejected=await asyncio.to_thread(parse_subscription,text)
        return preview_nodes(nodes,rejected)

    @app.post("/api/proxy-subscriptions", status_code=201)
    def add(body: SubscriptionInput):
        if bool(body.url) == bool(body.content):
            raise DomainError("subscription_source_required", 422)
        if body.url:
            value = body.url.get_secret_value()
            if body.source_profile=='leidahub_fast':
                value='https://jiedianleida.com/'
            validate_source_url(value)
            auto = body.auto_update if body.auto_update is not None else True
            config={"status":"not_synced", 'source':'url','source_host':urlsplit(value).hostname,
                'auto_update':auto, 'interval_minutes':body.interval_minutes,
                'target_platforms':body.target_platforms, 'auto_target_check':body.auto_target_check,
                'source_profile':body.source_profile,'recommendation_limit':body.recommendation_limit,
                'next_sync_at':store.clock() if auto else None}
            with store.tx() as (s,state):
                config['source_fingerprint']=unique_source(store,s,'url',value)
                identity=uid()
                s.add(Resource(id=identity,kind='proxy_subscription',name=body.name,version=1,updated_at=store.clock(),
                    data=json.dumps(config),secret=store.cipher.encrypt(value.encode()).decode()))
                store.emit(s,state,'proxy_subscription','created')
                return {'id':identity}
        if body.auto_update or body.source_profile!='standard':
            raise DomainError('subscription_url_missing', 422)
        nodes, rejected = parse_subscription(body.content.get_secret_value())
        if not nodes:
            raise DomainError('subscription_empty', 422)
        identity=uid()
        with store.tx() as (s,state):
            fingerprint=unique_source(store,s,'local',body.content.get_secret_value())
            row=Resource(id=identity,kind='proxy_subscription',name=body.name,version=1,updated_at=store.clock(),data=json.dumps({
                'source':body.source_kind,'file_name':safe_file_name(body.file_name),'auto_update':False,
                'source_fingerprint':fingerprint,
                'interval_minutes':body.interval_minutes,'target_platforms':body.target_platforms,'auto_target_check':body.auto_target_check}))
            s.add(row);s.flush()
            return {'id':identity,**save_nodes(store,identity,nodes,rejected,transaction=(s,state))}

    @app.put('/api/proxy-subscriptions/{subscription_id}')
    def edit(subscription_id: str,body: SubscriptionEdit):
        return edit_subscription(store,subscription_id,body)

    @app.delete('/api/proxy-subscriptions/{subscription_id}')
    def remove(subscription_id: str,expected_version: int):
        return delete_subscription(store,subscription_id,expected_version)

    @app.put('/api/proxy-subscriptions/{subscription_id}/schedule')
    def schedule(subscription_id: str, body: ScheduleInput):
        return save_schedule(store, subscription_id, body)

    @app.post("/api/proxy-subscriptions/{subscription_id}/sync")
    async def sync(subscription_id: str):
        return await scheduler.sync(subscription_id)

    @app.post('/api/proxy-subscriptions/{subscription_id}/sync-start',status_code=202)
    def sync_start(subscription_id: str):
        # The existing tasks-role scheduler consumes this durable request.
        # Closing the browser never cancels or duplicates a refresh.
        return scheduler.request_sync(subscription_id)
