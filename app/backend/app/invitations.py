"""Durable invitation submissions. Recovery only reads, never rebinds."""
import asyncio
import json
from sqlalchemy import select

from .models import Account, Job, Resource
from .store import DomainError, uid
from .upstream import UpstreamError
from .maintenance_proxy import resolve_maintenance_proxy


class Invitations:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry
        self.last_tick = self.last_error = None

    def queue(self, account_id, code=None, auto=False):
        if not self.registry.enabled:
            raise DomainError("network_execution_disabled")
        if code is not None:
            code = code.strip()
            if not code or len(code) > 128 or any(c.isspace() for c in code):
                raise DomainError("invalid_input", 422)
        with self.store.tx() as (s, state):
            a = self.store.account(s, account_id)
            if a.sandbox or a.platform != "Loomy" or not a.credential_version:
                raise DomainError("adapter_unavailable", 422)
            pending = s.scalar(select(Job).where(Job.account_id == a.id, Job.kind == "invitation", Job.status.in_(["queued", "running"]), Job.credential_version == a.credential_version))
            if pending:
                # Never silently replace an already queued code or operation.
                if code:
                    raise DomainError("invitation_busy")
                return {"job_id": pending.id, "status": pending.status}
            j = self.store.enqueue(s, a, "invitation", uid())
            private = self.store.cipher.encrypt(json.dumps({"code": code}).encode()).decode()
            s.add(Resource(id=uid(), kind="job_target", name=j.id, data=json.dumps({"auto": auto}), secret=private, updated_at=self.store.clock()))
            self.store.emit(s, state, "invitation", "queued", a.id, j.id)
            return {"job_id": j.id, "status": "queued"}

    def job_input(self, job):
        with self.store.read() as (s, _):
            j = s.get(Job, job["id"])
            a = self.store.account(s, job["account_id"])
            if not j or j.status != "running" or j.owner != job["owner"] or j.attempt != job["attempt"] or j.lease_until <= self.store.clock() or a.credential_version != job["version"]:
                raise DomainError("stale_job_receipt")
            row = s.scalar(select(Resource).where(Resource.kind == "job_target", Resource.name == j.id))
            return json.loads(self.store.cipher.decrypt(row.secret.encode())).get("code"), json.loads(row.data).get("auto", False)

    async def execute_job(self, job):
        code, auto = await asyncio.to_thread(self.job_input, job)
        return await self.execute(job["account_id"], code, auto=auto, job=job)

    def begin(self, account_id, code, job=None):
        with self.store.tx() as (s, state):
            a = self.store.account(s, account_id)
            if job:
                j = s.get(Job, job["id"])
                if j.status != "running" or j.owner != job["owner"] or j.attempt != job["attempt"] or j.lease_until <= self.store.clock() or a.credential_version != job["version"]:
                    raise DomainError("stale_job_receipt")
            if a.sandbox or a.platform != "Loomy":
                raise DomainError("adapter_unavailable", 422)
            row = s.scalar(select(Resource).where(Resource.kind == "invitation", Resource.name == account_id).order_by(Resource.updated_at.desc()))
            data = json.loads(row.data) if row else {}
            private = json.loads(self.store.cipher.decrypt(row.secret.encode())) if row and row.secret else {}
            if not isinstance(private, dict):
                private = {}
            if data.get("lease_until", 0) > self.store.clock():
                raise DomainError("invitation_busy")
            # Legacy bind records also require readback before another mutation.
            attempted = data.get("submission_started_at") or (data.get("operation") == "invite_bind" and data.get("submitted_at"))
            # A fresh, explicit form submission may retry a confirmed negative
            # read. Background recovery never comes through this branch.
            retry_confirmed = attempted and data.get("status") == "not_bound" and data.get("read_at", 0) >= attempted and self.store.clock()-data.get("read_at", 0) <= 300
            if code and ((attempted and not retry_confirmed) or data.get("binding_confirmed") or data.get("activated")):
                raise DomainError("invitation_requires_readback")
            if code and retry_confirmed:
                archive = {k: v for k, v in data.items() if k not in {"owner", "lease_until"}}
                s.add(Resource(id=uid(), kind="invitation_attempt", name=account_id, data=json.dumps(archive), secret=row.secret, updated_at=self.store.clock()))
                data = {"attempt_count": data.get("attempt_count", 1)+1}
                private = {}
            elif attempted:
                data["submission_started_at"] = attempted
            if code:
                private["requested_code"] = code
            if row is None:
                row = Resource(id=uid(), kind="invitation", name=account_id, data="{}", version=1, updated_at=self.store.clock())
                s.add(row)
            token = uid()
            data.update(account_id=account_id, operation="invite_bind" if code else "invite_read", status=data.get("status") if data.get("activated") else "running", owner=token, lease_until=self.store.clock()+75,
                        credential_version=a.credential_version, credit_confirmed=False, credit_status="evidence_unavailable")
            data["job"] = {k: job[k] for k in ("id", "owner", "attempt")} if job else None
            row.data = json.dumps(data)
            row.secret = self.store.cipher.encrypt(json.dumps(private).encode()).decode()
            row.version += 1
            row.updated_at = self.store.clock()
            self.store.emit(s, state, "invitation", "read_started", account_id)
            return row.id, token, a.platform, a.credential_version, private.get("requested_code"), bool(attempted or data.get("activated"))

    def update(self, rid, token, values, evidence=None, requested_code=None):
        with self.store.tx() as (s, state):
            row = s.get(Resource, rid)
            data = json.loads(row.data)
            if data.get("owner") != token or data.get("lease_until", 0) <= self.store.clock():
                raise DomainError("stale_invitation_receipt")
            a = self.store.account(s, data["account_id"])
            job = data.get("job")
            if a.credential_version != data["credential_version"]:
                raise DomainError("stale_invitation_receipt")
            if job:
                j = s.get(Job, job["id"])
                if not j or j.status != "running" or j.owner != job["owner"] or j.attempt != job["attempt"] or j.lease_until <= self.store.clock():
                    raise DomainError("stale_invitation_receipt")
            # Confirmed activation is a durable fact, even if a later read fails.
            values = dict(values)
            if data.get("activated"):
                for key in ("status", "activated", "binding_confirmed"):
                    values.pop(key, None)
            data.update(values)
            if values.get("status") == "uncertain":
                count = data.get("read_failures", 0) + 1
                data.update(read_failures=count, next_read_at=self.store.clock()+min(3600, 60*2**min(count, 6)))
            if data.get("activated") and not data.get("balance_followup_id"):
                followup = self.store.enqueue(s, a, "balance", f"after_invitation:{row.id}")
                followup.priority = 5
                data["balance_followup_id"] = followup.id
            if evidence is not None or requested_code is not None:
                private = json.loads(self.store.cipher.decrypt(row.secret.encode()))
                if evidence is not None:
                    private["last_readback"] = evidence
                if requested_code is not None:
                    private["requested_code"] = requested_code
                row.secret = self.store.cipher.encrypt(json.dumps(private).encode()).decode()
            row.data, row.updated_at = json.dumps(data), self.store.clock()
            row.version += 1
            self.store.emit(s, state, "invitation", values.get("status", "submission_recorded"), data["account_id"])
            return {k: v for k, v in data.items() if k not in {"owner", "lease_until", "job"}}

    def donors(self, account_id):
        with self.store.read() as (s, _):
            # Rotate eligible donors using a persisted cursor on the recipient.
            row = s.scalar(select(Resource).where(Resource.kind == "invitation", Resource.name == account_id))
            cursor = json.loads(row.data).get("donor_cursor", "") if row else ""
            rows = list(s.scalars(select(Account).where(Account.platform == "Loomy", Account.id != account_id, Account.enabled.is_(True), Account.sandbox.is_(False), Account.credential_status == "valid", Account.balance > 0, Account.balance_version == Account.credential_version).order_by(Account.id)))
            rows = [a for a in rows if a.id > cursor] + [a for a in rows if a.id <= cursor]
            return [(a.id, a.credential_version) for a in rows[:3]]

    async def donor_code(self, account_id, rid, token):
        for donor, version in await asyncio.to_thread(self.donors, account_id):
            await asyncio.to_thread(self.update, rid, token, {"donor_cursor": donor})
            adapter = None
            try:
                adapter = self.registry.make("Loomy", await asyncio.to_thread(self.store.credential, donor, version), donor, version, await asyncio.to_thread(resolve_maintenance_proxy, self.store, donor, "invitation"))
                if await adapter.balance() <= 0:
                    continue
                value = await adapter.invite_codes()
                for item in value.get("list", []):
                    code = item.get("inviteCode")
                    if isinstance(code, str) and 0 < len(code) <= 128 and not any(c.isspace() for c in code) and item.get("status") not in {"expired", "exhausted", "disabled"} and int(item.get("maxUses", 0)) > int(item.get("usedCount", 0)):
                        return code
            except (UpstreamError, DomainError, ValueError, TypeError):
                continue
            finally:
                if adapter:
                    await adapter.t.close()
        return None

    @staticmethod
    def binding(value, requested):
        if not isinstance(value, dict) or not isinstance(value.get("activated"), bool):
            raise UpstreamError("invalid_response")
        active = value["activated"]
        applied = value.get("appliedInvitationCode")
        matched = active and (not requested or applied == requested)
        return {"binding_confirmed": matched, "activated": active,
                "status": "bound" if matched else "bound_other_code" if active and applied else "binding_unconfirmed" if active else "not_bound"}

    async def execute(self, account_id, code=None, auto=False, job=None):
        if not self.registry.enabled:
            raise DomainError("network_execution_disabled")
        if code is not None:
            code = code.strip()
            if not code:
                raise DomainError("invalid_input", 422)
        rid, token, platform, version, requested, attempted = (await asyncio.to_thread(self.begin, account_id, code, job))
        adapter = None
        values = {}
        evidence = None
        try:
            adapter = self.registry.make(platform, (await asyncio.to_thread(self.store.credential, account_id, version)), account_id, version, (await asyncio.to_thread(resolve_maintenance_proxy, self.store, account_id, "invitation")))
            async with asyncio.timeout(60):
                # Preflight avoids submitting again for an already activated account.
                evidence = await adapter.invite_read()
                values = self.binding(evidence, requested)
                if auto and not attempted and not evidence["activated"]:
                    code = await self.donor_code(account_id, rid, token)
                    requested = code
                    if not code:
                        values["status"] = "waiting_donor"
                    else:
                        # Another actor may have bound while donor lookup ran.
                        evidence = await adapter.invite_read()
                        values = self.binding(evidence, requested)
                if code and not evidence["activated"]:
                    (await asyncio.to_thread(self.update, rid, token, {"submission_started_at": self.store.clock(), "submitted_at": self.store.clock(), "status": "submitting"}, evidence, code))
                    try:
                        await adapter.invite_submit(code)
                    except UpstreamError as error:
                        values["submit_error"] = error.code
                    # Even a rejected or disconnected POST may have taken effect.
                    evidence = await adapter.invite_read()
                    values.update(self.binding(evidence, requested))
                values.update(read_at=self.store.clock(), error=None, read_failures=0, next_read_at=None)
        except asyncio.CancelledError:
            (await asyncio.to_thread(self.update, rid, token, {"status": "uncertain", "error": "executor_lost", "lease_until": 0, "next_read_at": self.store.clock()+60}))
            raise
        except (UpstreamError, DomainError, TimeoutError) as error:
            values.update(status="uncertain", error=getattr(error, "code", "upstream_timeout"), next_read_at=self.store.clock()+60)
        except Exception:
            values.update(status="uncertain", error="internal_error", next_read_at=self.store.clock()+60)
        finally:
            if adapter:
                try:
                    await adapter.t.close()
                except Exception:
                    pass
        return (await asyncio.to_thread(self.update, rid, token, {**values, "lease_until": 0}, evidence))

    def records(self):
        records = self.store.resources("invitation")
        result, seen = [], set()
        for row in records:
            data = row["data"]
            if data["account_id"] in seen:
                continue
            seen.add(data["account_id"])
            if data.get("status") in {"running", "submitting"} and data.get("lease_until", 0) <= self.store.clock():
                data["status"] = "uncertain"
                data["error"] = "executor_lost"
            data.pop("owner", None)
            data.pop("lease_until", None)
            data.pop("job", None)
            result.append(row)
        return result

    def schedule(self):
        from .operations import settings
        cfg = settings(self.store)["values"]
        rows = {r["data"]["account_id"]: r["data"] for r in self.records()}
        with self.store.read() as (s, _):
            accounts = [a.id for a in s.scalars(select(Account).where(Account.platform == "Loomy", Account.enabled.is_(True), Account.sandbox.is_(False), Account.credential_version > 0, Account.credential_status.not_in(["invalid", "disabled"])))]
            lost = {j.account_id for j in s.scalars(select(Job).where(Job.kind == "invitation", Job.status == "uncertain"))}
            pending = {j.account_id for j in s.scalars(select(Job).where(Job.kind == "invitation", Job.status.in_(["queued", "running"])))}
        for account in accounts:
            if account in pending:
                continue
            data = rows.get(account, {})
            if data.get("activated"):
                continue
            uncertain = data.get("status") == "uncertain" or (account in lost and not data)
            due = (data.get("next_read_at") or 0) <= self.store.clock()
            automatic = cfg["auto_invitation"] and self.store.clock() >= (data.get("read_at") or 0) + cfg["invitation_interval"]
            if due and (uncertain or automatic):
                self.queue(account, auto=automatic and not uncertain)

    async def run(self):
        while True:
            self.last_tick = self.store.clock()
            try:
                if self.registry.enabled:
                    await asyncio.to_thread(self.schedule)
                self.last_error = None
            except Exception:
                self.last_error = "invitation_readback_unavailable"
            await asyncio.sleep(15)
