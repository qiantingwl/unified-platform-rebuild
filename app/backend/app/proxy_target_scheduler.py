"""Persistent, explicitly scoped target-check batches; no default full scan."""
import asyncio
import json
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, cast, func, select, type_coerce

from .models import Resource
from .proxy_target_health import KIND as CHECK_KIND, TARGET_URLS, TARGET_GROUPS, MAX_BATCH_CHECKS
from .store import DomainError, uid

BATCH_KIND = "proxy_target_batch"


class BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proxy_ids: list[str] = Field(min_length=1, max_length=256)
    platforms: list[str] = Field(min_length=1, max_length=7)


class ProxyTargetScheduler:
    def __init__(self, store, checker):
        self.store, self.checker = store, checker
        self.owner = uid()
        self.running = False
        self.last_error = None
        self.last_tick = None
        self._tick_lock = asyncio.Lock()

    def _insert(self, s, state, ids, platforms, source=None):
        ids, platforms = list(dict.fromkeys(ids)), list(dict.fromkeys(platforms))
        if not ids or not platforms or len(ids) * len(platforms) > MAX_BATCH_CHECKS:
            raise DomainError("invalid_proxy_check_batch", 422)
        if any(platform not in TARGET_URLS for platform in platforms):
            raise DomainError("unsupported_platform", 422)
        nodes = {r.id:r for r in s.scalars(select(Resource).where(Resource.kind=="proxy",Resource.id.in_(ids)))}
        if any(identity not in nodes for identity in ids):
            raise DomainError("proxy_not_found",404)
        items = [{"proxy_id":identity,"node_version":nodes[identity].version,"platform":platform} for identity in ids for platform in platforms]
        data = {"status":"queued","cursor":0,"total":len(items),"items":items,"counts":{},"source_subscription":source,"created_at":self.store.clock(),"lease_until":0}
        row=Resource(id=uid(),kind=BATCH_KIND,name="平台目标检测",data=json.dumps(data),updated_at=self.store.clock())
        s.add(row)
        self.store.emit(s,state,BATCH_KIND,"queued")
        return {"id":row.id,"status":"queued","total":len(items)}

    def queue(self, ids, platforms):
        if not self.checker.enabled:
            raise DomainError("network_execution_disabled",409)
        with self.store.tx() as (s,state):
            return self._insert(s,state,ids,platforms)

    def discover(self, force=False):
        """At most one 256-check page per enabled subscription; continue later."""
        if not self.checker.enabled:
            return 0
        created=0
        with self.store.tx() as (s,state):
            clock_row=s.scalar(select(Resource).where(Resource.kind=="proxy_target_discovery",Resource.name=="global"))
            now=self.store.clock()
            if not force and clock_row and now-json.loads(clock_row.data).get("last_checked_at",0)<60:
                return 0
            if clock_row is None:
                clock_row=Resource(id=uid(),kind="proxy_target_discovery",name="global",data="{}",updated_at=now)
                s.add(clock_row)
            clock_row.data=json.dumps({"last_checked_at":now})
            clock_row.updated_at=now
            meta=cast(Resource.data,JSON) if self.store.engine.dialect.name=='postgresql' else type_coerce(Resource.data,JSON)
            batches=[json.loads(r.data) for r in s.scalars(select(Resource).where(Resource.kind==BATCH_KIND,meta['status'].as_string().in_(['queued','running'])))]
            active={d.get("source_subscription") for d in batches if d["status"] in {"queued","running"}}
            planned={(item["proxy_id"],item["platform"]) for d in batches if d["status"] in {"queued","running"} for item in d["items"][d["cursor"]:]}
            checks={r.name:json.loads(r.data) for r in s.scalars(select(Resource).where(Resource.kind==CHECK_KIND))}
            nodes=list(s.scalars(select(Resource).where(Resource.kind=="proxy").order_by(Resource.id)))
            for subscription in s.scalars(select(Resource).where(Resource.kind=="proxy_subscription").order_by(Resource.id)):
                config=json.loads(subscription.data)
                platforms=list(dict.fromkeys(config.get("target_platforms") or []))
                if not config.get("auto_target_check") or config.get("enabled") is False or not platforms or any(p not in TARGET_URLS for p in platforms) or subscription.id in active:
                    continue
                # One page may contain different target deficits. Build exact
                # pairs so a failed platform never forces unrelated checks.
                candidates=[]
                for node in nodes:
                    meta=json.loads(node.data)
                    sources=set(meta.get("subscription_ids") or ([meta["subscription_id"]] if meta.get("subscription_id") else []))
                    if subscription.id not in sources or not meta.get("target_check_required") or not node.secret or meta.get("status") in {"disabled","retired","pending_kernel"}:
                        continue
                    for platform in platforms:
                        if (node.id,platform) in planned:continue
                        previous=checks.get(f"{node.id}:{platform}",{})
                        age=self.store.clock()-(previous.get("checked_at") or 0)
                        if previous.get("node_version")==node.version and previous.get("inflight_check_id") and self.store.clock()-previous.get("inflight_started_at",0)<120:
                            continue
                        interval=23*3600 if previous.get("eligible") else 15*60
                        if previous.get("node_version")==node.version and 0<=age<interval:
                            continue
                        unseen=not previous or previous.get("node_version")!=node.version
                        candidates.append(((0 if unseen else 1,previous.get("checked_at") or 0,node.id,platform),{"proxy_id":node.id,"node_version":node.version,"platform":platform}))
                # Untested/changed nodes before retries; then oldest observation.
                # Failed early nodes cannot starve the untouched tail of a large source.
                pending=[item for _,item in sorted(candidates,key=lambda item:item[0])[:MAX_BATCH_CHECKS]]
                if pending:
                    row=Resource(id=uid(),kind=BATCH_KIND,name="订阅平台目标检测",data=json.dumps({"status":"queued","cursor":0,"total":len(pending),"items":pending,"counts":{},"source_subscription":subscription.id,"created_at":self.store.clock(),"lease_until":0}),updated_at=self.store.clock())
                    s.add(row)
                    planned.update((item["proxy_id"],item["platform"]) for item in pending)
                    self.store.emit(s,state,BATCH_KIND,"queued")
                    created+=1
        return created

    def _claim(self):
        with self.store.tx() as (s,state):
            now=self.store.clock()
            meta=cast(Resource.data,JSON) if self.store.engine.dialect.name=='postgresql' else type_coerce(Resource.data,JSON)
            rows=list(s.scalars(select(Resource).where(Resource.kind==BATCH_KIND,meta['status'].as_string().in_(['queued','running'])).order_by(Resource.updated_at,Resource.id)))
            if any(json.loads(row.data).get("lease_until",0)>now for row in rows):
                return None
            for row in rows:
                data=json.loads(row.data)
                if data["status"] not in {"queued","running"} or data.get("lease_until",0)>now:
                    continue
                if data.get("source_subscription"):
                    source=s.get(Resource,data["source_subscription"])
                    config=json.loads(source.data) if source and source.kind=="proxy_subscription" else {}
                    permitted=set(config.get("target_platforms") or [])
                    if not config.get("auto_target_check") or config.get("enabled") is False or any(item["platform"] not in permitted for item in data["items"][data["cursor"]:]):
                        data.update(status="cancelled",finished_at=now)
                        row.data=json.dumps(data)
                        row.updated_at=now
                        self.store.emit(s,state,BATCH_KIND,"cancelled")
                        continue
                token=uid()
                data.update(status="running",owner=self.owner,lease_token=token,lease_until=now+120)
                row.data=json.dumps(data)
                row.updated_at=now
                self.store.emit(s,state,BATCH_KIND,"running")
                return {"id":row.id,"token":token,"items":data["items"][data["cursor"]:data["cursor"]+2],"cursor":data["cursor"],"source_subscription":data.get("source_subscription")}

    def _item_current(self,item,source_id=None):
        with self.store.read() as (s,_):
            node=s.get(Resource,item["proxy_id"])
            if node is None or node.kind!="proxy" or node.version!=item["node_version"]:
                return False
            meta=json.loads(node.data)
            if meta.get("status") in {"disabled","retired","pending_kernel"}:
                return False
            if source_id:
                sources=set(meta.get("subscription_ids") or ([meta["subscription_id"]] if meta.get("subscription_id") else []))
                source=s.get(Resource,source_id)
                config=json.loads(source.data) if source and source.kind=="proxy_subscription" else {}
                if source_id not in sources or not config.get("auto_target_check") or config.get("enabled") is False or item["platform"] not in (config.get("target_platforms") or []):
                    return False
            return True

    def cancel(self,identity):
        with self.store.tx() as (s,state):
            row=s.get(Resource,identity)
            if row is None or row.kind!=BATCH_KIND:
                raise DomainError('proxy_batch_not_found',404)
            data=json.loads(row.data)
            if data.get('status') in {'completed','cancelled'}:
                return {'id':identity,'status':data['status']}
            data.update(status='cancelled',finished_at=self.store.clock(),lease_until=0)
            data.pop('lease_token',None)
            row.data,row.updated_at=json.dumps(data),self.store.clock()
            self.store.emit(s,state,BATCH_KIND,'cancelled')
            return {'id':identity,'status':'cancelled'}

    def _finish_chunk(self, claim, results):
        with self.store.tx() as (s,state):
            row=s.get(Resource,claim["id"])
            if not row:return False
            data=json.loads(row.data)
            if data.get("lease_token")!=claim["token"] or data["cursor"]!=claim["cursor"]:
                return False
            counts=Counter(data["counts"])
            counts.update(r["code"] for r in results)
            data.update(cursor=data["cursor"]+len(claim["items"]),counts=dict(counts),lease_until=0)
            data["status"]="completed" if data["cursor"]>=data["total"] else "queued"
            if data["status"]=="completed":data["finished_at"]=self.store.clock()
            row.data=json.dumps(data)
            row.updated_at=self.store.clock()
            self.store.emit(s,state,BATCH_KIND,data["status"])
            return True

    async def tick(self):
        if not self.checker.enabled:return 0
        async with self._tick_lock:
            await asyncio.to_thread(self.discover)
            claim=await asyncio.to_thread(self._claim)
            if not claim:return 0
            async def one(item):
                try:
                    if not await asyncio.to_thread(self._item_current,item,claim.get("source_subscription")):
                        return {"code":"proxy_changed"}
                    return await self.checker.check(item["proxy_id"],item["platform"])
                except DomainError as error:
                    return {"code":error.code}
            results=await asyncio.gather(*(one(item) for item in claim["items"]))
            await asyncio.to_thread(self._finish_chunk,claim,results)
            self.last_tick=self.store.clock()
            return len(results)

    async def run(self):
        self.running=True
        try:
            while True:
                try:
                    count=await self.tick()
                    self.last_error=None
                except asyncio.CancelledError:raise
                except Exception:
                    self.last_error="proxy_target_scheduler_error"
                    count=0
                await asyncio.sleep(1 if count else 15)
        finally:self.running=False


