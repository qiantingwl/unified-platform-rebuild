"""Recovery planning and private material independent from active credentials."""
import json
import hashlib
import re
from pydantic import BaseModel,ConfigDict,Field,SecretStr
from sqlalchemy import select, func, or_
from fastapi import Query
from .registration_queries import registrations
from .login_material import requirements

from .models import Account,Credential,Job,Resource
from .store import DomainError,day_at,uid


class RecoveryProfile(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_version:int=Field(ge=0)
    phone:SecretStr | None=Field(default=None,max_length=30)
    password:SecretStr | None=Field(default=None,max_length=1024)
    sms_channel_id:str | None=None
    clear_phone:bool=False
    clear_password:bool=False


class Recovery:
    def __init__(self,service):
        self.service,self.store=service,service.store

    def material(self,account_id):
        with self.store.read() as (s,_):
            a=self.store.account(s,account_id)
            row=s.scalar(select(Credential).where(Credential.account_id==a.id,Credential.version==a.credential_version))
            try:
                credential=json.loads(self.store.cipher.decrypt(row.ciphertext.encode())) if row and not a.sandbox else {}
            except (ValueError,TypeError):
                credential={}
            if not isinstance(credential,dict):
                credential={}
            profile=s.scalar(select(Resource).where(Resource.kind=="recovery_profile",Resource.name==a.id))
            private=json.loads(self.store.cipher.decrypt(profile.secret.encode())) if profile and profile.secret else {}
            account=credential.get("account") if isinstance(credential.get("account"),dict) else {}
            material={"phone":credential.get("phone") or credential.get("loginid") or account.get("phoneNumber"),"password":credential.get("password"),"sms_channel_id":credential.get("sms_channel_id"),**private}
            s.expunge(a)  # read() rolls back; retain this immutable planning snapshot.
            return a,credential,material,profile.version if profile else 0

    def plan(self,account_id):
        a,c,m,version=self.material(account_id)
        result={"account_id":a.id,"platform":a.platform,"label":a.label,"profile_version":version,"has_phone":bool(m.get("phone")),"has_password":bool(m.get("password")),"sms_channel_id":m.get("sms_channel_id"),"relogin_day":a.relogin_day,"failures":a.relogin_failures,"credential_version":a.credential_version}
        from .account_directory import identity
        display=identity(a)
        result.update(display, label=display["display_name"])
        result.update(requirements(a.platform,c,m))
        if a.sandbox:
            return {**result,"action":"sandbox","reason":"sandbox"}
        with self.store.read() as (s,_):
            from .native_lifecycle import control
            lifecycle=control(s,a)
            result.update(lifecycle=lifecycle, automatic_login_allowed=lifecycle["state"]=="independent",
                          independent_session_verified=lifecycle["state"]=="independent")
            identity_row, identity = self.service.identity_book(s, a, m, create=False)
            identity_failures = max(a.relogin_failures, identity.get("no_sms_failures", 0))
            result["failures"] = identity_failures
            result["relogin_day"] = max(a.relogin_day or "", identity.get("day") or "") or None
            latest = s.scalar(registrations(s, a.id).order_by(Resource.updated_at.desc(), Resource.id.desc()).limit(1))
            latest_data = json.loads(latest.data) if latest else {}
            result.update(last_session_id=latest.id if latest else None,
                          last_login_status=latest_data.get("status"), last_login_error=latest_data.get("error"),
                          last_login_at=latest_data.get("finished_at"), balance_verified_at=a.balance_at)
            maintenance=select(Job).where(Job.account_id==a.id,Job.credential_version==a.credential_version,Job.kind.in_(["checkin","balance"])).order_by(Job.created_at.desc())
            if a.credential_status == "valid":
                stage = "verified"
            elif latest_data.get("status") == "succeeded":
                active = s.scalar(maintenance.where(Job.status.in_(["queued","running"])).limit(1))
                failed = s.scalar(maintenance.where(Job.status.in_(["failed","uncertain"])).limit(1))
                stage = f"{active.kind}_{active.status}" if active else "verification_failed" if failed else "credential_pending"
            else:
                stage = latest_data.get("status") or "not_started"
            result["recovery_stage"] = stage
            pending=s.scalar(registrations(s, a.id, pending=True).order_by(Resource.updated_at.desc()).limit(1))
            renew=s.scalar(select(Job).where(Job.account_id==a.id,Job.kind=="renew",Job.credential_version==a.credential_version).order_by(Job.created_at.desc()))
            if pending:
                return {**result,"action":"pending","reason":json.loads(pending.data)["status"],"session_id":pending.id}
            candidate=s.scalar(select(Resource).where(Resource.kind=="renewal_candidate",Resource.name==renew.id)) if renew else None
            if renew and renew.status in {"failed","uncertain"} and candidate and candidate.secret and renew.error_code not in {"invalid_credentials","identity_mismatch","identity_verifier_unavailable","invalid_credential_format"}:
                if self.store.clock()-(renew.finished_at or renew.created_at)<600:
                    return {**result,"action":"renew_wait","reason":"renew_backoff","job_id":renew.id}
                return {**result,"action":"renew_verify","reason":"issued_credential_pending","job_id":renew.id}
            if renew and renew.status in {"queued","running","uncertain"}:
                return {**result,"action":"renew_pending","reason":renew.status,"job_id":renew.id}
            intent_name=hashlib.sha256(f"{a.id}:renewal_intent:{a.credential_version}".encode()).hexdigest()
            intent_row=s.scalar(select(Resource).where(Resource.kind=="operation_ledger",Resource.name==intent_name))
            intent=json.loads(intent_row.data) if intent_row else {}
            renewal_ambiguous=bool(intent and not intent.get("safe_to_retry") and not candidate)
            if renewal_ambiguous:
                result.update(renewal_result_uncertain=True,material_guidance="official_reauthorization_required_after_ambiguous_refresh")
            failed_refresh=renewal_ambiguous or renew and renew.error_code in {"missing_material","iam_refresh_material_required","invalid_credentials","identity_mismatch","identity_verifier_unavailable"}
            retry_later=renew and renew.status=="failed" and self.store.clock()-(renew.finished_at or renew.created_at)<600
        if a.credential_status=="valid":
            return {**result,"action":"healthy","reason":"credential_valid"}
        if a.credential_status=="disabled":
            # 上游说账号已停用：重登/续期都不会好转，别把短信预算烧在这里。
            return {**result,"action":"stopped","reason":"upstream_disabled"}
        if identity_failures>=3:
            return {**result,"action":"stopped","reason":"relogin_attempts_exhausted"}
        if result["relogin_day"]==day_at(self.store.clock()):
            return {**result,"action":"tomorrow","reason":"relogin_already_scheduled_today"}
        refresh=result["can_refresh"]
        if refresh and not failed_refresh and "renew" in self.store.real_capabilities.get(a.platform,set()):
            return {**result,"action":"renew_wait" if retry_later else "renew","reason":"renew_backoff" if retry_later else "refresh_material_present"}
        if a.platform=="Loomy" and m.get("phone") and m.get("password"):
            mode="password"
        elif a.platform in {"Loomy","WorkBuddy 国内"} and m.get("phone") and m.get("sms_channel_id"):
            mode="sms"
        elif a.platform=="Loomy":
            return {**result,"action":"material","reason":"missing_relogin_material"}
        else:
            mode={"WPS 灵犀":"browser","小浣熊":"official","CodeArts":"official","WorkBuddy 国内":"official","WorkBuddy 国际":"official","Marvis":"oauth_wechat" if str(c.get("login_type"))=="2" else "oauth_qq"}.get(a.platform)
        if mode=="sms":
            try:
                self.service.sms.read(m["sms_channel_id"],for_new=True)
            except DomainError as error:
                return {**result,"action":"material","reason":error.code}
        return {**result,"action":"login" if mode in {"sms","password"} else "authorize","reason":"automatic_login" if mode in {"sms","password"} else "official_authorization_required","mode":mode}

    def prepare(self,account_id):
        plan=self.plan(account_id)
        action=plan["action"]
        if action in {"pending","renew_pending","renew_wait"}:
            return plan
        if action in {"renew","renew_verify"}:
            return {**plan,"job_id":self.store.plan(account_id,"renew")}
        if action not in {"login","authorize"}:
            raise DomainError(plan["reason"])
        a,_,material,_=self.material(account_id)
        material={**material,"auto_run":True,"recovery_source":"automatic"}
        # Official authorization can preserve the original phone without
        # buying a number or attaching a new SMS subscription.
        if plan["mode"] not in {"sms","password"} and a.platform not in {"WPS 灵犀","小浣熊"}:
            material["sms_channel_id"]=None
        sid=self.service.create(a.platform,a.label+" 恢复登录",plan["mode"],material,a.id)
        return {**plan,"session_id":sid}

    def save(self,account_id,body):
        with self.store.tx() as (s,state):
            a=self.store.account(s,account_id)
            if a.sandbox:
                raise DomainError("sandbox_account_not_login_target",422)
            row=s.scalar(select(Resource).where(Resource.kind=="recovery_profile",Resource.name==a.id))
            if (row.version if row else 0)!=body.expected_version:
                raise DomainError("resource_version_conflict")
            if body.sms_channel_id:
                channel=s.get(Resource,body.sms_channel_id)
                if not channel or channel.kind!="sms_channel":
                    raise DomainError("sms_channel_not_found",404)
            material=json.loads(self.store.cipher.decrypt(row.secret.encode())) if row and row.secret else {}
            for key,clear in (("phone",body.clear_phone),("password",body.clear_password)):
                value=getattr(body,key)
                if clear:
                    material[key]=None
                elif value:
                    raw=value.get_secret_value()
                    if key=="phone":
                        raw=raw.strip()
                        if raw and not re.fullmatch(r"\+?[0-9]{5,20}",raw):
                            raise DomainError("import_full_phone_required",422)
                    if raw:
                        material[key]=raw
            material["sms_channel_id"]=body.sms_channel_id
            if row is None:
                row=Resource(id=uid(),kind="recovery_profile",name=a.id,data="{}",updated_at=self.store.clock())
                s.add(row)
            else:
                row.version+=1
            row.secret=self.store.cipher.encrypt(json.dumps(material).encode()).decode()
            row.updated_at=self.store.clock()
            self.store.emit(s,state,"recovery","material_updated",a.id)


def mount_recovery(app,recovery):
    @app.get("/api/recovery/accounts")
    def plans(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
              platform: str | None = None, q: str | None = Query(None, max_length=100)):
        with recovery.store.read() as (s,_):
            query=select(Account.id).where(Account.sandbox.is_(False))
            if platform:
                query=query.where(Account.platform==platform)
            if q:
                query=query.where(or_(Account.label.contains(q, autoescape=True), Account.external_id.contains(q, autoescape=True), Account.id==q))
            total=s.scalar(select(func.count()).select_from(query.subquery()))
            ids=list(s.scalars(query.order_by(Account.id).offset((page-1)*page_size).limit(page_size)))
        return {"accounts":[recovery.plan(i) for i in ids],"total":total,"page":page,"page_size":page_size}

    @app.put("/api/recovery/accounts/{account_id}/material")
    def save(account_id:str,body:RecoveryProfile):
        recovery.save(account_id,body)
        return {"ok":True}

    @app.post("/api/recovery/accounts/{account_id}/prepare")
    def prepare(account_id:str):
        return recovery.prepare(account_id)
