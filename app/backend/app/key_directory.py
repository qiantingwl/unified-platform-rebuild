"""Safe API key listing and reversible disable, distinct from revocation."""
from fastapi import Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select,func,or_
from .models import ApiKey
from .store import DomainError
from .key_policy import read

class KeySettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:bool

def mount_key_directory(app,store):
    @app.get('/api/keys')
    def keys(page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=200),q:str='',platform:str='',status:str=''):
        with store.read() as (s,_):
            filters=[]
            if q: filters.append(or_(ApiKey.name.icontains(q,autoescape=True),ApiKey.prefix.icontains(q,autoescape=True)))
            if platform: filters.append(ApiKey.platform==platform)
            if status=='revoked': filters.append(ApiKey.revoked_at.is_not(None))
            if status in ('enabled','disabled'): filters.extend([ApiKey.revoked_at.is_(None),ApiKey.enabled.is_(status=='enabled')])
            total=s.scalar(select(func.count()).select_from(ApiKey).where(*filters))
            rows=s.scalars(select(ApiKey).where(*filters).order_by(ApiKey.created_at.desc(),ApiKey.id).offset((page-1)*page_size).limit(page_size))
            items=[]
            for row in rows:
                data={k:getattr(row,k) for k in ('id','name','platform','model','prefix','enabled','created_at','revoked_at')}
                data.update(revoked=row.revoked_at is not None,policy=read(s,row));items.append(data)
            return dict(items=items,total=total,page=page,page_size=page_size,as_of=store.clock())

    @app.patch('/api/keys/{key_id}')
    def update(key_id:str,body:KeySettings):
        with store.tx() as (s,state):
            row=s.get(ApiKey,key_id)
            if row is None: raise DomainError('key_not_found',404)
            if row.revoked_at is not None: raise DomainError('key_revoked',409)
            row.enabled=body.enabled
            store.emit(s,state,'api_key','enabled' if body.enabled else 'disabled')
            return dict(id=row.id,enabled=row.enabled,revoked_at=row.revoked_at)
