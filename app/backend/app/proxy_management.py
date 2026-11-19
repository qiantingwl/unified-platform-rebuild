"""Native node directory and guarded mutations. Never returns proxy secrets."""
import json
from typing import Literal
from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import JSON, cast, func, or_, select, type_coerce
from .models import Resource
from .store import DomainError


def metadata(dialect):
    return cast(Resource.data, JSON) if dialect == 'postgresql' else type_coerce(Resource.data, JSON)


def query_nodes(q='', source='', protocol='', status='', dialect='sqlite'):
    meta = metadata(dialect)
    stmt = select(Resource).where(Resource.kind == 'proxy')
    if status != 'retired':
        stmt = stmt.where(or_(meta['deleted'].as_boolean().is_(None),meta['deleted'].as_boolean().is_(False)))
    if q:
        escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        stmt = stmt.where(or_(Resource.name.ilike('%'+escaped+'%', escape='\\'), Resource.id == q))
    if protocol:
        stmt = stmt.where(meta['scheme'].as_string() == protocol)
    if status:
        stmt = stmt.where(meta['status'].as_string() == status)
    # IDs are server generated UUIDs. JSON ownership matching is boundary exact.
    owners = or_(meta['subscription_id'].as_string().is_not(None),func.coalesce(meta['subscription_ids'].as_string(),'[]').not_in(['[]','null','']))
    if source == 'manual':
        stmt = stmt.where(~owners)
    elif source == 'subscription':
        stmt = stmt.where(owners)
    elif source:
        stmt = stmt.where(Resource.data.contains(json.dumps(source), autoescape=True))
    return stmt


def public(row,store=None):
    address=None
    if store and row.secret:
        from urllib.parse import urlsplit
        try:
            parsed=urlsplit(store.cipher.decrypt(row.secret.encode()).decode())
            if parsed.hostname and parsed.port:
                host='['+parsed.hostname+']' if ':' in parsed.hostname else parsed.hostname
                address=f'{parsed.scheme}://{host}:{parsed.port}'
        except (ValueError,TypeError,InvalidToken):
            pass
    return dict(address=address,id=row.id,name=row.name,version=row.version,data=json.loads(row.data),has_secret=bool(row.secret),updated_at=row.updated_at)


def references(session, identity, store):
    kinds = ['proxy_binding','proxy_pool','proxy_route','maintenance_proxy_route','proxy_affinity','registration_session','registration_proxy_session','registration_batch']
    hits = []
    for row in session.scalars(select(Resource).where(Resource.kind.in_(kinds), Resource.data.contains(json.dumps(identity), autoescape=True))):
        record=json.loads(row.data)
        if row.kind=='registration_batch' and record.get('status') in {'completed','cancelled'}:
            continue
        hits.append({'kind':row.kind,'id':row.id})
    # Fixed registration egress is encrypted alongside session materials.
    for row in session.scalars(select(Resource).where(Resource.kind=='registration',Resource.secret.is_not(None),or_(Resource.data.contains(json.dumps(identity),autoescape=True),~Resource.data.contains('"proxy_id":')))):
        record=json.loads(row.data)
        if record.get('status') in {'completed','succeeded','cancelled','expired','failed'} and not record.get('result_unresolved'):
            continue
        if 'proxy_id' in record:
            if record['proxy_id']==identity:
                hits.append({'kind':'registration','id':row.id})
            continue
        try:
            material=json.loads(store.cipher.decrypt(row.secret.encode()).decode())
        except (ValueError, TypeError, InvalidToken):
            raise DomainError('registration_material_invalid',409) from None
        if material.get('proxy_id')==identity:
            hits.append({'kind':'registration','id':row.id})
    return hits


class NodeEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)
    name: str | None = Field(default=None,min_length=1,max_length=100)
    url: SecretStr | None = None
    enabled: bool | None = None


class BatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['enable','disable','delete']
    ids: list[str] = Field(min_length=1,max_length=256)


