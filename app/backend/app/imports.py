"""Offline, staged migrations. No old scheduling state is trusted or restored."""
import asyncio
import hashlib
import json
import re
from collections import Counter
from typing import Literal

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, func, cast, JSON, true, Integer
from sqlalchemy.dialects.postgresql import JSONB
from .registration_queries import field

from .models import Account, Credential, Resource
from .store import DomainError, PLATFORMS, uid
from .upstream import UpstreamError

ALIASES = {"loomy": "Loomy", "raccoon": "小浣熊", "wps-lingxi": "WPS 灵犀", "wps_lingxi": "WPS 灵犀", "codearts": "CodeArts", "marvis": "Marvis"}
VERIFIABLE = {"小浣熊", "WPS 灵犀", "CodeArts", "WorkBuddy 国内", "WorkBuddy 国际"}


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: SecretStr = Field(min_length=2, max_length=8_000_000)
    platform: str | None = None
    source: str = Field(default="账户迁移", min_length=1, max_length=80)
    mode: Literal["accounts", "phone_mapping"] = "accounts"


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


def object_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise DomainError("import_plain_json_required", 422) from None
    if not isinstance(value, dict):
        raise DomainError("invalid_credential_format", 422)
    return value


def platform_of(value):
    if not isinstance(value, str):
        return None
    return value if value in PLATFORMS else ALIASES.get(value)


def normalize(item, selected):
    item = object_value(item)
    credential = dict(object_value(item.get("credential", item.get("item", item))))
    embedded = item.get("platform") or credential.get("platform") or credential.get("provider")
    if embedded is not None and not isinstance(embedded, str):
        raise DomainError("import_platform_required", 422)
    platform = platform_of(embedded) or platform_of(selected)
    if not platform or embedded and not platform_of(embedded) and embedded not in {"workbuddy", "codebuddy"}:
        raise DomainError("import_platform_required", 422)
    if selected and platform_of(selected) != platform:
        raise DomainError("import_platform_mismatch", 422)
    account = credential.get("account") or {}
    if not isinstance(account, dict) or not isinstance(credential.get("auth", {}), dict):
        raise DomainError("invalid_credential_format", 422)
    if platform == "Loomy":
        identity = credential.get("userid") or credential.get("uid")
        valid = credential.get("session") or credential.get("token")
    elif platform == "小浣熊":
        identity = credential.get("userId") or credential.get("uid")
        valid = credential.get("access_token") or credential.get("accessToken") or credential.get("token")
        credential["access_token"] = valid
        credential["refresh_token"] = credential.get("refresh_token") or credential.get("refreshToken")
    elif platform == "WPS 灵犀":
        identity = credential.get("userid") or credential.get("uid")
        valid = credential.get("cookie")
        if not valid and credential.get("sid"):
            valid = "wps_sid="+str(credential["sid"])
            credential["cookie"] = valid
    elif platform.startswith("WorkBuddy"):
        from .adapters import workbuddy_identity
        identity, valid = (workbuddy_identity(account) if account.get("uid") else None), (credential.get("auth") or {}).get("accessToken")
    elif platform == "CodeArts":
        evidence = credential.get("verified_identity") or {}
        identity = str(evidence["account_id"])+":"+str(evidence["principal_id"]) if isinstance(evidence, dict) and evidence.get("account_id") and evidence.get("principal_id") else None
        valid = (credential.get("access_key") or credential.get("access_key_id") or credential.get("accessKeyId")) and (credential.get("secret_key") or credential.get("secretAccessKey") or credential.get("secret_access_key")) and (credential.get("security_token") or credential.get("securityToken"))
        credential["access_key"] = credential.get("access_key") or credential.get("access_key_id") or credential.get("accessKeyId")
        credential["secret_key"] = credential.get("secret_key") or credential.get("secret_access_key") or credential.get("secretAccessKey")
        credential["security_token"] = credential.get("security_token") or credential.get("securityToken")
    else:
        identity, valid = credential.get("openid"), credential.get("access_token")
    explicit = item.get("external_id") or (item.get("uid") if "item" in item else None)
    if explicit and identity and str(explicit) != str(identity):
        raise DomainError("identity_mismatch", 422)
    identity = str(explicit or identity or "")
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", identity):
        raise DomainError("import_identity_required", 422)
    if not valid:
        raise DomainError("missing_material", 422)
    if not isinstance(valid, str):
        raise DomainError("invalid_credential_format", 422)
    if len(json.dumps(credential)) > 65536:
        raise DomainError("import_credential_too_large", 422)
    phone = item.get("phone") or credential.get("phone") or account.get("phoneNumber")
    if phone and not re.fullmatch(r"\+?[0-9]{5,20}", str(phone)):
        raise DomainError("import_full_phone_required", 422)
    if phone:
        credential["phone"] = str(phone)
    return {"platform": platform, "external_id": identity, "credential": credential}


