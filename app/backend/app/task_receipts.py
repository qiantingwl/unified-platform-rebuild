"""Read-only public task receipts; credentials and write tokens stay private."""
import hashlib
import json
from fastapi import Query
from sqlalchemy import JSON, Integer, cast, func, select, true
from sqlalchemy.dialects.postgresql import JSONB
from .models import Credential, Job, Resource
from .store import DomainError, uid


def ledger_row(session, account_id, key):
    name = hashlib.sha256(f"{account_id}:{key}".encode()).hexdigest()
    return select(Resource).where(Resource.kind == "operation_ledger", Resource.name == name)


def mount_task_receipts(app, store, registry=None):
    @app.post("/api/accounts/{account_id}/benefits/inspect",status_code=201)
    def inspect_benefits(account_id: str):
        if registry is None or not registry.enabled:
            raise DomainError("network_execution_disabled")
        with store.tx() as (s,state):
            account=store.account(s,account_id)
            if account.platform!='WorkBuddy 国内' or account.sandbox or 'benefits' not in store.real_capabilities.get(account.platform,set()):
                raise DomainError('capability_unavailable',422)
            row=s.scalar(select(Credential).where(Credential.account_id==account_id,Credential.version==account.credential_version))
            credential=json.loads(store.cipher.decrypt(row.ciphertext.encode())) if row else {}
            profile=credential.get('account')
            profile=profile if isinstance(profile,dict) else {}
            if not row or profile.get('enterpriseId') or profile.get('enterprise_id'):
                raise DomainError('capability_unavailable',422)
            active=s.scalar(select(Job).where(Job.account_id==account_id,Job.kind=='benefits',
                Job.credential_version==account.credential_version,Job.status.in_(['queued','running'])))
            if active:
                target=s.scalar(select(Resource).where(Resource.kind=='job_target',Resource.name==active.id))
                if target and json.loads(target.data).get('benefits_read_only'):
                    return {'id':active.id,'status':active.status,'read_only':True}
                raise DomainError('benefits_busy',409)
            job=store.enqueue(s,account,'benefits','inspect:'+uid())
            s.add(Resource(id=uid(),kind='job_target',name=job.id,data=json.dumps({'benefits_read_only':True}),updated_at=store.clock()))
            store.emit(s,state,'job','benefits_inspection_scheduled',account_id,job.id)
            return {'id':job.id,'status':job.status,'read_only':True}

    @app.get("/api/accounts/{account_id}/benefits/status")
    def benefit_status(account_id: str):
        with store.read() as (s,_):
            account=store.account(s,account_id)
            row=s.scalar(ledger_row(s,account_id,'benefits_status'))
            data=json.loads(row.data) if row else {}
            safe={key:data[key] for key in ('status','day','error_code','job_id','credential_version','attempt') if key in data}
            safe['tasks']=[{key:task[key] for key in ('key','status','eligible','cards','reason','error') if key in task}
                for task in data.get('tasks',[]) if isinstance(task,dict)]
            return {**safe,'status':data.get('status','not_started'),'read_only':True,'account_id':account_id,
                'credential_current':data.get('credential_version')==account.credential_version,'observed_at':row.updated_at if row else None}

    @app.get("/api/accounts/{account_id}/benefits")
    def benefits(account_id: str):
        with store.read() as (s, _):
            account = store.account(s, account_id)
            row = s.scalar(ledger_row(s, account_id, "benefits"))
            data = json.loads(row.data) if row else {}
            safe = {k: data[k] for k in ("settled", "claimed", "needs_balance", "day", "error_code", "job_id", "credential_version", "attempt") if k in data}
            safe["tasks"] = [{k: task[k] for k in ("key", "status", "changed", "error", "reason") if k in task}
                             for task in data.get("tasks", [])]
            return {**safe, "account_id": account_id, "status": "available" if row else "not_started",
                    "credential_current": data.get("credential_version") == account.credential_version,
                    "observed_at": row.updated_at if row else None}

    @app.get("/api/accounts/{account_id}/credit-packages")
    def credit_packages(account_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
        with store.read() as (s, _):
            account = store.account(s, account_id)
            query = ledger_row(s, account_id, "credit_packages")
            if s.get_bind().dialect.name == "sqlite":
                metadata = func.json_remove(Resource.data, "$.packages")
            else:
                metadata = cast(Resource.data, JSONB).op("-")("packages")
            row = s.execute(query.with_only_columns(Resource.id, metadata.label("metadata"))).first()
            if not row:
                return {"status": "unavailable", "account_id": account_id, "packages": [], "total": 0,
                        "page": page, "page_size": page_size, "credential_version": None, "credential_current": False,
                        "transactions_available": False}
            data = json.loads(row.metadata) if isinstance(row.metadata, str) else row.metadata
            if s.get_bind().dialect.name == "sqlite":
                elements = func.json_each(Resource.data, "$.packages").table_valued("key", "value")
                order = cast(elements.c.key, Integer)
                count = func.json_array_length(Resource.data, "$.packages")
            else:
                elements = func.json_array_elements(cast(Resource.data, JSON)["packages"]).table_valued("value", with_ordinality="ordinality")
                order = elements.c.ordinality
                count = func.json_array_length(cast(Resource.data, JSON)["packages"])
            total = s.scalar(select(count).where(Resource.id == row.id)) or 0
            values = s.scalars(select(elements.c.value).select_from(Resource).join(elements, true())
                .where(Resource.id == row.id).order_by(order).offset((page-1)*page_size).limit(page_size))
            packages = [json.loads(v) if isinstance(v, str) else v for v in values]
            current = data.get("credential_version") == account.credential_version
            return {**data, "status": data.get("status", "unavailable") if current else "stale",
                    "account_id": account_id, "packages": packages, "total": total,
                    "page": page, "page_size": page_size, "credential_current": current, "transactions_available": False}
