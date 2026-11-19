"""Local scheduler controls, bounded resource samples and full-window analytics."""
import asyncio
import base64
import hashlib
import json
import math
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

import psutil
from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from .models import Account, Job, Attempt, ModelState, RequestLog, RequestAttempt, Resource
from .store import DomainError, TEMPORARY_MODEL_BLOCKS, day_at, uid


DEFAULT_SETTINGS = {"maintenance_capacity": 64, "daily_checkin": False, "balance_interval": 1800, "refresh_balances": False, "recovery_tests": False, "recovery_interval": 900, "startup_seconds": 120, "idle_seconds": 120, "max_attempts": 3}
DEFAULT_SETTINGS.update({"auto_renew":False,"renewal_lead_seconds":600,"auto_relogin":False,"relogin_batch":8})
DEFAULT_SETTINGS.update({"checkin_max_attempts":3,"checkin_retry_interval":1800,"daily_checkin_minute":0})
DEFAULT_SETTINGS.update({"auto_invitation":False,"invitation_interval":3600})
DEFAULT_SETTINGS.update({"placeholder_enabled":True})

#: 「账户对这个模型没有可用能力」的确定性结论：起步就拉长，别拿探测当训练。
_STUBBORN_REASONS = {"identity", "permission"}

#: 复核退避上限：一天一次。上游长期不恢复时，继续加密探测没有意义。
REVIEW_MAX_DELAY = 86400

#: 不做自动复核的原因。这几种的自动探测等价于重复制造同一个失败：
#: - identity：未实名/身份校验不过，不换实名材料就永远恢复不了；
#: - permission：上游明确「该账号对该模型无权限」，等多久都不会自己变；
#: - rate_limit / network / unknown_403：到期后回到候选，等待真实请求。
#: 结论：只保留人工「解除限制」和真实请求两条恢复路径，不再定时打上游。
#: 临时故障使用与原生调度和目录一致的到期规则。
_NO_AUTO_REVIEW = {"identity", "permission"} | TEMPORARY_MODEL_BLOCKS


def should_auto_review(reason):
    """该原因的限制是否值得定时探测。不可恢复的原因一律交给人工。"""
    return reason not in _NO_AUTO_REVIEW


def review_delay(config, block):
    """下一次复核要等多少秒。

    原来是固定 `recovery_interval`（900 秒）。上游持续拒绝时，账户会每 15 分钟
    被拉起来探测一次、必然失败、再等 15 分钟——实测 WorkBuddy 国际两个账户
    连续 12 轮 permission 全失败，一天烧近 200 次必然失败的上游请求。

    改成按已失败次数指数退避：15min → 30min → 1h → 2h → 4h → 8h → 24h（封顶）。
    `identity` / `permission` 这类确定性结论起步就给 6 小时。
    """
    base = int(config.get("recovery_interval") or 900)
    attempts = int(getattr(block, "attempts", 0) or 0)
    delay = base * (2 ** min(max(attempts, 0), 7))
    if getattr(block, "reason", "") in _STUBBORN_REASONS:
        delay = max(delay, 21600)
    return min(delay, REVIEW_MAX_DELAY)


def settings(store):
    rows = store.resources("settings")
    row = next((r for r in rows if r["name"] == "runtime"), None)
    return {"id": row["id"] if row else None, "version": row["version"] if row else 0, "values": {**DEFAULT_SETTINGS,"maintenance_capacity":int(os.getenv("UNIFIED_MAINTENANCE_CAPACITY","64")), **(row["data"] if row else {})}}


