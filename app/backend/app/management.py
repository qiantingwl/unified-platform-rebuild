import asyncio
import json
import time
from urllib.parse import urlsplit

from fastapi import Request, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, func, case, cast, Float, and_

from .models import Account, Resource
from .store import DomainError, PLATFORMS
from .upstream import UpstreamError
from .model_catalog import catalog_details


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProxyInput(Body):
    name: str = Field(min_length=1, max_length=100)
    url: SecretStr


class BindingInput(Body):
    proxy_id: str | None = None


class ProbeInput(Body):
    platform: str


class InviteInput(Body):
    code: SecretStr = Field(min_length=1, max_length=128)


PROBE_URLS = {"Loomy": "https://loomyad.xunfei.cn", "小浣熊": "https://xiaohuanxiong.com", "WPS 灵犀": "https://lingxi.wps.cn", "WorkBuddy 国内": "https://copilot.tencent.com", "WorkBuddy 国际": "https://www.workbuddy.ai", "CodeArts": "https://snap-access.cn-north-4.myhuaweicloud.com", "Marvis": "https://yybadaccess.3g.qq.com"}


def proxy_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or not parsed.port or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError()
    except ValueError:
        raise DomainError("invalid_proxy_url", 422) from None
    return parsed


def catalog_models(value):
    return [row["id"] for row in catalog_details(value)]


def mount_management(app, store, registry):
    from .proxy_pool import mount_proxy_pools
    mount_proxy_pools(app, store)
    from .maintenance_proxy import mount_maintenance_proxy
    mount_maintenance_proxy(app, store)

    @app.get("/api/capabilities")
    def capabilities():
        return {"platforms": registry.capabilities(), "network_enabled": registry.enabled, "protocols": ["chat", "responses", "messages"], "live_verified": False}

    from .proxy_management import mount_proxy_management
    mount_proxy_management(app, store, registry)

    @app.get("/api/catalogs")
    def catalogs():
        return {"catalogs": store.resources("model_catalog")}

    def adapter_for(account_id):
        with store.read() as (s, _):
            a = store.account(s, account_id)
            platform, version = a.platform, a.credential_version
        return registry.make(platform, store.credential(account_id, version), account_id, version, store.account_proxy(account_id))

    @app.post("/api/accounts/{account_id}/models/sync")
    async def sync_models(account_id: str):
        adapter = adapter_for(account_id)
        try:
            details = catalog_details(await adapter.models())
            names = [row["id"] for row in details]
        finally:
            await adapter.t.close()
        with store.read() as (s, _):
            platform = store.account(s, account_id).platform
        previous = next((r for r in store.resources("model_catalog") if r["name"] == platform), None)
        data = {**(previous["data"] if previous else {}), "models": names, "model_details": details, "source_account_id": account_id, "observed_at": store.clock()}
        identity = (await asyncio.to_thread(store.resource_put, "model_catalog", platform, data, resource_id=previous["id"] if previous else None, expected_version=previous["version"] if previous else None))
        return {"id": identity, "models": names, "model_details": details}

    from .invitations import Invitations
    invitations_service = Invitations(store, registry)
    app.state.invitations = invitations_service
    app.state.worker.invitations = invitations_service

    @app.get("/api/invitations")
    def invitations(page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=200),platform:str='',status:str=''):
        from .registration_queries import field
        from .account_directory import identity
        with store.read() as (s,_):
            account_field=field(s,'account_id')
            from sqlalchemy import JSON
            lease=func.json_extract(Resource.data,'$.lease_until') if s.get_bind().dialect.name=='sqlite' else cast(Resource.data,JSON)['lease_until'].as_string()
            effective=case((and_(field(s,'status').in_(['running','submitting']),cast(lease,Float)<=store.clock()),'uncertain'),else_=field(s,'status'))
            ranked=select(Resource.id,func.row_number().over(partition_by=account_field,order_by=(Resource.updated_at.desc(),Resource.id.desc())).label('rn')).where(Resource.kind=='invitation').subquery()
            filters=[Resource.kind=='invitation',Resource.id.in_(select(ranked.c.id).where(ranked.c.rn==1))]
            if platform: filters.append(account_field.in_(select(Account.id).where(Account.platform==platform)))
            counts=dict(s.execute(select(effective,func.count()).where(*filters).group_by(effective)).all())
            if status:filters.append(effective==status)
            total=s.scalar(select(func.count()).select_from(Resource).where(*filters))
            rows=list(s.scalars(select(Resource).where(*filters).order_by(Resource.updated_at.desc(),Resource.id.desc()).offset((page-1)*page_size).limit(page_size)))
            ids=[json.loads(r.data).get('account_id') for r in rows]
            accounts={a.id:a for a in s.scalars(select(Account).where(Account.id.in_(ids)))}
            records=[]
            for row in rows:
                data=json.loads(row.data)
                if data.get('status') in {'running','submitting'} and data.get('lease_until',0)<=store.clock():data.update(status='uncertain',error='executor_lost')
                for name in ('owner','lease_until','job'):data.pop(name,None)
                safe=identity(accounts[data['account_id']]) if data.get('account_id') in accounts else {}
                records.append(dict(id=row.id,name=row.name,data=data,version=row.version,updated_at=row.updated_at,account_display_name=safe.get('display_name'),identity_hint=safe.get('identity_hint')))
            return dict(records=records,total=total,page=page,page_size=page_size,counts=counts,as_of=store.clock(),last_tick=invitations_service.last_tick,last_error=invitations_service.last_error)

    @app.get("/api/accounts/{account_id}/invitation/history")
    def invitation_history(account_id: str,page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=200)):
        from .registration_queries import field
        with store.read() as (s, _):
            store.account(s, account_id)
            filters=[Resource.kind.in_(['invitation','invitation_attempt']),field(s,'account_id')==account_id]
            total=s.scalar(select(func.count()).select_from(Resource).where(*filters))
            records=[]
            for row in s.scalars(select(Resource).where(*filters).order_by(Resource.updated_at.desc(),Resource.id.desc()).offset((page-1)*page_size).limit(page_size)):
                data=json.loads(row.data)
                for name in ('owner','lease_until','job'):data.pop(name,None)
                records.append(dict(id=row.id,kind=row.kind,name=row.name,data=data,version=row.version,updated_at=row.updated_at))
            return dict(records=records,total=total,page=page,page_size=page_size,as_of=store.clock())

    @app.post("/api/accounts/{account_id}/invitation/read")
    async def read_invitation(account_id: str):
        return await asyncio.to_thread(invitations_service.queue, account_id)

    @app.post("/api/accounts/{account_id}/invitation/bind")
    async def bind_invitation(account_id: str, body: InviteInput):
        return await asyncio.to_thread(invitations_service.queue, account_id, body.code.get_secret_value())