class Imports:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry

    def preview(self, body):
        raw = body.content.get_secret_value().lstrip("\ufeff")
        try:
            parsed = json.loads(raw)
        except ValueError:
            try:
                parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
            except ValueError:
                raise DomainError("import_plain_json_required", 422) from None
        if isinstance(parsed, dict):
            parsed = parsed.get("accounts", parsed.get("items", [parsed]))
        if not isinstance(parsed, list) or not 1 <= len(parsed) <= 10000:
            raise DomainError("import_count_invalid", 422)
        # HMAC avoids publishing a raw hash of low-entropy input files.
        import hmac
        digest = hmac.new(self.store.cipher._signing_key, raw.encode(), hashlib.sha256).hexdigest()
        with self.store.tx() as (s, state):
            entries, private, seen = [], [], set()
            existing = {(a.platform, a.external_id): a for a in s.scalars(select(Account))}
            for index, item in enumerate(parsed, 1):
                try:
                    if body.mode == "phone_mapping":
                        item = object_value(item)
                        platform = platform_of(item.get("platform") or body.platform)
                        if body.platform and platform != platform_of(body.platform):
                            raise DomainError("import_platform_mismatch", 422)
                        external_id = str(item.get("external_id") or "")
                        phone = str(item.get("phone") or "")
                        if not re.fullmatch(r"\+?[0-9]{5,20}", phone):
                            raise DomainError("import_full_phone_required", 422)
                        account = existing.get((platform, external_id))
                        if not account or account.sandbox:
                            raise DomainError("account_not_found", 422)
                        profile = s.scalar(select(Resource).where(Resource.kind == "recovery_profile", Resource.name == account.id))
                        material = json.loads(self.store.cipher.decrypt(profile.secret.encode())) if profile and profile.secret else {}
                        old_phone = material.get("phone")
                        if "phone" not in material and account.credential_version:
                            current = s.scalar(select(Credential).where(Credential.account_id == account.id, Credential.version == account.credential_version))
                            try:
                                c = json.loads(self.store.cipher.decrypt(current.ciphertext.encode())) if current else {}
                                old_phone = c.get("phone") or (c.get("account") or {}).get("phoneNumber")
                            except (ValueError, TypeError, AttributeError):
                                pass
                        key = (platform, external_id)
                        reason = "duplicate_in_file" if key in seen else "same_phone" if old_phone == phone else "phone_conflict" if "phone" in material or old_phone else "new_mapping"
                        seen.add(key)
                        entries.append({"line": index, "platform": platform, "status": reason, "account_id": account.id, "has_phone": bool(old_phone)})
                        private.append({"account_id": account.id, "phone": phone, "profile_version": profile.version if profile else 0, "credential_version": account.credential_version} if reason == "new_mapping" else None)
                        continue
                    value = normalize(item, body.platform)
                    key = (value["platform"], value["external_id"])
                    reason = "duplicate_in_file" if key in seen else "existing_account" if key in existing else "new"
                    seen.add(key)
                    entries.append({"line": index, "platform": value["platform"], "status": reason, "has_phone": bool(value["credential"].get("phone")), "account_id": existing[key].id if key in existing else None})
                    private.append(value if reason == "new" else None)
                except DomainError as error:
                    entries.append({"line": index, "status": "invalid", "error": error.code})
                    private.append(None)
            rid = uid()
            data = {"status": "preview", "mode": body.mode, "source": body.source, "fingerprint": digest, "created_at": self.store.clock(), "counts": dict(Counter(e["status"] for e in entries)), "entries": entries}
            s.add(Resource(id=rid, kind="account_import", name=body.source, version=1, data=json.dumps(data), secret=self.store.cipher.encrypt(json.dumps(private).encode()).decode(), updated_at=self.store.clock()))
            self.store.emit(s, state, "account_import", "preview_saved")
            return {"id": rid, "version": 1, "data": data}

    def commit(self, rid, expected):
        with self.store.tx() as (s, state):
            row = s.get(Resource, rid)
            if not row or row.kind != "account_import":
                raise DomainError("resource_not_found", 404)
            data = json.loads(row.data)
            if data["status"] == "committed":
                return data
            if row.version != expected or data["status"] != "preview":
                raise DomainError("resource_version_conflict")
            values = json.loads(self.store.cipher.decrypt(row.secret.encode()))
            now = self.store.clock()
            for entry, value in zip(data["entries"], values):
                if value is None:
                    continue
                if data.get("mode") == "phone_mapping":
                    account = self.store.account(s, value["account_id"])
                    profile = s.scalar(select(Resource).where(Resource.kind == "recovery_profile", Resource.name == account.id))
                    if (profile.version if profile else 0) != value["profile_version"] or account.credential_version != value["credential_version"]:
                        entry.update(status="phone_conflict", error="resource_version_conflict")
                        continue
                    material = json.loads(self.store.cipher.decrypt(profile.secret.encode())) if profile and profile.secret else {}
                    material["phone"] = value["phone"]
                    self.store.record_phone(s, account.platform, value["phone"], "imported", account.id)
                    if profile is None:
                        profile = Resource(id=uid(), kind="recovery_profile", name=account.id, version=1, data="{}", updated_at=now)
                        s.add(profile)
                    else:
                        profile.version += 1
                    profile.secret = self.store.cipher.encrypt(json.dumps(material).encode()).decode()
                    profile.updated_at = now
                    entry["status"] = "mapped"
                    self.store.emit(s, state, "recovery", "phone_mapping_saved", account.id)
                    continue
                existing = s.scalar(select(Account).where(Account.platform == value["platform"], Account.external_id == value["external_id"]))
                if existing:
                    entry.update(status="existing_account", account_id=existing.id)
                    continue
                aid = uid()
                # Imported credentials remain candidates until identity readback.
                external=value["external_id"]
                hint=external[:2]+"…"+external[-3:] if len(external)>6 else "***"+external[-2:]
                account=Account(id=aid, platform=value["platform"], external_id=external, label=f"{value['platform']} · {hint}", enabled=True, sandbox=False, credential_version=0, credential_status="missing", created_at=now, updated_at=now)
                from .native_identity import capture_identity
                capture_identity(account,value["credential"])
                s.add(account)
                entry.update(status="imported", account_id=aid)
                candidate = {"account_id": aid, "batch_id": rid, "status": "pending" if value["platform"] in VERIFIABLE else "needs_login", "expected_version": 0, "created_at": now, "line": entry["line"], "platform": value["platform"]}
                s.add(Resource(id=uid(), kind="import_candidate", name=aid, data=json.dumps(candidate), secret=self.store.cipher.encrypt(json.dumps(value["credential"]).encode()).decode(), updated_at=now))
                profile = {k: value["credential"][k] for k in ("phone", "password") if value["credential"].get(k)}
                if profile:
                    s.add(Resource(id=uid(), kind="recovery_profile", name=aid, data="{}", secret=self.store.cipher.encrypt(json.dumps(profile).encode()).decode(), updated_at=now))
            data.update(status="committed", committed_at=now, counts=dict(Counter(e["status"] for e in data["entries"])))
            row.data, row.secret, row.updated_at = json.dumps(data), None, now
            row.version += 1
            self.store.emit(s, state, "account_import", "committed")
            return data

    async def verify(self, cid):
        if not self.registry.enabled:
            raise DomainError("network_execution_disabled")
        with self.store.tx() as (s, state):
            row = s.get(Resource, cid)
            if not row or row.kind != "import_candidate":
                raise DomainError("resource_not_found", 404)
            data = json.loads(row.data)
            a = self.store.account(s, data["account_id"])
            if a.credential_version != data["expected_version"] or data["status"] == "activated":
                raise DomainError("credential_version_conflict")
            if a.platform not in VERIFIABLE:
                raise DomainError("import_official_login_required")
            if data.get("lease_until", 0) > self.store.clock():
                raise DomainError("import_verification_busy")
            credential = json.loads(self.store.cipher.decrypt(row.secret.encode()))
            token = uid()
            data.update(status="verifying", owner=token, lease_until=self.store.clock()+75)
            row.data = json.dumps(data)
            row.version += 1
            self.store.emit(s, state, "account_import", "verification_started", a.id)
        transport = None
        verified, failure = None, None
        try:
            transport = self.registry.transport_factory(proxy=(await asyncio.to_thread(self.store.account_proxy, a.id)))
            async with asyncio.timeout(60):
                if a.platform.startswith("WorkBuddy"):
                    adapter = self.registry.make(a.platform, credential, a.id, 0, (await asyncio.to_thread(self.store.account_proxy, a.id)))
                    try:
                        await adapter.validate_renewal(credential)
                    finally:
                        await adapter.t.close()
                    from .adapters import workbuddy_identity
                    verified = {"identity": workbuddy_identity(credential["account"]), "credential": credential}
                else:
                    from .login_connectors import VerifiedImport
                    verified = await VerifiedImport(transport, a.platform).start({"credential": credential}, "verified_import")
                if verified["identity"] != a.external_id:
                    raise UpstreamError("identity_mismatch")
        except (DomainError, UpstreamError, TimeoutError) as error:
            failure = getattr(error, "code", "upstream_timeout")
        except Exception:
            failure = "internal_error"
        finally:
            if transport:
                await transport.close()
        with self.store.tx() as (s, state):
            row = s.get(Resource, cid)
            data = json.loads(row.data)
            if data.get("owner") != token or data.get("lease_until", 0) <= self.store.clock():
                raise DomainError("stale_job_receipt")
            a = self.store.account(s, data["account_id"])
            if a.credential_version != data["expected_version"]:
                failure = "credential_version_conflict"
            data.update(status="failed" if failure else "activated", error=failure, lease_until=0, verified_at=self.store.clock())
            if not failure:
                previous_version=a.credential_version
                a.credential_version += 1
                a.credential_status, a.updated_at = "pending", self.store.clock()
                s.add(Credential(id=uid(), account_id=a.id, version=a.credential_version, ciphertext=self.store.cipher.encrypt(json.dumps(verified["credential"]).encode()).decode(), created_at=self.store.clock()))
                from .native_identity import capture_identity
                from .native_lifecycle import record_native_login
                capture_identity(a,verified["credential"])
                record_native_login(self.store,s,a,"verified_import",verified["credential"],previous_version)
                self.store.after_login(s, state, a, f"import:{cid}")
                row.secret = None
            row.data, row.updated_at = json.dumps(data), self.store.clock()
            row.version += 1
            self.store.emit(s, state, "account_import", data["status"], a.id)
            return {"status": data["status"], "error": failure}