def credential_expiry(credential):
    """Normalize existing 3065/3067 expiry shapes for scheduling, never authentication."""
    from datetime import datetime, timezone
    auth=credential.get("auth") if isinstance(credential.get("auth"),dict) else {}
    values=[credential.get(key) for key in ("expires_at","expiresAt","tokenExpiresAt")]
    values.extend(auth.get(key) for key in ("expires_at","expiresAt","tokenExpiresAt"))
    # Older Raccoon imports only carried JWT exp. Reading a claim is a scheduling
    # hint; the adapter still validates both renewal and the resulting identity.
    for material in (credential,auth):
        token=next((material.get(key) for key in ("access_token","accessToken","token")
                    if isinstance(material.get(key),str) and material[key]),None)
        if token:
            try:
                encoded=token.removeprefix("Bearer ").removeprefix("bearer ").split(".")[1]
                claims=json.loads(base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)))
                if isinstance(claims,dict):
                    values.append(claims.get("exp"))
            except (ValueError,IndexError,TypeError):
                pass
    for value in values:
        if value is None or isinstance(value,bool):
            continue
        try:
            expiry=float(value)
        except (ValueError,TypeError,OverflowError):
            try:
                date=datetime.fromisoformat(str(value).replace("Z","+00:00"))
                expiry=(date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
            except (ValueError,TypeError,OverflowError,OSError):
                continue
        if math.isfinite(expiry) and expiry>0:
            return expiry/1000 if expiry>1e12 else expiry
    return None


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    maintenance_capacity: int = Field(ge=1, le=1024)
    daily_checkin: bool
    daily_checkin_minute: int = Field(default=0, ge=0, le=1439)
    refresh_balances: bool
    balance_interval: int = Field(ge=60, le=86400)
    recovery_tests: bool
    recovery_interval: int = Field(ge=60, le=86400)
    startup_seconds: int = Field(ge=5, le=600)
    idle_seconds: int = Field(ge=5, le=600)
    max_attempts: int = Field(ge=1, le=10)
    auto_renew: bool = False
    auto_relogin: bool = False
    relogin_batch: int = Field(default=8, ge=1, le=256)
    renewal_lead_seconds: int = Field(default=600,ge=60,le=86400)
    checkin_max_attempts: int = Field(default=3, ge=1, le=10)
    checkin_retry_interval: int = Field(default=1800, ge=60, le=86400)
    auto_invitation: bool = False
    invitation_interval: int = Field(default=3600, ge=300, le=86400)
    placeholder_enabled: bool = True


def write_settings(store, body):
    # Compare-and-set in one transaction, including the initial empty row.
    with store.tx() as (s, state):
        row = s.scalar(select(Resource).where(Resource.kind == "settings", Resource.name == "runtime"))
        if (row.version if row else 0) != body.expected_version:
            raise DomainError("resource_version_conflict")
        if row is None:
            row = Resource(id=uid(), kind="settings", name="runtime", version=1, updated_at=store.clock(), data="{}")
            s.add(row)
        else:
            row.version += 1
        row.data = json.dumps(body.model_dump(exclude={"expected_version"}))
        row.updated_at = store.clock()
        store.emit(s, state, "settings", "configuration_saved")


class Supervisor:
    def __init__(self, store, worker, gateway, root):
        self.store, self.worker, self.gateway, self.root = store, worker, gateway, Path(root)
        self.process = psutil.Process()
        self.process.cpu_percent(None)
        self.running, self.last_error, self.last_tick = False, None, None

    def schedule(self, config):
        self.store.reconcile_maintenance()
        # 到期的冷却（额度/权限/限流）到点自动解除，别让它变成永久封锁。
        self.store.expire_model_blocks()
        if config["daily_checkin"]:
            self.store.schedule_today(config.get("checkin_max_attempts", 3), config.get("checkin_retry_interval", 1800),
                automatic=True, daily_checkin_minute=config.get("daily_checkin_minute",0))
        now = self.store.clock()
        if config.get("auto_renew"):
            with self.store.read() as (s,_):
                from sqlalchemy import JSON,cast
                lifecycle_state=func.json_extract(Resource.data,'$.state') if s.get_bind().dialect.name=='sqlite' else cast(Resource.data,JSON)['state'].as_string()
                eligible=list(s.execute(select(Account.id,Account.credential_version,Account.platform).join(Resource,Resource.name==Account.id).where(Resource.kind=='lifecycle_control',lifecycle_state=='independent',Account.sandbox.is_(False),Account.credential_version>0,Account.credential_status!='disabled')))
            for account_id,version,platform in eligible:
                try:
                    credential=self.store.credential(account_id,version)
                    expiry=credential_expiry(credential)
                    if not expiry:
                        continue
                    if expiry-now>config["renewal_lead_seconds"]:
                        continue
                    with self.store.tx() as (s,_):
                        last=s.scalar(select(Job).where(Job.account_id==account_id,Job.kind=="renew",Job.credential_version==version).order_by(Job.created_at.desc()))
                        if last and (last.status in {"running","queued","uncertain"} or last.error_code in {"missing_material","iam_refresh_material_required","invalid_credentials","identity_mismatch","identity_verifier_unavailable"} or now-(last.finished_at or last.created_at)<600):
                            continue
                    self.store.plan(account_id,"renew")
                except (DomainError,ValueError,TypeError):
                    continue
        if config["refresh_balances"]:
            self.schedule_periodic_balances(config, now)
        if config["recovery_tests"]:
            with self.store.tx() as (s, state):
                for block in s.scalars(select(ModelState).where(ModelState.verified_at.is_(None), ModelState.review_at <= now)):
                    # Migrated account-wide restrictions may lack the original
                    # model. A wildcard is not a usable upstream probe target.
                    if block.model == "*" or not should_auto_review(block.reason):
                        continue
                    account = self.store.account(s, block.account_id)
                    if not account.sandbox and "verify" not in self.store.real_capabilities.get(account.platform, set()):
                        continue
                    if account.credential_status != "valid":
                        continue
                    pending = s.scalar(select(Job.id).where(Job.account_id == account.id, Job.kind == "verify", Job.status.in_(["queued", "running"])))
                    if pending:
                        continue
                    job = self.store.enqueue(s, account, "verify", f"recovery:{block.id}:{int(now // config['recovery_interval'])}")
                    s.add(Resource(id=uid(), kind="job_target", name=job.id, data=json.dumps({"model": block.model}), updated_at=now))
                    block.review_at = now + review_delay(config, block)
                    self.store.emit(s, state, "job", "scheduled", account.id, job.id)

    def _due_balance_accounts(self, session, config, now, account_ids=None):
        from .maintenance_readiness import DEPENDENCY_ERRORS, MaintenanceReadiness

        def in_scope(query, column):
            return query if account_ids is None else query.where(column.in_(account_ids))

        # Select candidates without holding the maintenance write lock. Reuse
        # this check for each short commit so a concurrent check-in, credential
        # change, balance query or proxy edit wins over the earlier snapshot.
        ranked = in_scope(select(Job.id, func.row_number().over(
            partition_by=Job.account_id, order_by=(Job.created_at.desc(), Job.id.desc())
        ).label("rank")).where(Job.kind == "balance"), Job.account_id).subquery()
        latest_balance = {job.account_id: job for job in session.scalars(
            select(Job).join(ranked, ranked.c.id == Job.id).where(ranked.c.rank == 1))}
        readiness = None
        today = day_at(now)
        accounts = in_scope(select(Account), Account.id).order_by(Account.id)
        for account in session.scalars(accounts):
            capabilities = {"balance", "checkin"} if account.sandbox else self.store.real_capabilities.get(account.platform, set())
            # Pending/suspect validation is scheduled by reconcile_maintenance.
            if "balance" not in capabilities or account.credential_version == 0 or account.credential_status in {"invalid", "suspect", "pending", "disabled"}:
                continue
            confirmed_zero = account.balance is not None and account.balance <= 0 and account.balance_version == account.credential_version
            if "checkin" in capabilities and config["daily_checkin"] and account.daily_checkin_enabled and account.checkin_day != today and confirmed_zero:
                continue
            interval = account.balance_interval_seconds or config["balance_interval"]
            if account.balance_at is not None and now - account.balance_at < interval:
                continue
            last = latest_balance.get(account.id)
            if last and (last.status in {"queued", "running"} or now - last.created_at < interval):
                continue
            if last and last.credential_version == account.credential_version and last.status == "failed" and last.error_code in DEPENDENCY_ERRORS:
                if readiness is None:
                    readiness = MaintenanceReadiness(self.store, session)
                if not readiness.ready(account):
                    continue
            yield account, f"periodic:{int(now // interval)}"

    def schedule_periodic_balances(self, config, now):
        with self.store.read() as (session, _):
            candidates = [account.id for account, _ in self._due_balance_accounts(session, config, now)]
        for start in range(0, len(candidates), 64):
            with self.store.tx() as (session, state):
                due = list(self._due_balance_accounts(session, config, now, candidates[start:start + 64]))
                if not due:
                    continue
                keys = [f"{account.id}:balance:{suffix}" for account, suffix in due]
                exists = set(session.scalars(select(Job.dedupe_key).where(Job.dedupe_key.in_(keys))))
                for (account, suffix), key in zip(due, keys):
                    if key not in exists:
                        job = self.store.new_job(session, account, "balance", suffix)
                        self.store.emit(session, state, "job", "scheduled", account.id, job.id)

    def sample(self):
        now = self.store.clock()
        disk = psutil.disk_usage(str(self.root))
        row = {"at": now, "pid": self.process.pid, "process_started_at": self.process.create_time(), "process_cpu_percent": self.process.cpu_percent(None), "process_memory_bytes": self.process.memory_info().rss, "disk_free_bytes": disk.free, "disk_total_bytes": disk.total, "maintenance_active": len(self.worker.tasks), "maintenance_capacity": self.worker.capacity}
        with self.store.tx() as (s, state):
            row["maintenance_active"]=s.scalar(select(func.count()).select_from(Job).where(Job.status=="running",Job.lease_until>now))
            row["registration_active"]=len(self.store.execution_leases(s))
            row["total_active"]=row["maintenance_active"]+row["registration_active"]
            row["maintenance_capacity"]=self.store.execution_capacity(s,self.worker.capacity)
            s.add(Resource(id=uid(), kind="resource_sample", name="local-process", data=json.dumps(row), updated_at=now))
            # Retain one week of samples, never delete request/job history here.
            s.execute(delete(Resource).where(Resource.kind == "resource_sample", Resource.updated_at < now - 7 * 86400))
            uncertain = s.scalar(select(func.count()).select_from(Job).where(Job.status == "uncertain"))
            oldest = s.scalar(select(func.min(Job.created_at)).where(Job.status == "queued"))
            conditions = {"disk_low": disk.free < max(1024 ** 3, disk.total * .05), "uncertain_jobs": uncertain > 0, "queue_waiting": oldest is not None and now - oldest > 1800}
            for code, active in conditions.items():
                alert = s.scalar(select(Resource).where(Resource.kind == "alert", Resource.name == code))
                old = json.loads(alert.data) if alert else {}
                if bool(old.get("active")) == active:
                    continue
                if alert is None:
                    alert = Resource(id=uid(), kind="alert", name=code, data="{}", updated_at=now)
                    s.add(alert)
                alert.data = json.dumps({"active": active, "first_seen": old.get("first_seen", now), "changed_at": now, "count": int(uncertain) if code == "uncertain_jobs" else None})
                alert.updated_at = now
                self.store.emit(s, state, "alert", code + ("_active" if active else "_resolved"))
            self.store.emit(s, state, "monitor", "sample_recorded")
        return row

    async def run(self):
        self.running = True
        while True:
            try:
                config = (await asyncio.to_thread(settings, self.store))["values"]
                self.worker.capacity = config["maintenance_capacity"]
                self.gateway.startup, self.gateway.idle, self.gateway.max_attempts = config["startup_seconds"], config["idle_seconds"], config["max_attempts"]
                self.gateway.placeholder_enabled = config['placeholder_enabled']
                await asyncio.to_thread(self.schedule, config)
                await asyncio.to_thread(self.sample)
                if self.store.clock() - getattr(self, "pruned_at", 0) > 3600:
                    await asyncio.to_thread(self.store.prune)
                    self.pruned_at = self.store.clock()
                self.last_tick, self.last_error = self.store.clock(), None
            except asyncio.CancelledError:
                self.running = False
                raise
            except Exception:
                self.last_error = "supervisor_tick_failed"
            await asyncio.sleep(15)


REQUEST_FIELDS = ("id", "platform", "model", "account_id", "started_at", "ended_at", "placeholder_at", "first_content_at", "status", "error_code", "input_tokens", "cached_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "key_id")


def percentiles(values):
    data = sorted(values)
    if not data:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {"count": len(data), "p50": data[math.ceil(.5 * len(data)) - 1], "p95": data[math.ceil(.95 * len(data)) - 1], "max": data[-1]}


def analyze(store, start, end, basis, platform=None):
    if end <= start or basis not in {"start", "end"}:
        raise DomainError("invalid_time_window", 422)
    with store.read() as (s, _):
        stamp = RequestLog.started_at if basis == "start" else RequestLog.ended_at
        query = select(RequestLog).where(stamp >= start, stamp < end)
        if platform:
            query = query.where(RequestLog.platform == platform)
        groups = defaultdict(list)
        for row in s.scalars(query):
            groups[(row.platform, row.model)].append(row)
        output = []
        for (p, model), rows in groups.items():
            statuses = dict(Counter(r.status for r in rows))
            reported = [r for r in rows if r.input_tokens is not None and r.input_tokens > 0 and r.cached_tokens is not None and 0 <= r.cached_tokens <= r.input_tokens]
            inputs, cache = sum(r.input_tokens for r in reported), sum(r.cached_tokens for r in reported)
            output.append({"platform": p, "model": model, "total": len(rows), "statuses": statuses, "errors": dict(Counter(r.error_code or r.status for r in rows if r.status not in {"succeeded", "inflight"})), "duration": percentiles([r.ended_at - r.started_at for r in rows if r.ended_at]), "effective_output": percentiles([r.first_content_at - r.started_at for r in rows if r.first_content_at]), "placeholder": percentiles([r.placeholder_at - r.started_at for r in rows if r.placeholder_at]), "cache": {"reported_requests": len(reported), "missing_requests": len(rows) - len(reported), "input_tokens": inputs, "cached_tokens": cache, "ratio": cache / inputs if inputs else None}})
        inflight = s.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.status == "inflight", RequestLog.started_at < end))
    return {"start": start, "end": end, "basis": basis, "as_of": store.clock(), "groups": output, "inflight_before_end": inflight, "end_window_excludes_inflight": basis == "end"}


