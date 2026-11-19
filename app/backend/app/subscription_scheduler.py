"""Persistent subscription refresh leases; only the tasks role runs the loop."""
import asyncio
import json
from uuid import uuid4

from sqlalchemy import select

from .models import Resource
from .store import DomainError


async def fetch_content(transport,url,progress=None):
    """Read a bounded feed. Never persist or expose its URL or raw body."""
    try:
        async with asyncio.timeout(30):
            async with transport.client.stream('GET',url,follow_redirects=False) as response:
                if response.status_code!=200:
                    raise DomainError('subscription_fetch_failed',502)
                length=response.headers.get('content-length','')
                total=int(length) if length.isdigit() else None
                if total is not None and total>2*1024*1024:
                    raise DomainError('subscription_too_large',413)
                chunks,size,last=[],0,0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>2*1024*1024:
                        raise DomainError('subscription_too_large',413)
                    chunks.append(chunk)
                    if progress and size-last>=65536:
                        await progress('fetching',received_bytes=size,total_bytes=total)
                        last=size
                if progress:
                    await progress('parsing',received_bytes=size,total_bytes=total)
        return b''.join(chunks).decode('utf-8-sig')
    except DomainError:
        raise
    except (UnicodeError,TimeoutError):
        raise DomainError('subscription_invalid_response',502) from None
    except Exception:
        raise DomainError('subscription_fetch_failed',502) from None


