"""Batch registration orchestration. Waiting for SMS never occupies a worker."""
import asyncio
import json
from collections import Counter

from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select, func
from fastapi import Query

from .models import Resource,Account,Credential
from .operations import settings
from .store import DomainError,day_at,uid
from .upstream import UpstreamError
from .registration_queries import runnable, field


class BatchInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str=Field(min_length=1,max_length=80)
    platform:str
    # Target number of successful registrations (not attempts).
    count:int=Field(ge=1,le=10000)
    sms_channel_id:str
    proxy_id:str | None=None
    # Legacy API name: limit on registration attempts, not purchased numbers.
    # None means the operator has not configured a limit.
    number_budget:int | None=Field(default=None,ge=1,le=200000)
    max_consecutive_failures:int | None=Field(default=None,ge=1,le=1000)
    # "Number already used" is a skip, not a failure; too many in a row means the pool is dry.
    max_consecutive_skips:int | None=Field(default=None,ge=1,le=10000)
    parallel:int=Field(default=4,ge=1,le=64)


class RegistrationRunner:
    def __init__(self,service,worker):
        self.service,self.store,self.worker=service,service.store,worker
        self.tasks={}
        self.last_tick=self.last_error=None
        self.next_poll={}
        # Finished sessions with nothing left to release, keyed by row timestamp.
        self.settled={}
        self.last_dispatched={}
        self.dispatch_serial=0

    @staticmethod
    def can_commit_receipt(data, material):
        """Only a complete, already issued result can resume without upstream work."""
        receipt=material.get("issued_result")
        return (data.get("status")=="uncertain" and isinstance(receipt,dict)
                and bool(receipt.get("identity")) and isinstance(receipt.get("credential"),dict)
                and bool(receipt["credential"]))

    def schedule_relogin(self, limit=8):
        """Bound planning work and rotate candidates durably, including skipped ones."""
        limit=max(1,min(256,int(limit)))
        scan_limit=max(64,limit*4)
        with self.store.tx() as (s,_):
            cursor=s.scalar(select(Resource).where(Resource.kind=="relogin_cursor",Resource.name=="automatic"))
            after=json.loads(cursor.data).get("after") if cursor else None
            query=select(Account.id).where(Account.credential_status=="invalid",Account.sandbox.is_(False)).order_by(Account.id)
            accounts=list(s.scalars((query.where(Account.id>after) if after else query).limit(scan_limit)))
            if after and len(accounts)<scan_limit:
                accounts+=list(s.scalars(query.where(Account.id<=after).limit(scan_limit-len(accounts))))
            from .native_lifecycle import control
            blocked={a.id for a in s.scalars(select(Account).where(Account.id.in_(accounts)))
                     if control(s,a)["state"]!="independent"}

        scheduled=0
        last=None
        for account_id in accounts:
            if scheduled>=limit:
                break
            last=account_id
            # Automatic credential actions require a verified independent session.
            if account_id in blocked:
                continue
            try:
                plan=self.service.recovery.plan(account_id)
                if plan["action"] in {"renew","renew_verify","login","authorize"}:
                    result=self.service.recovery.prepare(account_id)
                    if result.get("session_id") or result.get("job_id"):
                        scheduled+=1
            except DomainError as error:
                self.last_error=error.code
        if last is not None:
            with self.store.tx() as (s,_):
                cursor=s.scalar(select(Resource).where(Resource.kind=="relogin_cursor",Resource.name=="automatic"))
                if cursor is None:
                    cursor=Resource(id=uid(),kind="relogin_cursor",name="automatic",data="{}",updated_at=self.store.clock())
                    s.add(cursor)
                cursor.data,cursor.updated_at=json.dumps({"after":last}),self.store.clock()

    def make_batch(self,body):
        if body.platform not in {"Loomy","WorkBuddy 国内","WPS 灵犀","小浣熊","CodeArts"}:
            raise DomainError("batch_requires_sms_adapter",422)
        self.service.sms.read(body.sms_channel_id,for_new=True)
        with self.store.tx() as (s,state):
            return self.create_batch(s,state,body)

    def create_batch(self,s,state,body,automation_id=None):
        """Create through the existing batch path inside the caller's transaction."""
        batch=uid()
        data={"platform":body.platform,"target":body.count,"status":"active","created_at":self.store.clock(),"sessions":[],
              "sms_channel_id":body.sms_channel_id,"proxy_id":body.proxy_id,"number_budget":body.number_budget,"budget_unit":"registration_attempt",
              "max_consecutive_failures":body.max_consecutive_failures,"max_consecutive_skips":body.max_consecutive_skips,"parallel":body.parallel}
        if automation_id:
            data["automation_id"]=automation_id
        row=Resource(id=batch,kind="registration_batch",name=body.name,data=json.dumps(data),updated_at=self.store.clock())
        s.add(row)
        # The first sessions commit with the batch; a crash cannot leave half a batch.
        self.refill(s,state,row)
        self.store.emit(s,state,"registration_batch","prepared")
        return batch

    @staticmethod
    def progress(data,sessions):
        """Successes, attempts in flight and the two trailing streaks, in finishing order."""
        rows=[sessions[i] for i in data.get("sessions",[]) if i in sessions]
        succeeded=sum(r["status"]=="succeeded" and not r.get("result_unresolved") for r in rows)
        inflight=sum(r["status"] not in {"succeeded","failed","cancelled"} or r.get("result_unresolved",False) for r in rows)
        # Missing/ambiguous receipts reserve their original slot. A terminal
        # label alone must not authorize buying a replacement number.
        inflight+=sum(i not in sessions for i in data.get("sessions",[]))
        finished=sorted((r for r in rows if r["status"] in {"succeeded","failed"} and not r.get("result_unresolved")),key=lambda r:(r.get("finished_at") or 0))
        failures=skips=0
        for r in reversed(finished):
            if r["status"]=="succeeded":
                break
            if r.get("error")!="phone_previously_used":
                failures+=1
        for r in reversed(finished):
            if r.get("error")!="phone_previously_used":
                break
            skips+=1
        return {"succeeded":succeeded,"inflight":inflight,"attempts":len(data.get("sessions",[])),"failure_streak":failures,"skip_streak":skips}

    def refill(self,s,state,row):
        """Keep up to `parallel` attempts running until the success target, a stop condition
        or the number budget is reached. Stops pause the batch with a reason; nothing restarts
        it except a person pressing resume."""
        data=json.loads(row.data)
        if data.get("status")!="active" or "number_budget" not in data:
            return 0
        sessions={}
        for sid in data.get("sessions",[]):
            session=s.get(Resource,sid)
            if session is not None:
                sessions[sid]=json.loads(session.data)
        p=self.progress(data,sessions)
        stop=None
        if p["succeeded"]>=data["target"]:
            data["status"]="completed"
        elif data.get("max_consecutive_failures") is not None and p["failure_streak"]>=data["max_consecutive_failures"]:
            stop="consecutive_failures"
        elif data.get("max_consecutive_skips") is not None and p["skip_streak"]>=data["max_consecutive_skips"]:
            stop="consecutive_used_numbers"
        elif data.get("number_budget") is not None and p["attempts"]>=data["number_budget"] and not p["inflight"]:
            stop="number_budget_exhausted"
        if stop:
            data.update(status="paused",pause_reason=stop,paused_at=self.store.clock())
        created=0
        if data["status"]=="active":
            room=min(data["parallel"]-p["inflight"],data["target"]-p["succeeded"]-p["inflight"])
            if data.get("number_budget") is not None:
                room=min(room,data["number_budget"]-p["attempts"])
            for _ in range(max(0,room)):
                index=len(data["sessions"])+1
                mode={"Loomy":"register","WPS 灵犀":"browser","小浣熊":"official","CodeArts":"register"}.get(data["platform"],"sms")
                sid=self.service.create(data["platform"],f"{row.name} {index}",mode,{"sms_channel_id":data["sms_channel_id"],"auto_run":True,"batch_id":row.id,"proxy_id":data.get("proxy_id")},transaction=(s,state))
                data["sessions"].append(sid)
                created+=1
        if created or stop or data["status"]!=json.loads(row.data).get("status"):
            row.data,row.version,row.updated_at=json.dumps(data),(row.version or 1)+1,self.store.clock()
            self.store.emit(s,state,"registration_batch",stop or data["status"])
        return created

    def refill_batches(self):
        with self.store.tx() as (s,state):
            for row in s.scalars(select(Resource).where(Resource.kind=="registration_batch",field(s,"status")=="active")):
                if json.loads(row.data).get("status")=="active":
                    self.refill(s,state,row)

    def batches(self, page=1, page_size=25, platform=None, status=None):
        with self.store.read() as (s,_):
            query=select(Resource).where(Resource.kind=="registration_batch")
            if platform:
                query=query.where(field(s,"platform")==platform)
            if status:
                query=query.where(field(s,"status")==status)
            rows=list(s.scalars(query.order_by(Resource.updated_at.desc(),Resource.id).offset((page-1)*page_size).limit(page_size)))
            result=[]
            for row in rows:
                data=json.loads(row.data)
                # Only this page's batches are queried, never global history.
                ids=data.get("sessions",[])
                sessions={}
                for start in range(0,len(ids),500):
                    for item in s.scalars(select(Resource).where(Resource.kind=="registration",Resource.id.in_(ids[start:start+500]))):
                        sessions[item.id]=json.loads(item.data)
                counts=Counter(item["status"] for item in sessions.values())
                result.append({"id":row.id,"name":row.name,"version":row.version,"updated_at":row.updated_at,
                    "data":{k:v for k,v in data.items() if k!="sessions"},"session_count":len(ids),
                    "counts":dict(counts),"finished":sum(counts[k] for k in ("succeeded","failed","cancelled")),
                    "progress":self.progress(data,sessions),"skipped":sum(item.get("error")=="phone_previously_used" for item in sessions.values())})
            return result

    def batch_count(self, platform=None, status=None):
        with self.store.read() as (s,_):
            query=select(func.count()).select_from(Resource).where(Resource.kind=="registration_batch")
            if platform:
                query=query.where(field(s,"platform")==platform)
            if status:
                query=query.where(field(s,"status")==status)
            return s.scalar(query)

    async def one(self,sid):
        try:
            data,material=(await asyncio.to_thread(self.service.details, sid))
            if data["status"]=="prepared":
                await self.service.run(sid)
            elif self.can_commit_receipt(data,material):
                # _run commits issued_result before constructing a transport.
                # Never resend SMS or replay an ambiguous login request here.
                await self.service.run(sid,continuation=True)
            elif data["status"]=="waiting_code" and material.get("sms_lease_id"):
                await self.service.poll_sms(sid)
            elif data["status"]=="waiting_ticket":
                await self.service.run(sid,continuation=True)
            elif data["status"]=="waiting_authorization" and data["platform"].startswith("WorkBuddy"):
                await self.service.run(sid,continuation=True)
            data,material=(await asyncio.to_thread(self.service.details, sid))
            if data.get("result_unresolved"):
                return
            lease=material.get("sms_lease_id") or data.get("sms_lease_id")
            if data["status"] in {"succeeded","failed","cancelled"} and lease:
                async with self.service.execution(sid):
                    await self.service.sms.retire(lease,data["status"])
        except (DomainError,UpstreamError) as error:
            if error.code not in {"registration_state_conflict","registration_execution_busy","account_maintenance_busy","maintenance_capacity_busy","maintenance_priority_pending"}:
                self.last_error=error.code
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_error="registration_runner_error"
        finally:
            self.next_poll[sid]=self.store.clock()+5

    async def tick(self):
        (await asyncio.to_thread(self.service.recover))
        if self.service.registry.enabled:
            config=(await asyncio.to_thread(settings, self.store))["values"]
            if config.get("auto_relogin"):
                (await asyncio.to_thread(self.schedule_relogin, int(config.get("relogin_batch") or 8)))
            from .registration_automation import RegistrationAutomation
            await asyncio.to_thread(RegistrationAutomation(self).tick)
            await asyncio.to_thread(self.refill_batches)
            capacity=config["maintenance_capacity"]
            free=max(0,capacity-len(self.tasks))
            batches={r["id"]:r["data"]["status"] for r in await asyncio.to_thread(self.store.resources, "registration_batch")}
            rows=(await asyncio.to_thread(runnable, self.store))
            # A failed provider release must not precede a live SMS challenge.
            # Rotate equal-priority work even when polling leaves its row unchanged.
            def priority(row):
                status=row["data"]["status"]
                rank=2 if status=="prepared" else 1 if status in {"succeeded","failed","cancelled"} else 0
                return rank,self.last_dispatched.get(row["id"],0),row["updated_at"],row["id"]
            rows.sort(key=priority)
            # History grows without bound; keep per-tick work proportional to open sessions.
            live={r["id"] for r in rows}
            now=self.store.clock()
            self.settled={k:v for k,v in self.settled.items() if k in live}
            self.last_dispatched={k:v for k,v in self.last_dispatched.items() if k in live}
            self.next_poll={k:v for k,v in self.next_poll.items() if k in live and v>now}
            for row in rows:
                if free<=0:
                    break
                sid=row["id"]
                if sid in self.tasks or self.next_poll.get(sid,0)>self.store.clock():
                    continue
                if self.settled.get(sid)==row["updated_at"] or row["data"]["status"] not in {"prepared","waiting_code","waiting_ticket","waiting_authorization","uncertain","succeeded","failed","cancelled"}:
                    continue
                data,material=(await asyncio.to_thread(self.service.details, sid))
                if data["status"] not in {"prepared","waiting_code","waiting_ticket","waiting_authorization","uncertain","succeeded","failed","cancelled"}:
                    continue
                if data["status"]=="uncertain" and not self.can_commit_receipt(data,material):
                    continue
                if data["status"]=="waiting_authorization" and not data["platform"].startswith("WorkBuddy"):
                    continue
                if not material.get("auto_run") and not data.get("sms_lease_id"):
                    continue
                batch=material.get("batch_id")
                if batch and batches.get(batch)!="active" and data["status"]=="prepared":
                    continue
                if data["status"] in {"succeeded","failed","cancelled"}:
                    if data.get("result_unresolved"):
                        continue
                    lease=data.get("sms_lease_id") or material.get("sms_lease_id")
                    if not lease:
                        self.settled[sid]=row["updated_at"]
                        continue
                    status,_=(await asyncio.to_thread(self.service.sms.lease, lease))
                    if status["status"] in {"retired","provider_auto_release"}:
                        self.settled[sid]=row["updated_at"]
                        continue
                task=asyncio.create_task(self.one(sid))
                self.tasks[sid]=task
                self.dispatch_serial+=1
                self.last_dispatched[sid]=self.dispatch_serial
                task.add_done_callback(lambda _,key=sid:self.tasks.pop(key,None))
                free-=1

    async def run(self):
        try:
            while True:
                self.last_tick=self.store.clock()
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except (DomainError,UpstreamError) as error:
                    self.last_error=error.code
                except Exception:
                    self.last_error="registration_scheduler_error"
                await asyncio.sleep(5)
        finally:
            tasks=list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