def mutate_node(store, session, row, body):
    if row.version != body.expected_version:
        raise DomainError('resource_version_conflict',409)
    data = json.loads(row.data)
    old_status=data.get('status')
    if body.name is not None:
        if body.name!=row.name and (data.get('subscription_ids') or data.get('subscription_id')):
            data['name_override']=body.name
        row.name = body.name
    if body.url is not None:
        if data.get('subscription_ids') or data.get('subscription_id'):
            raise DomainError('proxy_source_managed',422)
        from .subscriptions import parse_subscription
        nodes, rejected = parse_subscription(body.url.get_secret_value())
        if rejected or len(nodes) != 1:
            raise DomainError('single_proxy_required',422)
        node = nodes[0]
        # Editing the wire material of a bound node changes its fixed egress.
        if references(session,row.id,store):
            raise DomainError('proxy_in_use',409)
        data.update(scheme=node['scheme'],identity=node['identity'],slot_identity=node.get('slot_identity',node['identity']),target_check_required=True)
        if node.get('kernel'):
            from .store import uid
            material = session.scalar(select(Resource).where(Resource.kind=='proxy_kernel_source',Resource.name==row.id))
            if material is None:
                material = Resource(id=uid(),kind='proxy_kernel_source',name=row.id,data='{}',updated_at=store.clock())
                session.add(material)
            material.secret = store.cipher.encrypt(json.dumps(node['kernel'],sort_keys=True).encode()).decode()
            material.data = json.dumps({'protocol':node['scheme'],'identity':node['identity']})
            material.updated_at = store.clock()
            row.secret = None
            data.update(kernel_state='pending',status='pending_kernel')
        else:
            row.secret = store.cipher.encrypt(node['url'].encode()).decode()
            for material in session.scalars(select(Resource).where(Resource.kind=='proxy_kernel_source',Resource.name==row.id)):
                session.delete(material)
            data.pop('kernel_state',None)
            data['status']='untested'
    if body.url is not None and old_status in {'disabled','retired'}:
        data['status']=old_status
    if body.enabled is False:
        data['status']='disabled'
    elif body.enabled is True:
        from .proxy_protocols import KERNEL_PROTOCOLS
        data['status']='pending_kernel' if data.get('scheme') in KERNEL_PROTOCOLS else 'untested'
        if data.get('scheme') in KERNEL_PROTOCOLS:
            data['kernel_state']='pending'
            row.secret=None
        data.pop('retired_reason',None)
        data.pop('deleted',None)
    row.data,row.version,row.updated_at=json.dumps(data),row.version+1,store.clock()


def delete_node(store,session,row):
    if references(session,row.id,store):
        raise DomainError('proxy_in_use',409)
    from .subscriptions import source_ids
    data=json.loads(row.data)
    if source_ids(data):
        data.update(status='retired',deleted=True,retired_reason='user_deleted')
        row.data,row.version,row.updated_at=json.dumps(data),row.version+1,store.clock()
        return
    for material in session.scalars(select(Resource).where(Resource.kind=='proxy_kernel_source',Resource.name==row.id)):
        session.delete(material)
    session.delete(row)