class SubscriptionScheduler:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry
        self.last_tick = None
        self.last_error = None
        self.running = False

    def request_sync(self,identity):
        if not self.registry.enabled:
            raise DomainError('network_execution_disabled',409)
        with self.store.tx() as (s,state):
            row=s.get(Resource,identity)
            if not row or row.kind!='proxy_subscription' or not row.secret:
                raise DomainError('subscription_url_missing',404)
            data=json.loads(row.data);now=self.store.clock()
            if data.get('enabled') is False:
                raise DomainError('subscription_disabled',409)
            if data.get('sync_requested') or data.get('sync_lease',{}).get('until',0)>now:
                raise DomainError('subscription_sync_in_progress',409)
            data.update(sync_requested=True,status='refresh_queued',next_sync_at=now,
                        sync_progress={'phase':'queued','requested_at':now})
            row.data,row.updated_at=json.dumps(data),now
            self.store.emit(s,state,'proxy_subscription','refresh_queued')
        return {'id':identity,'status':'queued'}

    def progress(self,identity,token,phase,**values):
        with self.store.tx() as (s,state):
            row=s.get(Resource,identity)
            data=json.loads(row.data) if row else {}
            if data.get('sync_lease',{}).get('token')!=token:
                raise DomainError('subscription_sync_superseded',409)
            data['sync_progress']={**data.get('sync_progress',{}),'phase':phase,**values}
            row.data=json.dumps(data)
            self.store.emit(s,state,'proxy_subscription','refresh_progress')

    def config(self, identity, token):
        with self.store.read() as (s,_):
            row=s.get(Resource,identity)
            data=json.loads(row.data) if row else {}
            if data.get('sync_lease',{}).get('token')!=token:
                raise DomainError('subscription_sync_superseded',409)
            return data

    def claim(self, identity, due=False):
        with self.store.tx() as (s, state):
            row = s.get(Resource, identity)
            if row is None or row.kind != 'proxy_subscription' or not row.secret:
                raise DomainError('subscription_url_missing', 404)
            data = json.loads(row.data)
            now = self.store.clock()
            if data.get('enabled') is False:
                if due:
                    return None
                raise DomainError('subscription_disabled',409)
            if data.get('sync_lease', {}).get('until', 0) > now:
                raise DomainError('subscription_sync_in_progress', 409)
            if due and not data.get('sync_requested') and (not data.get('auto_update', False) or data.get('next_sync_at', 0) is None or data.get('next_sync_at', 0) > now):
                return None
            token = uuid4().hex
            data.pop('sync_requested',None)
            data['sync_lease'] = {'token':token, 'until':now + 300}
            data['last_attempt_at'] = now
            data.update(status='refresh_running',sync_progress={'phase':'fetching','started_at':now,'received_bytes':0})
            row.data, row.updated_at = json.dumps(data), now
            self.store.emit(s, state, 'proxy_subscription', 'refresh_started')
            return token, self.store.cipher.decrypt(row.secret.encode()).decode()

    def fail(self, identity, token, code):
        with self.store.tx() as (s, state):
            row = s.get(Resource, identity)
            if row is None:
                return
            data = json.loads(row.data)
            if data.get('sync_lease', {}).get('token') != token:
                return
            data.pop('sync_lease', None)
            data.update(status='refresh_failed', last_error=code, last_attempt_at=self.store.clock())
            data.update(last_failure_at=self.store.clock(),last_failure_code=code,
                        sync_progress={'phase':'failed','finished_at':self.store.clock()})
            data['next_sync_at'] = self.store.clock() + data.get('interval_minutes', 360) * 60 if data.get('auto_update', False) else None
            row.data, row.updated_at = json.dumps(data), self.store.clock()
            self.store.emit(s, state, 'proxy_subscription', 'refresh_failed')

    async def sync(self, identity, *, due=False):
        if not self.registry.enabled:
            raise DomainError('network_execution_disabled', 409)
        claim_task = asyncio.create_task(asyncio.to_thread(self.claim, identity, due))
        try:
            claimed = await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            claimed = await claim_task
            if claimed is not None:
                await asyncio.shield(asyncio.to_thread(self.fail, identity, claimed[0], 'cancelled'))
            raise
        if claimed is None:
            return None
        token, url = claimed
        transport = None
        try:
            transport = self.registry.transport_factory(proxy=None)
            from .subscriptions import parse_subscription, save_nodes
            config=await asyncio.to_thread(self.config,identity,token)
            metadata=None
            if config.get('source_profile')=='leidahub_fast':
                from .proxy_recommendations import fetch_recommended
                nodes,rejected,metadata=await fetch_recommended(transport,limit=config.get('recommendation_limit',100))
            else:
                async def report(phase,**values):
                    await asyncio.to_thread(self.progress,identity,token,phase,**values)
                text=await fetch_content(transport,url,report)
                nodes, rejected = await asyncio.to_thread(parse_subscription,text)
            if not nodes:
                # Empty/malformed HTTP success is not evidence all old nodes
                # vanished. Retain existing sources and their working nodes.
                raise DomainError('subscription_empty', 422)
            await asyncio.to_thread(self.progress,identity,token,'saving',parsed=len(nodes),rejected=len(rejected))
            pending = asyncio.create_task(asyncio.to_thread(save_nodes, self.store, identity, nodes, rejected, token, metadata))
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self.fail, identity, token, 'cancelled'))
            raise
        except DomainError as error:
            await asyncio.to_thread(self.fail, identity, token, error.code)
            raise
        except Exception:
            await asyncio.to_thread(self.fail, identity, token, 'subscription_fetch_failed')
            raise DomainError('subscription_fetch_failed', 502) from None
        finally:
            if transport:
                try:
                    await transport.close()
                except Exception:
                    pass

    def due_ids(self):
        with self.store.read() as (s, _):
            rows = list(s.scalars(select(Resource).where(Resource.kind=='proxy_subscription').order_by(Resource.updated_at)))
            now = self.store.clock()
            return [r.id for r in rows if r.secret and (d:=json.loads(r.data)).get('enabled') is not False
                    and (d.get('sync_requested') or (d.get('auto_update', False) and d.get('next_sync_at', 0) is not None and d.get('next_sync_at', 0)<=now))
                    and d.get('sync_lease', {}).get('until',0)<=now][:4]

    async def tick(self):
        self.last_tick = self.store.clock()
        if not self.registry.enabled:
            return 0
        count = 0
        for identity in await asyncio.to_thread(self.due_ids):
            try:
                result = await self.sync(identity, due=True)
                count += result is not None
            except DomainError as error:
                self.last_error = error.code
            else:
                self.last_error = None
        return count

    async def run(self):
        self.running = True
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.last_error = 'subscription_scheduler_failed'
                await asyncio.sleep(30)
        finally:
            self.running = False