def mount_registration_runner(app,runner):
    @app.get("/api/registration-batches")
    def batches(page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100),platform:str | None=None,status:str | None=None):
        return {"batches":runner.batches(page,page_size,platform,status),"total":runner.batch_count(platform,status),
                "page":page,"page_size":page_size,"active":len(runner.tasks),"last_tick":runner.last_tick,"last_error":runner.last_error}

    @app.post("/api/registration-batches",status_code=201)
    def create(body:BatchInput):
        return {"id":runner.make_batch(body)}

    @app.post("/api/registration-batches/{batch_id}/{action}")
    def control(batch_id:str,action:str):
        if action not in {"pause","resume","cancel"}:
            raise DomainError("invalid_batch_action",422)
        with runner.store.tx() as (s,state):
            row=s.get(Resource,batch_id)
            if not row or row.kind!="registration_batch":
                raise DomainError("resource_not_found",404)
            data=json.loads(row.data)
            if data["status"]=="cancelled":
                raise DomainError("batch_already_cancelled")
            if action=="cancel":
                for sid in data.get("sessions",[]):
                    session=s.get(Resource,sid)
                    if session:
                        item=json.loads(session.data)
                        if item["status"]=="prepared":
                            item.update(status="cancelled",finished_at=runner.store.clock(),error="cancelled")
                            session.data=json.dumps(item)
                            session.version+=1
                            session.updated_at=runner.store.clock()
                # Started sessions retain their receipts and finish normally.
            if action=="resume" and data["status"] not in {"paused"}:
                raise DomainError("batch_not_paused")
            row.data=json.dumps({**{k:v for k,v in data.items() if k not in {"pause_reason","paused_at"}},"status":{"pause":"paused","resume":"active","cancel":"cancelled"}[action]})
            row.version+=1
            row.updated_at=runner.store.clock()
            runner.store.emit(s,state,"registration_batch",action)
        return {"ok":True}