class NewNode(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str=Field(min_length=1,max_length=100)
    url: SecretStr

class Binding(BaseModel):
    model_config=ConfigDict(extra='forbid')
    proxy_id: str | None=None

class Probe(BaseModel):
    model_config=ConfigDict(extra='forbid')
    platform: str


def mount_proxy_management(app,store,registry):
    from fastapi import Query
    @app.get('/api/proxy-directory')
    def directory(q: str = Query('',max_length=100),source: str='',protocol: str='',status: str='',page: int=Query(1,ge=1),page_size: int=Query(50,ge=1,le=100),sort: Literal['name','updated','protocol']='name'):
        stmt=query_nodes(q,source,protocol,status,store.engine.dialect.name)
        with store.read() as (s,_):
            total=s.scalar(select(func.count()).select_from(stmt.subquery()))
            order={'name':Resource.name,'updated':Resource.updated_at.desc(),'protocol':metadata(store.engine.dialect.name)['scheme'].as_string()}[sort]
            rows=list(s.scalars(stmt.order_by(order,Resource.id).limit(page_size).offset((page-1)*page_size)))
            bound_id=metadata(store.engine.dialect.name)['proxy_id'].as_string()
            counts=dict(s.execute(select(bound_id,func.count()).where(Resource.kind=='proxy_binding',bound_id.in_([r.id for r in rows])).group_by(bound_id)).all())
            return {'nodes':[public(r,store)|{'account_bindings':counts.get(r.id,0)} for r in rows],'total':total,'page':page,'page_size':page_size}

    @app.get('/api/proxy-directory/{identity}/references')
    def node_references(identity: str,page: int=Query(1,ge=1),page_size: int=Query(25,ge=1,le=100)):
        kinds=['proxy_binding','proxy_pool','proxy_route','maintenance_proxy_route','proxy_affinity','registration','registration_batch']
        stmt=select(Resource).where(Resource.kind.in_(kinds),Resource.data.contains(json.dumps(identity),autoescape=True))
        with store.read() as (session,_):
            node=session.get(Resource,identity)
            if node is None or node.kind!='proxy':
                raise DomainError('proxy_not_found',404)
            total=session.scalar(select(func.count()).select_from(stmt.subquery()))
            rows=session.scalars(stmt.order_by(Resource.kind,Resource.id).limit(page_size).offset((page-1)*page_size))
            return {'items':[{'id':r.id,'kind':r.kind,'name':r.name,'status':json.loads(r.data).get('status')} for r in rows],
                    'total':total,'page':page,'page_size':page_size}

    @app.put('/api/proxy-directory/{identity}')
    def edit(identity: str,body: NodeEdit):
        with store.tx() as (s,state):
            row=s.get(Resource,identity)
            if row is None or row.kind!='proxy':
                raise DomainError('proxy_not_found',404)
            mutate_node(store,s,row,body)
            store.emit(s,state,'proxy','updated')
            return public(row,store)

    @app.delete('/api/proxy-directory/{identity}')
    def remove(identity: str,expected_version: int):
        with store.tx() as (s,state):
            row=s.get(Resource,identity)
            if row is None or row.kind!='proxy':
                raise DomainError('proxy_not_found',404)
            if row.version!=expected_version:
                raise DomainError('resource_version_conflict',409)
            delete_node(store,s,row)
            store.emit(s,state,'proxy','deleted')
            return {'deleted':identity}

    @app.post('/api/proxy-directory/batch')
    def batch(body: BatchInput):
        ids=list(dict.fromkeys(body.ids))
        with store.tx() as (s,state):
            rows=list(s.scalars(select(Resource).where(Resource.kind=='proxy',Resource.id.in_(ids))))
            if len(rows)!=len(ids):
                raise DomainError('proxy_not_found',404)
            for row in rows:
                if body.action=='delete':
                    delete_node(store,s,row)
                else:
                    mutate_node(store,s,row,NodeEdit(expected_version=row.version,enabled=body.action=='enable'))
            store.emit(s,state,'proxy','batch_'+body.action)
            return {'affected':len(rows),'ids':ids}

    @app.get('/api/proxies')
    def legacy_list(q: str=Query('',max_length=100),source: str='',protocol: str='',status: str='',page: int=Query(1,ge=1),page_size: int=Query(50,ge=1,le=100),sort: Literal['name','updated','protocol']='name'):
        return directory(q,source,protocol,status,page,page_size,sort)

    @app.post('/api/proxies',status_code=201)
    def add(body: NewNode):
        from .store import uid
        identity=uid()
        with store.tx() as (s,state):
            row=Resource(id=identity,kind='proxy',name=body.name,version=0,data='{"manual_source": true}',updated_at=store.clock())
            s.add(row)
            mutate_node(store,s,row,NodeEdit(expected_version=0,url=body.url))
            store.emit(s,state,'proxy','created')
            return {'id':identity}

    @app.put('/api/accounts/{account_id}/proxy')
    def bind(account_id: str,body: Binding):
        from .store import uid
        from .proxy_pool import _usable
        with store.tx() as (session,state):
            store.account(session,account_id)
            if body.proxy_id and not _usable(session.get(Resource,body.proxy_id)):
                raise DomainError('proxy_unavailable',422)
            current=session.scalar(select(Resource).where(Resource.kind=='proxy_binding',Resource.name==account_id))
            if current:
                session.delete(current)
            if body.proxy_id:
                session.add(Resource(id=uid(),kind='proxy_binding',name=account_id,data=json.dumps({'proxy_id':body.proxy_id}),updated_at=store.clock()))
            store.emit(session,state,'proxy','binding_updated',account_id)
        return {'ok':True}

    @app.post('/api/proxies/{identity}/check')
    async def check(identity: str,body: Probe):
        from .proxy_target_health import ProxyTargetHealth
        return await ProxyTargetHealth(store,enabled=registry.enabled).check(identity,body.platform)

    @app.get('/api/accounts/{account_id}/proxy')
    def binding(account_id: str):
        from .models import Account
        with store.read() as (s,_):
            if s.get(Account,account_id) is None:
                raise DomainError('account_not_found',404)
            row=s.scalar(select(Resource).where(Resource.kind=='proxy_binding',Resource.name==account_id))
            identity=json.loads(row.data).get('proxy_id') if row else None
            node=s.get(Resource,identity) if identity else None
            return {'proxy_id':identity,'proxy_name':node.name if node else None,'status':json.loads(node.data).get('status') if node else None}