def mount_operations(app, store, supervisor):
    @app.get("/api/jobs")
    def jobs(day: str | None=None, kind: str | None=None, status: str | None=None, account_id: str | None=None, offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200), page:int|None=Query(None,ge=1),page_size:int=Query(25,ge=1,le=200),platform:str|None=None,q:str|None=None,start:float|None=None,end:float|None=None):
        if page is not None: offset,limit=(page-1)*page_size,page_size
        with store.read() as (s,_):
            criteria=[]
            if day:
                criteria.append(Job.business_day==day)
            if kind:
                criteria.append(Job.kind.in_(kind.split(",")))
            if status:
                criteria.append(Job.status==status)
            if account_id:
                criteria.append(Job.account_id==account_id)
            if platform: criteria.append(Job.account_id.in_(select(Account.id).where(Account.platform==platform)))
            if q: criteria.append(Job.account_id.in_(select(Account.id).where(Account.label.icontains(q,autoescape=True) | Account.external_id.icontains(q,autoescape=True))))
            if start is not None: criteria.append(Job.created_at>=start)
            if end is not None: criteria.append(Job.created_at<end)
            total=s.scalar(select(func.count()).select_from(Job).where(*criteria))
            counts={status:count for status,count in s.execute(select(Job.status,func.count()).where(*criteria).group_by(Job.status))}
            records=list(s.scalars(select(Job).where(*criteria).order_by(Job.created_at.desc(),Job.id).offset(offset).limit(limit)))
            candidates={r.name for r in s.scalars(select(Resource).where(Resource.kind=="renewal_candidate",Resource.name.in_([j.id for j in records]))) if r.secret}
            fields=("id","account_id","kind","business_day","credential_version","status","created_at","available_at","finished_at","attempt","lease_until","result_code","error_code")
            from .task_presentation import enrich_jobs
            from .account_directory import identity
            enrichment=enrich_jobs(s,records,store.clock())
            accounts={a.id:a for a in s.scalars(select(Account).where(Account.id.in_([j.account_id for j in records])))}
            rows=[]
            for j in records:
                safe=identity(accounts[j.account_id])
                rows.append({**{k:getattr(j,k) for k in fields},**enrichment[j.id],**safe,'label':safe['display_name'],'account_display_name':safe['display_name'],"has_renewal_candidate":j.id in candidates})
            return {"total":total,"counts":counts,"rows":rows,"items":rows,"page":page or offset//limit+1,"page_size":limit,"offset":offset,"limit":limit,"as_of":store.clock()}

    @app.get('/api/jobs/{job_id}')
    def job_detail(job_id:str):
        with store.read() as (s,_):
            row=s.get(Job,job_id)
            if row is None: raise DomainError('job_not_found',404)
            from .task_presentation import enrich_jobs
            from .account_directory import identity
            result={k:getattr(row,k) for k in ('id','account_id','kind','business_day','credential_version','status','created_at','available_at','finished_at','attempt','lease_until','result_code','error_code')}
            safe=identity(store.account(s,row.account_id))
            result.update(enrich_jobs(s,[row],store.clock())[row.id]);result.update(safe);result.update(label=safe['display_name'],account_display_name=safe['display_name'])
            result['attempts']=[{k:getattr(a,k) for k in ('number','started_at','finished_at','outcome')} for a in s.scalars(select(Attempt).where(Attempt.job_id==job_id).order_by(Attempt.number))]
            return result

    @app.post("/api/accounts/{account_id}/model-blocks/release")
    def release_model_blocks(account_id: str, model: str | None = None):
        """人工解除账户限制（唯一能绕过自动复核的出口）。

        自动复核要求「原报错模型真实取得正文」。上游持续拒绝时这个条件永远
        无法满足，账户会被永久锁死在这里。人工解除只把限制放回候选池，
        不伪造可用性：下一次真实请求若仍被拒，会立刻重新 block。
        """
        released = store.release_model_block(account_id, model)
        if not released:
            raise DomainError("no_active_block", 404)
        return {"released": released, "as_of": store.clock()}

    @app.get("/api/jobs/{job_id}/attempts")
    def job_attempts(job_id:str):
        with store.read() as (s,_):
            if not s.get(Job,job_id):
                raise DomainError("job_not_found",404)
            fields=("number","started_at","finished_at","outcome")
            return {"attempts":[{k:getattr(a,k) for k in fields} for a in s.scalars(select(Attempt).where(Attempt.job_id==job_id).order_by(Attempt.number))]}

    @app.get("/api/settings")
    def get_settings():
        return settings(store)

    @app.put("/api/settings")
    def set_settings(body: SettingsInput):
        write_settings(store, body)
        return {"ok": True}

    @app.get("/api/monitor")
    def monitor():
        rows = store.resources("resource_sample")[:240]
        if app.state.role == "api":
            # Background loops run in the task service; report what it last published.
            remote = store.service_status("tasks")
            sup, reg, inv = remote.get("supervisor") or {}, remote.get("registration") or {}, remote.get("invitation") or {}
            return {"samples": rows, "alerts": store.resources("alert"), "supervisor_running": bool(remote["online"] and sup.get("running")), "last_tick": sup.get("last_tick"), "last_error": sup.get("last_error"), "sample_interval_seconds": 15, "scope": "task_service_process", "task_service": remote, "registration": reg, "invitation": inv}
        runner=app.state.registration_runner
        return {"samples": rows, "alerts": store.resources("alert"), "supervisor_running": supervisor.running, "last_tick": supervisor.last_tick, "last_error": supervisor.last_error, "sample_interval_seconds": 15, "scope": "current_application_process", "registration":{"last_tick":runner.last_tick,"last_error":runner.last_error},"invitation":{"last_tick":app.state.invitations.last_tick,"last_error":app.state.invitations.last_error}}

    @app.get("/api/analytics")
    def analytics(start: float, end: float, basis: str = "start", platform: str | None = None, model: str | None = None, account_id: str | None = None,all_time:bool=False,top_limit:int=Query(20,ge=1,le=100),rank_by:str='total_tokens',key_id:str|None=None,client_source:str|None=None):
        from .reporting import analyze as sql_analyze
        return sql_analyze(store, start, end, basis, platform, model, account_id,all_time,top_limit,rank_by,key_id,client_source)

    @app.get('/api/analytics/sources')
    def analytics_sources(kind:str,start:float,end:float,basis:str='start',platform:str|None=None,model:str|None=None,account_id:str|None=None,key_id:str|None=None,client_source:str|None=None,page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=200),sort:str='total_tokens'):
        from .reporting import sources
        return sources(store,kind,start,end,basis,platform,model,account_id,key_id,client_source,page,page_size,sort)

    @app.get("/api/requests")
    def requests(start: float, end: float, basis: str = "start", platform: str | None = None, status: str | None = None, request_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), model:str|None=None,account_id:str|None=None,key_id:str|None=None,client_source:str|None=None):
        if end <= start or basis not in {"start", "end"}:
            raise DomainError("invalid_time_window", 422)
        with store.read() as (s, _):
            stamp = RequestLog.started_at if basis == "start" else RequestLog.ended_at
            criteria = [stamp >= start, stamp < end]
            if platform:
                criteria.append(RequestLog.platform == platform)
            if status:
                criteria.append(RequestLog.status == status)
            if request_id:
                criteria.append(RequestLog.id.startswith(request_id, autoescape=True))
            if model: criteria.append(RequestLog.model==model)
            if account_id: criteria.append(RequestLog.account_id==account_id)
            if key_id: criteria.append(RequestLog.key_id.is_(None) if key_id=="unknown" else RequestLog.key_id==key_id)
            if client_source: criteria.append(RequestLog.client_source.is_(None) if client_source=="unknown" else RequestLog.client_source==client_source)
            total = s.scalar(select(func.count()).select_from(RequestLog).where(*criteria))
            counts = {status: count for status, count in s.execute(select(RequestLog.status, func.count()).where(*criteria).group_by(RequestLog.status))}
            rows = [{k: getattr(r, k) for k in REQUEST_FIELDS} for r in s.scalars(select(RequestLog).where(*criteria).order_by(stamp.desc(), RequestLog.id).offset(offset).limit(limit))]
            return {"total": total, "counts": counts, "rows": rows, "offset": offset, "limit": limit, "as_of": store.clock()}

    @app.get("/api/requests/{request_id}")
    def request_detail(request_id: str):
        with store.read() as (s, _):
            r = s.get(RequestLog, request_id)
            if not r:
                raise DomainError("request_not_found", 404)
            records=list(s.scalars(select(RequestAttempt).where(RequestAttempt.request_id == request_id).order_by(RequestAttempt.started_at)))
            accounts={a.id:a for a in s.scalars(select(Account).where(Account.id.in_([r.account_id for r in records])))}
            from .account_directory import identity
            attempts=[]
            for a in records:
                row={k:getattr(a,k) for k in ('account_id','credential_version','started_at','upstream_response_at','first_frame_at','ended_at','code')}
                safe=identity(accounts[a.account_id]) if a.account_id in accounts else {}
                row.update(account_display_name=safe.get('display_name'),identity_hint=safe.get('identity_hint'),selection_data=json.loads(a.selection_data) if a.selection_data else None)
                attempts.append(row)
            return {"request": {k: getattr(r, k) for k in REQUEST_FIELDS}, "attempts": attempts}
