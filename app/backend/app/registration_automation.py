"""Durable, opt-in creation of registration batches; this module sends no requests."""
import json
from typing import Literal

from pydantic import Field
from sqlalchemy import func, select, String, cast, or_
from .registration_queries import registrations, field, TERMINAL

from .models import Account, Resource
from .registration_runner import BatchInput
from .store import DomainError, uid

PLATFORMS=("Loomy","WorkBuddy 国内","WPS 灵犀","小浣熊","CodeArts")
KIND="registration_automation"


class AutomationInput(BatchInput):
    platform:Literal["Loomy","WorkBuddy 国内","WPS 灵犀","小浣熊","CodeArts"]
    enabled:bool=False
    mode:Literal["interval","inventory"]="interval"
    interval_minutes:int=Field(default=60,ge=1,le=44640)
    inventory_target:int=Field(default=10,ge=1,le=1000000)
    expected_version:int=Field(default=0,ge=0)


class RegistrationAutomation:
    def __init__(self,runner):
        self.runner,self.store=runner,runner.store

    def config_row(self,s,platform):
        return s.scalar(select(Resource).where(Resource.kind==KIND,Resource.name==platform))

    @staticmethod
    def public(row):
        return {"id":row.id,"version":row.version,"platform":row.name,**json.loads(row.data)}

    def validate_inputs(self,s,config):
        channel=s.get(Resource,config["sms_channel_id"])
        if not channel or channel.kind!="sms_channel" or not channel.secret:
            raise DomainError("sms_channel_not_found",404)
        if json.loads(channel.data).get("enabled") is False:
            raise DomainError("sms_channel_disabled")
        if config.get("proxy_id"):
            proxy=s.get(Resource,config["proxy_id"])
            if not proxy or proxy.kind!="proxy" or not proxy.secret or json.loads(proxy.data).get("status")=="retired":
                raise DomainError("missing_proxy",422)

    def evaluate(self,s,config,runtime=None):
        """Database-only preview, using exactly the same rules as the scheduler."""
        runtime=runtime or {}
        platform=config["platform"]
        inventory=s.scalar(select(func.count()).select_from(Account).where(
            Account.platform==platform,Account.sandbox.is_(False),Account.enabled.is_(True),
            Account.credential_version>0,Account.credential_status.in_(["valid","pending"]))) or 0
        base=registrations(s).where(field(s,"platform")==platform,field(s,"account_id").is_(None))
        unresolved_flag=cast(field(s,"result_unresolved"),String).in_(["1","true"])
        pending=s.scalar(select(func.count()).select_from(base.where(or_(unresolved_flag,field(s,"status").not_in(TERMINAL))).subquery())) or 0
        unresolved=s.scalar(select(func.count()).select_from(base.where(or_(unresolved_flag,field(s,"status")=="uncertain")).subquery())) or 0
        blocker=None
        existing=s.scalar(select(Resource).where(Resource.kind=="registration_batch",field(s,"platform")==platform,
            field(s,"status").in_(["active","paused"])).order_by(Resource.updated_at.desc()).limit(1))
        if existing:
            blocker="existing_batch_active" if json.loads(existing.data)["status"]=="active" else "existing_batch_paused"
        last=s.get(Resource,runtime["last_batch_id"]) if runtime.get("last_batch_id") else None
        # A cancellation is intentional. Do not silently replace the cancelled
        # automatic batch; a user must disable then explicitly re-enable this policy.
        if last and json.loads(last.data).get("status")=="cancelled":
            blocker="automatic_batch_cancelled"
        target=config["count"] if config["mode"]=="interval" else min(config["count"],max(0,config["inventory_target"]-inventory-pending))
        reason="ready"
        if not config["enabled"]:
            reason="disabled"
        elif not self.runner.service.registry.enabled:
            reason="upstream_disabled"
        elif blocker:
            reason=blocker
        elif unresolved:
            reason="registration_result_unresolved"
        elif pending:
            reason="registration_in_progress"
        elif target==0:
            reason="inventory_sufficient"
        else:
            try:
                self.validate_inputs(s,config)
            except DomainError as error:
                reason=error.code
        due=runtime.get("next_run_at")
        return {"reason":reason,"would_create":reason=="ready","count":target if reason=="ready" else 0,
                "inventory":inventory,"pending":pending,"unresolved":unresolved,
                "inventory_basis":"enabled_non_sandbox_valid_or_pending_credential_without_balance_or_model_filter",
                "next_run_at":due,"due":bool(due is not None and due<=self.store.clock())}

    def get(self,platform=None):
        with self.store.read() as (s,_):
            rows=s.scalars(select(Resource).where(Resource.kind==KIND))
            result=[]
            for row in rows:
                if platform and row.name!=platform:
                    continue
                item=self.public(row)
                item["preview"]=self.evaluate(s,item["config"],item["runtime"])
                result.append(item)
            return {"policies":result,"platforms":list(PLATFORMS),"default_enabled":False}

    def preview(self,body):
        config=body.model_dump(exclude={"expected_version"})
        with self.store.read() as (s,_):
            row=self.config_row(s,body.platform)
            runtime=json.loads(row.data).get("runtime",{}) if row else {}
            return self.evaluate(s,config,runtime)

    def save(self,body):
        config=body.model_dump(exclude={"expected_version"})
        with self.store.tx() as (s,state):
            row=self.config_row(s,body.platform)
            if (row.version if row else 0)!=body.expected_version:
                raise DomainError("version_conflict")
            if body.enabled:
                self.validate_inputs(s,config)
            previous=json.loads(row.data) if row else {}
            runtime=previous.get("runtime",{})
            old=previous.get("config",{})
            if body.enabled and (not old.get("enabled") or old.get("interval_minutes")!=body.interval_minutes or old.get("mode")!=body.mode):
                runtime={**runtime,"next_run_at":self.store.clock()+body.interval_minutes*60}
                # Re-enabling is explicit authorization for future batches.
                if not old.get("enabled"):
                    runtime.pop("last_batch_id",None)
                    runtime.pop("last_error",None)
            if not body.enabled:
                runtime["next_run_at"]=None
            if row is None:
                row=Resource(id=uid(),kind=KIND,name=body.platform,data="{}",version=0,updated_at=self.store.clock())
                s.add(row)
            row.data=json.dumps({"config":config,"runtime":runtime})
            row.version,row.updated_at=row.version+1,self.store.clock()
            self.store.emit(s,state,KIND,"configured")
            return self.public(row)

    def tick(self):
        if not self.runner.service.registry.enabled:
            return 0
        with self.store.read() as (s,_):
            ids=list(s.scalars(select(Resource.id).where(Resource.kind==KIND)))
        created=0
        for policy_id in ids:
            # Store.tx locks the shared State row (PG) / BEGIN IMMEDIATE (SQLite).
            # Creating sessions, advancing the due time and recording batch_id
            # commit together, including after worker/process restart.
            with self.store.tx() as (s,state):
                row=s.get(Resource,policy_id)
                if row is None:
                    continue
                data=json.loads(row.data)
                config,runtime=data["config"],data["runtime"]
                if not config["enabled"] or runtime.get("next_run_at") is None or runtime["next_run_at"]>self.store.clock():
                    continue
                decision=self.evaluate(s,config,runtime)
                runtime.update(last_checked_at=self.store.clock(),last_reason=decision["reason"],next_run_at=self.store.clock()+config["interval_minutes"]*60)
                if decision["would_create"]:
                    fields={key:config[key] for key in BatchInput.model_fields}
                    fields["count"]=decision["count"]
                    # Nested savepoint means invalid proxy/adapter input cannot
                    # leave a partial batch, or starve independent policies.
                    try:
                        with s.begin_nested():
                            batch=self.runner.create_batch(s,state,BatchInput(**fields),automation_id=row.id)
                        runtime.update(last_batch_id=batch,last_created_at=self.store.clock(),last_error=None)
                        created+=1
                    except DomainError as error:
                        runtime.update(last_error=error.code,last_reason=error.code)
                row.data,row.updated_at=json.dumps(data),self.store.clock()
                # Configuration version only changes on a user's save; routine
                # scheduler ticks must not make the edit form permanently stale.
                self.store.emit(s,state,KIND,runtime["last_reason"])
        return created


def mount_registration_automation(app,runner):
    service=RegistrationAutomation(runner)

    @app.get("/api/registration-automation")
    def get(platform:str|None=None):
        return service.get(platform)

    @app.post("/api/registration-automation/preview")
    def preview(body:AutomationInput):
        return service.preview(body)

    @app.put("/api/registration-automation")
    def save(body:AutomationInput):
        return service.save(body)