def register_routes(app,store,checker):
    scheduler=ProxyTargetScheduler(store,checker)
    app.state.proxy_target_scheduler=scheduler

    @app.get("/api/proxy-target-checks")
    def records(platform:str|None=None,proxy_ids:str|None=None):
        if platform is not None and platform not in TARGET_URLS:
            raise DomainError("unsupported_platform",422)
        ids=list(dict.fromkeys(proxy_ids.split(','))) if proxy_ids else None
        if ids is not None and len(ids)>100:
            raise DomainError('invalid_proxy_check_batch',422)
        with store.read() as (session,_):
            stmt=select(Resource).where(Resource.kind==CHECK_KIND)
            if ids is not None:
                names=[f'{identity}:{target}' for identity in ids for target in ([platform] if platform else TARGET_URLS)]
                stmt=stmt.where(Resource.name.in_(names))
            elif platform:
                stmt=stmt.where(Resource.name.endswith(':'+platform,autoescape=True))
            rows=session.scalars(stmt.order_by(Resource.updated_at.desc()).limit(700))
            checks=[{'id':r.id,'name':r.name,'version':r.version,'data':json.loads(r.data),'updated_at':r.updated_at} for r in rows]
        return {"checks":checks,"targets":TARGET_URLS,"target_groups":{key:list(value) for key,value in TARGET_GROUPS.items()}}

    @app.post("/api/proxy-target-checks",status_code=202)
    def queue(body:BatchInput):
        return scheduler.queue(body.proxy_ids,body.platforms)

    @app.get("/api/proxy-target-batches")
    def batches(page:int=1,page_size:int=25):
        if page<1 or not 1<=page_size<=100:
            raise DomainError('invalid_pagination',422)
        stmt=select(Resource).where(Resource.kind==BATCH_KIND)
        with store.read() as (session,_):
            total=session.scalar(select(func.count()).select_from(stmt.subquery()))
            rows=session.scalars(stmt.order_by(Resource.updated_at.desc(),Resource.id).limit(page_size).offset((page-1)*page_size))
            result=[]
            for row in rows:
                data=json.loads(row.data)
                for key in ('items','owner','lease_token'):
                    data.pop(key,None)
                result.append({'id':row.id,'name':row.name,'data':data,'updated_at':row.updated_at})
            return {'batches':result,'total':total,'page':page,'page_size':page_size}

    @app.post('/api/proxy-target-batches/{identity}/cancel')
    def cancel_batch(identity:str):
        return scheduler.cancel(identity)

    return scheduler