def mount_imports(app, store, registry):
    service = Imports(store, registry)

    @app.post("/api/imports/preview")
    def preview(body: PreviewInput):
        return service.preview(body)

    @app.post("/api/imports/{rid}/commit")
    def commit(rid: str, body: VersionInput):
        return service.commit(rid, body.expected_version)

    @app.post("/api/imports/{rid}/discard")
    def discard(rid: str, body: VersionInput):
        with store.tx() as (s, state):
            row = s.get(Resource, rid)
            if not row or row.kind != "account_import":
                raise DomainError("resource_not_found", 404)
            data = json.loads(row.data)
            if row.version != body.expected_version or data["status"] != "preview":
                raise DomainError("resource_version_conflict")
            data["status"] = "discarded"
            row.data, row.secret = json.dumps(data), None
            row.version += 1
            row.updated_at = store.clock()
            store.emit(s, state, "account_import", "discarded")
        return {"ok": True}

    @app.post("/api/imports/candidates/{cid}/verify")
    async def verify(cid: str):
        return await service.verify(cid)

    @app.get("/api/imports")
    def history(page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100)):
        with store.read() as (s,_):
            total=s.scalar(select(func.count()).select_from(Resource).where(Resource.kind=="account_import"))
            rows=s.execute(select(Resource.id,Resource.name,Resource.version,Resource.updated_at,without_entries(s).label("public_data"))
                .where(Resource.kind=="account_import").order_by(Resource.updated_at.desc(),Resource.id)
                .offset((page-1)*page_size).limit(page_size))
            return {"batches":[{"id":row.id,"name":row.name,"version":row.version,"updated_at":row.updated_at,
                "data":decode_json(row.public_data)} for row in rows],"total":total,"page":page,"page_size":page_size}

    @app.get("/api/imports/{rid}")
    def detail(rid: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
        with store.read() as (s,_):
            row=s.execute(select(Resource.id,Resource.version,without_entries(s).label("public_data"))
                .where(Resource.id==rid,Resource.kind=="account_import")).first()
            if not row:
                raise DomainError("resource_not_found",404)
            data=decode_json(row.public_data)
            if s.get_bind().dialect.name=="sqlite":
                elements=func.json_each(Resource.data,"$.entries").table_valued("key","value")
                total=s.scalar(select(func.json_array_length(Resource.data,"$.entries")).where(Resource.id==rid)) or 0
                order=cast(elements.c.key,Integer)
            else:
                elements=func.json_array_elements(cast(Resource.data,JSON)["entries"]).table_valued("value",with_ordinality="ordinality")
                total=s.scalar(select(func.json_array_length(cast(Resource.data,JSON)["entries"])).where(Resource.id==rid)) or 0
                order=elements.c.ordinality
            entries=[decode_json(value) for value in s.scalars(select(elements.c.value).select_from(Resource).join(elements,true())
                .where(Resource.id==rid).order_by(order).offset((page-1)*page_size).limit(page_size))]
            data["entries"]=entries
            account_ids=[e["account_id"] for e in entries if e.get("account_id")]
            candidates=[]
            accounts={a.id:a for a in s.scalars(select(Account).where(Account.id.in_(account_ids)))}
            for candidate in s.scalars(select(Resource).where(Resource.kind=="import_candidate",field(s,"batch_id")==rid,
                field(s,"account_id").in_(account_ids)).order_by(Resource.updated_at,Resource.id).limit(page_size)):
                item=json.loads(candidate.data)
                account=accounts.get(item["account_id"])
                if not account:
                    continue
                if item["status"]!="activated" and account.credential_version!=item["expected_version"]:
                    item["status"]="superseded"
                elif item["status"]=="verifying" and item.get("lease_until",0)<=store.clock():
                    item["status"]="uncertain"
                item.pop("owner",None)
                item.pop("lease_until",None)
                from .account_directory import identity
                display=identity(account)
                item.update(label=display["display_name"],identity_hint=display["identity_hint"])
                candidates.append({"id":candidate.id,"data":item})
            return {"id":rid,"version":row.version,"data":data,"total":total,"candidates":candidates,"page":page,"page_size":page_size}


def decode_json(value):
    return json.loads(value) if isinstance(value,str) else value


def without_entries(session):
    if session.get_bind().dialect.name=="sqlite":
        return func.json_remove(Resource.data,"$.entries")
    return cast(Resource.data,JSONB).op("-")("entries")
