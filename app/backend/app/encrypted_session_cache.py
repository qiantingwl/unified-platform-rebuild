"""Persistent encrypted upstream session cache, isolated from public resources."""
import json,hashlib
from sqlalchemy import select
from .models import Resource
from .store import uid

class EncryptedSessionCache:
    def __init__(self,store): self.store=store
    def get(self,key):
        with self.store.read() as (s,_):
            row=s.scalar(select(Resource).where(Resource.kind=='private_session_cache',Resource.name==key))
            if not row: return None
            metadata=json.loads(row.data)
            if metadata.get('expires_at',0)<=self.store.clock(): return None
            return json.loads(self.store.cipher.decrypt(row.secret.encode()))
    def put(self,key,value,ttl_seconds=3600):
        if len(key)!=64: raise ValueError('invalid_session_cache_key')
        with self.store.tx(scope='session-cache:'+key) as (s,_):
            row=s.scalar(select(Resource).where(Resource.kind=='private_session_cache',Resource.name==key))
            if not row:
                row=Resource(id=uid(),kind='private_session_cache',name=key,data='{}',updated_at=self.store.clock());s.add(row)
            row.data=json.dumps({'expires_at':self.store.clock()+ttl_seconds})
            row.secret=self.store.cipher.encrypt(json.dumps(value).encode()).decode();row.updated_at=self.store.clock()
    def delete(self,key):
        with self.store.tx(scope='session-cache:'+key) as (s,_):
            row=s.scalar(select(Resource).where(Resource.kind=='private_session_cache',Resource.name==key))
            if row:s.delete(row)


    def claim(self,key,ttl_seconds=90):
        from uuid import uuid4
        with self.store.tx(scope='session-cache:'+key) as (s,_):
            row=s.scalar(select(Resource).where(Resource.kind=='private_session_claim',Resource.name==key))
            old=json.loads(row.data) if row else {}
            if old.get('expires_at',0)>self.store.clock():return None
            token=uuid4().hex
            if not row:
                row=Resource(id=uid(),kind='private_session_claim',name=key,data='{}',updated_at=self.store.clock());s.add(row)
            row.data=json.dumps({'token':token,'expires_at':self.store.clock()+ttl_seconds});row.updated_at=self.store.clock()
            return token

    def publish(self,key,token,value,ttl_seconds=3600):
        with self.store.tx(scope='session-cache:'+key) as (s,_):
            claim=s.scalar(select(Resource).where(Resource.kind=='private_session_claim',Resource.name==key))
            data=json.loads(claim.data) if claim else {}
            if data.get('token')!=token or data.get('expires_at',0)<=self.store.clock():return False
            row=s.scalar(select(Resource).where(Resource.kind=='private_session_cache',Resource.name==key))
            if not row:
                row=Resource(id=uid(),kind='private_session_cache',name=key,data='{}',updated_at=self.store.clock());s.add(row)
            row.data=json.dumps({'expires_at':self.store.clock()+ttl_seconds})
            row.secret=self.store.cipher.encrypt(json.dumps(value).encode()).decode();row.updated_at=self.store.clock()
            s.delete(claim)
            return True

    def release(self,key,token):
        with self.store.tx(scope='session-cache:'+key) as (s,_):
            row=s.scalar(select(Resource).where(Resource.kind=='private_session_claim',Resource.name==key))
            if row and json.loads(row.data).get('token')==token:s.delete(row)
