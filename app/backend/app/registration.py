"""Durable interactive login. Side effects are claimed before network execution."""
import asyncio
import json
import secrets
import re
import time
import anyio
from contextlib import asynccontextmanager, nullcontext

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from fastapi import Query
from .registration_queries import registrations, recovery_candidates, page as registration_page

from .adapters import encode, required
from .models import Account, Credential, Resource
from .store import DomainError, day_at, uid
from .upstream import UpstreamError, payload
from .vendor.mirai.loomy import signing
from .login_connectors import SUPPORTED, connector, official_profile

# Only an explicit number-specific result is permanent. Generic send refusal
# also covers rate limits, challenges and egress failures.
_SMS_NUMBER_REFUSED = {"sms_number_rejected", "number_not_supported", "invalid_phone_number"}


async def _complete_cleanup(awaitable):
    """Join cleanup even if an HTTP disconnect or repeated task cancel arrives."""
    with anyio.CancelScope(shield=True):
        task = asyncio.ensure_future(awaitable)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()


def _loomy_error(desc):
    """把 Loomy 账号服务的文案归类成可判断的错误码。

    这个服务只回业务 code + desc，不同错误共用同一个 code；不区分的话
    「号码已被注册」会被当成普通失败，注册就永远撞在南墙上。
    """
    text = str(desc or "").lower()
    if any(word in text for word in ("未注册", "不存在", "没有找到", "未找到", "not exist", "not found", "unregistered", "no such")):
        return "loomy_not_registered"
    if any(word in text for word in ("已注册", "已存在", "已经注册", "exists", "already registered")):
        return "loomy_already_registered"
    if any(word in text for word in ("密码", "password")):
        return "loomy_password_required"
    if any(word in text for word in ("验证码", "mcode")) and any(word in text for word in ("错误", "无效", "过期", "invalid", "expired", "incorrect")):
        return "verification_code_incorrect"
    if any(word in text for word in ("频繁", "上限", "限制", "frequent", "limit")):
        return "rate_limit"
    return "upstream_rejected"


class LoomyLogin:
    def __init__(self, transport):
        self.t = transport
        #: 由 RegistrationService 在 _run 里注入；start 阶段要落密码和设备号。
        self.checkpoint = None

    async def call(self, path, params, device):
        raw = encode({"base": signing._base(device), "param": params}).decode()
        data = await self.t.json("POST", "https://account.xfinfr.com" + path, signing.sign_headers("POST", path, raw), raw.encode())
        return payload(data)

    async def call_explained(self, path, params, device):
        """和 call 一样，但业务错误按文案细分。只用于注册/登录这类需要分支的接口。"""
        raw = encode({"base": signing._base(device), "param": params}).decode()
        data = await self.t.json("POST", "https://account.xfinfr.com" + path, signing.sign_headers("POST", path, raw), raw.encode())
        if isinstance(data, dict):
            code = str(data.get("code") or "")
            if code and code not in {"0", "200", "000000", "success"}:
                desc = data.get("desc") or data.get("message") or data.get("msg") or ""
                raise UpstreamError(_loomy_error(desc), 400)
        return payload(data)

    async def start(self, material, mode):
        phone = required(material, "phone")
        if mode == "register":
            patch = {}
            if not material.get("device"):
                # 同一个设备号上建多个号是最明显的多开特征，新注册一律独立设备号。
                material["device"] = "web-" + secrets.token_hex(8)
                patch["device"] = material["device"]
            if not material.get("password"):
                # 注册是唯一能设密码的时刻；不设密码，这张 14 天的 session 到期就报废，
                # 接码号又收不到第二次短信，等于一次性账号。密码会随凭据入库，供续期用。
                material["password"] = "Lm" + secrets.token_hex(7)
                patch["password"] = material["password"]
            if patch and self.checkpoint:
                await self.checkpoint(patch)
        device = material.get("device") or "web"
        if mode == "password":
            public = await self.call("/login/account/getPuKey", {}, device)
            data = await self.call("/login/account/byPwd", {"loginid": phone, "password": signing.rsa_encrypt(required(public, "pukey"), required(material, "password")), "rcode": required(public, "rcode"), "type": 1, "expire": signing.SESSION_EXPIRE_SECONDS}, device)
            return self.result(data, material)
        data = await self.call("/login/phone/sendMsgCode", {"ccode": "86", "phone": phone, "expire": 300}, device)
        return {"challenge": required(data, "msgid"), "device": device}

    async def verify(self, material, mode, code):
        device = material.get("device") or "web"
        if mode == "register" and isinstance(material.get("issued_credential"), dict):
            # Resume only the read-back login after registration issued a
            # session. Never consume the SMS or create the account a second time.
            return await self.verify_registered_password(material["issued_credential"])
        challenge = {"ccode": "86", "phone": required(material, "phone"), "mcode": code, "msgid": required(material, "challenge"), "expire": signing.SESSION_EXPIRE_SECONDS}
        if mode == "register":
            # 先登录：接码平台卖出来的号大概率早就注册过（真实样本 isnew=0、haspwd=1），
            # 直接调注册接口只会拿到一个没有信息量的「参数不合法」。登录成功就用它，
            # 也说明这个号原本有自己的密码，不能把我们临时生成的密码写进凭据。
            try:
                data = await self.call_explained("/login/phone/checkCode", challenge, device)
                material.pop("password", None)
                return self.result(data, material)
            except UpstreamError as error:
                if error.code != "loomy_not_registered":
                    raise
            data = await self.call_explained("/register/phone/submit", {"phone": required(material, "phone"), "code": code, **({"password": material["password"]} if material.get("password") else {})}, device)
            return await self.verify_registered_password(self.result(data, material)["credential"])
        data = await self.call("/login/phone/checkCode", challenge, device)
        return self.result(data, material)

    async def verify_registered_password(self, credential):
        """7866 _loomy_register: save first, then verify the password identity."""
        credential = {**credential, "password_verified": False}
        expected = required(credential, "userid")
        if self.checkpoint:
            await self.checkpoint({"issued_credential": credential})
        try:
            device = credential.get("device") or "web"
            public = await self.call("/login/account/getPuKey", {}, device)
            data = await self.call("/login/account/byPwd", {
                "loginid": required(credential, "phone"),
                "password": signing.rsa_encrypt(required(public, "pukey"), required(credential, "password")),
                "rcode": required(public, "rcode"), "type": 1,
                "expire": signing.SESSION_EXPIRE_SECONDS,
            }, device)
            if str(required(data, "userid", "uid")) != str(expected):
                raise UpstreamError("identity_mismatch", 409)
            credential.update(session=required(data, "session"), password_verified=True,
                              password_verified_at=time.time(), password_verification_error=None)
        except UpstreamError as error:
            credential["password_verification_error"] = error.code
            if self.checkpoint:
                await self.checkpoint({"issued_credential": credential})
            if error.code == "identity_mismatch":
                raise
            # A registered account still exists when password read-back fails.
            # Keep its issued session and disclose the unverified password.
        except Exception:
            credential["password_verification_error"] = "password_verification_failed"
        if self.checkpoint:
            await self.checkpoint({"issued_credential": credential})
        return {"identity": expected, "credential": credential}

    def result(self, data, material):
        return {"identity": required(data, "userid", "uid"), "credential": {"session": required(data, "session"), "userid": required(data, "userid", "uid"), "phone": required(material, "phone"), **({"password": material["password"]} if material.get("password") else {}), "device": material.get("device", "web"), "profile": official_profile(data)}}


class RegistrationService:
    def __init__(self, store, registry, factory=LoomyLogin):
        self.store, self.registry, self.factory = store, registry, factory
        from .sms_channels import SmsChannels
        self.sms = SmsChannels(store,registry)
        from .recovery import Recovery
        self.recovery=Recovery(self)

    def identity_book(self, s, account, material, create=True):
        phone = re.sub(r"[\s-]", "", str(material.get("phone") or ""))
        if phone.startswith("+86") and re.fullmatch(r"\+861\d{10}", phone):
            phone = phone[3:]
        if not phone:
            return None, {}
        key = self.store.phone_key(account.platform, phone)
        row = s.scalar(select(Resource).where(Resource.kind == "relogin_identity", Resource.name == key))
        data = json.loads(row.data) if row else {"accounts": [], "no_sms_failures": 0}
        if not create:
            return row, data
        if row is None:
            row = Resource(id=uid(), kind="relogin_identity", name=key, data="{}", updated_at=self.store.clock())
            s.add(row)
        if account.id not in data["accounts"]:
            data["accounts"].append(account.id)
        row.data = json.dumps(data)
        return row, data

    def count_no_sms(self, s, account, data, material):
        if data.get("failure_counted") or account.credential_version != data["credential_version"]:
            return
        row, book = self.identity_book(s, account, material)
        failures = max(account.relogin_failures, book.get("no_sms_failures", 0)) + 1
        account.relogin_failures = failures
        if row:
            book["no_sms_failures"] = failures
            row.data, row.updated_at = json.dumps(book), self.store.clock()
            for aid in book["accounts"]:
                linked = s.get(Account, aid)
                if linked:
                    linked.relogin_failures = failures
        data["failure_counted"] = True

    def wait_expiry_error(self, s, data, material):
        browser_wait = data["status"] == "waiting_authorization" and data.get("browser_sms_confirmed") and not material.get("browser_code_fingerprint")
        if data.get("code_submitted") or (data["status"] != "waiting_code" and not browser_wait):
            return "verification_expired"
        lease_id = material.get("sms_lease_id") or data.get("sms_lease_id")
        lease = s.get(Resource, lease_id) if lease_id else None
        observation = json.loads(lease.data) if lease else {}
        if observation.get("status") == "code_received":
            return "verification_expired"
        if observation.get("poll_all_failed"):
            return observation.get("poll_error") or "sms_provider_rejected"
        return "no_sms"

    @asynccontextmanager
    async def execution(self, session_id, priority=False):
        from .operations import settings
        capacity = (await asyncio.to_thread(settings, self.store))["values"]["maintenance_capacity"]
        # A DB thread keeps running after its caller is cancelled. Join its
        # result before releasing, otherwise it may commit an orphan slot.
        with anyio.CancelScope(shield=True):
            acquisition = asyncio.create_task(asyncio.to_thread(self.store.acquire_execution, session_id, capacity, priority))
            try:
                token = await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                try:
                    token = await _complete_cleanup(acquisition)
                except Exception:
                    pass  # A rejected acquisition owns no slot.
                else:
                    await _complete_cleanup(asyncio.to_thread(self.store.release_execution, session_id, token))
                raise
        parent = asyncio.current_task()
        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(20)
                    await asyncio.to_thread(self.store.heartbeat_execution, session_id, token)
            except asyncio.CancelledError:
                raise
            except Exception:
                parent.cancel()
        pulse = asyncio.create_task(heartbeat())
        try:
            yield token
        finally:
            async def release():
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
                await asyncio.to_thread(self.store.release_execution, session_id, token)
            await _complete_cleanup(release())

    def assert_execution(self, s, session_id, token):
        if not token or not any(x["session_id"] == session_id and x["owner"] == token for x in self.store.execution_leases(s)):
            raise DomainError("registration_execution_lost")

    def proxy(self, data, material):
        if material.get("_proxy_selected"):
            # The encrypted session fixes its egress before SMS/login starts.
            # Later platform-route edits cannot change the IP mid-flow.
            if material.get("proxy_id"):
                with self.store.read() as (s, _):
                    row = s.get(Resource, material["proxy_id"])
                    if not row or row.kind != "proxy" or json.loads(row.data).get("status") in {"retired", "disabled"}:
                        raise DomainError("proxy_unavailable", 503)
                if not material.get("_proxy_url"):
                    raise DomainError("proxy_unavailable", 503)
            return material.get("_proxy_url")
        if data.get("account_id"):
            return self.store.account_proxy(data["account_id"])
        if not material.get("proxy_id"):
            return None
        with self.store.tx() as (s,_):
            row=s.get(Resource,material["proxy_id"])
            if not row or row.kind!="proxy" or not row.secret:
                raise DomainError("missing_proxy",422)
            if json.loads(row.data).get("status") in {"retired", "disabled"}:
                raise DomainError("proxy_unavailable",503)
            return self.store.cipher.decrypt(row.secret.encode()).decode()

    def recover(self):
        """Expired execution is uncertainty, never permission to resend a code."""
        # Lock-free scan first: most ticks have nothing to recover and must not queue on the write lock.
        with self.store.read() as (s, _):
            active = {x["session_id"] for x in self.store.execution_leases(s)}
            now = self.store.clock()
            pending = False
            for row in s.scalars(recovery_candidates(s, now).where(Resource.id.not_in(active)).order_by(Resource.updated_at, Resource.id).limit(256)):
                data = json.loads(row.data)
                if row.id not in active and (data["status"] in {"sending", "verifying"} or (data["status"].startswith("waiting_") and data.get("expires_at") and data["expires_at"] <= now)):
                    pending = True
                    break
        if not pending:
            return
        with self.store.tx() as (s, state):
            active = {x["session_id"] for x in self.store.execution_leases(s)}
            now = self.store.clock()
            for row in s.scalars(recovery_candidates(s, now).where(Resource.id.not_in(active)).order_by(Resource.updated_at, Resource.id).limit(256)):
                data = json.loads(row.data)
                if row.id in active:
                    continue
                if data["status"] in {"sending", "verifying"}:
                    data.update(status="uncertain", error="executor_lost", operation=None)
                elif data["status"].startswith("waiting_") and data.get("expires_at") and data["expires_at"] <= now:
                    material = json.loads(self.store.cipher.decrypt(row.secret.encode())) if row.secret else {}
                    if any(material.get(k) for k in ("issued_result", "issued_credential", "issued_token")):
                        data.update(status="uncertain", error="issued_credential_pending")
                    else:
                        code = self.wait_expiry_error(s, data, material)
                        no_sms = code == "no_sms"
                        data.update(status="failed", error=code, finished_at=now)
                        if no_sms and data.get("account_id") and not data.get("failure_counted"):
                            a = self.store.account(s, data["account_id"])
                            self.count_no_sms(s,a,data,material)
                else:
                    continue
                row.data, row.updated_at, row.version = json.dumps(data), now, row.version + 1
                self.store.emit(s, state, "registration", data["error"], data.get("account_id"))

    def cancel(self, session_id):
        with self.store.tx() as (s, state):
            row = s.get(Resource, session_id)
            if not row or row.kind != "registration":
                raise DomainError("registration_not_found", 404)
            data = json.loads(row.data)
            if data["status"] in {"succeeded", "cancelled"}:
                return
            if any(x["session_id"] == session_id for x in self.store.execution_leases(s)):
                raise DomainError("registration_execution_busy")
            if data["status"] in {"sending", "verifying"}:
                raise DomainError("registration_result_unresolved")
            material = json.loads(self.store.cipher.decrypt(row.secret.encode())) if row.secret else {}
            if data["status"] == "uncertain" or any(material.get(k) for k in ("issued_result","issued_credential","issued_token")):
                raise DomainError("registration_result_unresolved")
            data.update(status="cancelled", error="cancelled", finished_at=self.store.clock(), operation=None,
                        result_unresolved=data["status"] == "uncertain" or bool(material.get("issued_result")))
            row.data, row.version, row.updated_at = json.dumps(data), row.version + 1, self.store.clock()
            self.store.emit(s, state, "registration", "cancelled", data.get("account_id"))

    def close_rejected(self, session_id):
        """Archive an explicitly rejected candidate without erasing attempt limits."""
        with self.store.tx() as (s, state):
            row = s.get(Resource, session_id)
            if not row or row.kind != "registration":
                raise DomainError("registration_not_found", 404)
            data = json.loads(row.data)
            if data["status"] != "failed" or data.get("error") not in {"invalid_credentials", "identity_mismatch", "invalid_credential_format", "wps_sso_incomplete"}:
                raise DomainError("registration_result_unresolved")
            if any(x["session_id"] == session_id for x in self.store.execution_leases(s)):
                raise DomainError("registration_execution_busy")
            # Keep the rejected evidence encrypted; exclude it from future work.
            s.add(Resource(id=uid(), kind="rejected_login_receipt", name=session_id, data=json.dumps({"account_id": data.get("account_id"), "error": data["error"], "archived_at": self.store.clock()}), secret=row.secret, updated_at=self.store.clock()))
            material = json.loads(self.store.cipher.decrypt(row.secret.encode())) if row.secret else {}
            material = {k: material[k] for k in ("sms_lease_id", "auto_run") if k in material}
            row.secret = self.store.cipher.encrypt(json.dumps(material).encode()).decode()
            data.update(status="cancelled", result_unresolved=False, finished_at=self.store.clock(), operation=None)
            row.data, row.version, row.updated_at = json.dumps(data), row.version+1, self.store.clock()
            self.store.emit(s, state, "registration", "rejected_candidate_archived", data.get("account_id"))

    def create(self, platform, label, mode, material, account_id=None, transaction=None):
        if mode not in SUPPORTED.get(platform, []):
            raise DomainError("registration_adapter_unavailable", 501)
        if mode in {"sms", "password", "register"} and (not material.get("phone") and not material.get("sms_channel_id") or mode == "password" and not material.get("password")):
            raise DomainError("missing_material", 422)
        if mode == "verified_import" and not isinstance(material.get("credential"), dict):
            raise DomainError("missing_material", 422)
        with (nullcontext(transaction) if transaction else self.store.tx()) as (s, state):
            if material.get("proxy_id") and not account_id:
                proxy=s.get(Resource,material["proxy_id"])
                if not proxy or proxy.kind!="proxy" or not proxy.secret or json.loads(proxy.data).get("status")=="retired":
                    raise DomainError("missing_proxy",422)
            a = self.store.account(s, account_id) if account_id else None
            if a and a.platform != platform:
                raise DomainError("identity_mismatch", 422)
            if a and a.sandbox:
                raise DomainError("sandbox_account_not_login_target", 422)
            if a:
                for existing in s.scalars(registrations(s, a.id, pending=True).limit(1)):
                    old=json.loads(existing.data)
                    if old.get("account_id")==a.id and (old.get("result_unresolved") or old["status"] not in {"succeeded","failed","cancelled"}):
                        raise DomainError("relogin_result_unresolved")
                material = {**material, "expected_identity": a.external_id}
                if a.credential_version:
                    saved=s.scalar(select(Credential).where(Credential.account_id==a.id,Credential.version==a.credential_version))
                    previous=json.loads(self.store.cipher.decrypt(saved.ciphertext.encode())) if saved else {}
                    if "phone" not in material and isinstance(previous,dict):
                        material["phone"]=previous.get("phone") or (previous.get("account") or {}).get("phoneNumber")
                if not material.get("phone") and mode in {"sms","password","register"}:
                    raise DomainError("relogin_phone_required",422)
                if material.get("sms_channel_id") and not material.get("phone"):
                    raise DomainError("relogin_phone_required",422)
            from .proxy_pool import select_registration_proxy
            session_id = uid()
            proxy_id, proxy_url = select_registration_proxy(self.store, s, platform, session_id, material.get("proxy_id"), account_id)
            # Keep login egress stable, but do not turn a pool choice into a
            # permanent account binding after registration. Derive provenance
            # only from server-side configuration, inside the selection txn.
            binding = s.scalar(select(Resource).where(Resource.kind == "proxy_binding", Resource.name == account_id)) if account_id else None
            route_row = s.scalar(select(Resource).where(Resource.kind == "proxy_route", Resource.name == platform))
            route = json.loads(route_row.data) if route_row else {"mode": "direct"}
            proxy_origin = "binding" if binding else "explicit" if material.get("proxy_id") and not account_id else route["mode"]
            proxy_pool_id = route.get("pool_id") if proxy_origin == "pool" else None
            material = {**material, "proxy_id": proxy_id, "_proxy_selected": True, "_proxy_url": proxy_url,
                        "_proxy_origin": proxy_origin, "_proxy_pool_id": proxy_pool_id}
            record = {"platform": platform, "mode": mode, "account_id": account_id, "status": "prepared", "created_at": self.store.clock(), "expires_at": None, "error": None, "credential_version": a.credential_version if a else None, "batch_id": material.get("batch_id"), "proxy_id": proxy_id}
            r = Resource(id=session_id, kind="registration", name=label, data=json.dumps(record), secret=self.store.cipher.encrypt(json.dumps(material).encode()).decode(), updated_at=self.store.clock())
            s.add(r)
            self.store.emit(s, state, "registration", "prepared", account_id)
            return r.id

    def transition(self, session_id, expected, status, token):
        with self.store.tx() as (s, state):
            self.assert_execution(s, session_id, token)
            r = s.get(Resource, session_id)
            if not r or r.kind != "registration":
                raise DomainError("registration_not_found", 404)
            data = json.loads(r.data)
            if data["status"] != expected:
                raise DomainError("registration_state_conflict")
            material = json.loads(self.store.cipher.decrypt(r.secret.encode())) if r.secret else {}
            issued = any(material.get(k) for k in ("issued_result", "issued_credential", "issued_token"))
            if (expected.startswith("waiting_") or expected == "uncertain") and data.get("expires_at") and data["expires_at"] <= self.store.clock() and not issued:
                raise DomainError("verification_expired", 409)
            if expected == "prepared" and data.get("account_id"):
                a = self.store.account(s, data["account_id"])
                if a.relogin_failures >= 3:
                    raise DomainError("relogin_attempts_exhausted")
                if a.relogin_day == day_at(self.store.clock()):
                    raise DomainError("relogin_already_scheduled_today")
                identity_row, identity = self.identity_book(s, a, material)
                if identity.get("no_sms_failures", 0) >= 3:
                    raise DomainError("relogin_attempts_exhausted")
                if identity.get("day") == day_at(self.store.clock()):
                    raise DomainError("relogin_already_scheduled_today")
                if a.credential_version != data["credential_version"]:
                    raise DomainError("credential_version_conflict")
                unresolved = s.scalars(registrations(s, a.id, pending=True).where(Resource.id != r.id).limit(1))
                for other in unresolved:
                    old = json.loads(other.data)
                    if old.get("account_id") == a.id and (old.get("result_unresolved") or old.get("status", "").startswith("waiting_") or old.get("status") in {"sending", "verifying", "uncertain"}):
                        raise DomainError("relogin_result_unresolved")
                a.relogin_day = day_at(self.store.clock())
                if identity_row:
                    identity.update(day=a.relogin_day, last_session_id=session_id)
                    identity_row.data, identity_row.updated_at = json.dumps(identity), self.store.clock()
            data["previous_status"], data["status"], data["operation"] = expected, status, secrets.token_hex(16)
            data["execution_owner"] = token
            r.data, r.version, r.updated_at = json.dumps(data), r.version + 1, self.store.clock()
            self.store.emit(s, state, "registration", status, data.get("account_id"))
            return data, json.loads(self.store.cipher.decrypt(r.secret.encode()))

    def checkpoint(self, session_id, operation, patch):
        with self.store.tx() as (s, state):
            r = s.get(Resource, session_id)
            data = json.loads(r.data)
            self.assert_execution(s, session_id, data.get("execution_owner"))
            if data.get("operation") != operation or data["status"] not in {"sending", "verifying"}:
                raise DomainError("registration_state_conflict")
            material = json.loads(self.store.cipher.decrypt(r.secret.encode()))
            r.secret = self.store.cipher.encrypt(json.dumps({**material, **patch}).encode()).decode()
            for key in ("step","sms_lease_id","code_submitted"):
                if key in patch:
                    data[key]=patch[key]
            r.data=json.dumps(data)
            r.updated_at, r.version = self.store.clock(), r.version + 1
            self.store.emit(s, state, "registration", "credential_checkpoint", data.get("account_id"))

    def details(self, session_id):
        with self.store.read() as (s, _):
            r = s.get(Resource, session_id)
            if not r or r.kind != "registration":
                raise DomainError("registration_not_found", 404)
            data = json.loads(r.data)
            material = json.loads(self.store.cipher.decrypt(r.secret.encode())) if r.secret else {}
            return data, material

    def finish(self, session_id, operation, result=None, error=None, material=None):
        with self.store.tx() as (s, state):
            r = s.get(Resource, session_id)
            data = json.loads(r.data)
            self.assert_execution(s, session_id, data.get("execution_owner"))
            if data.get("operation") != operation or data["status"] not in {"sending", "verifying"}:
                raise DomainError("registration_state_conflict")
            if error:
                # Network ambiguity is not an account failure and must not resend SMS.
                data["status"] = "uncertain" if error in {"network", "upstream_timeout", "response_read_failed", "internal_error", "upstream_5xx", "invalid_response", "response_too_large"} else "failed"
                if error in {"verification_code_incorrect", "invalid_captcha_result", "oauth_state_mismatch", "invalid_oauth_callback", "rate_limit"} and data.get("previous_status", "").startswith("waiting_"):
                    data["status"] = data["previous_status"]
                data["error"] = error
                saved=json.loads(self.store.cipher.decrypt(r.secret.encode())) if r.secret else {}
                data["result_unresolved"]=any(saved.get(k) for k in ("issued_result","issued_credential","issued_token"))
                if data["status"] == "failed":
                    data["finished_at"] = self.store.clock()
                # Only "no SMS received" counts toward stopping automatic relogin. Other
                # errors stay on this record with their own code and are not counted as no_sms.
                if data.get("account_id") and not data.get("failure_counted") and error == "no_sms":
                    a = self.store.account(s, data["account_id"])
                    self.count_no_sms(s,a,data,saved)
                if error in _SMS_NUMBER_REFUSED and saved.get("phone"):
                    # The classified result explicitly identifies this number.
                    self.store.mark_phone_rejected(s, data["platform"], saved["phone"], error)
            elif result.get("waiting"):
                current = json.loads(self.store.cipher.decrypt(r.secret.encode()))
                data["status"] = result["waiting"]
                data["expires_at"] = data.get("expires_at") or self.store.clock() + 600
                data["error"] = None
                r.secret = self.store.cipher.encrypt(json.dumps({**current, **result}).encode()).decode()
            elif result.get("challenge"):
                data["status"], data["expires_at"] = "waiting_code", self.store.clock() + result.get("expires_in", 300)
                current = json.loads(self.store.cipher.decrypt(r.secret.encode()))
                r.secret = self.store.cipher.encrypt(json.dumps({**current, **result}).encode()).decode()
            else:
                identity, credential = result["identity"], result["credential"]
                saved=json.loads(self.store.cipher.decrypt(r.secret.encode())) if r.secret else {}
                if saved.get("phone"):
                    credential={**credential,"phone":saved["phone"]}
                if saved.get("sms_channel_id"):
                    credential={**credential,"sms_channel_id":saved["sms_channel_id"]}
                if data.get("account_id"):
                    a = self.store.account(s, data["account_id"])
                    if a.external_id != identity:
                        raise DomainError("identity_mismatch")
                    if a.credential_version != data["credential_version"]:
                        raise DomainError("credential_version_conflict")
                else:
                    a = s.scalar(select(Account).where(Account.platform == data["platform"], Account.external_id == identity))
                    if a:
                        raise DomainError("account_exists_use_relogin")
                    a = Account(id=uid(), platform=data["platform"], external_id=identity, label=r.name, sandbox=False, enabled=True, credential_version=0, created_at=self.store.clock(), updated_at=self.store.clock())
                    s.add(a)
                    s.flush()
                    if a.platform in {"Loomy", "小浣熊"} and data.get("mode") != "verified_import":
                        # Separate one-time rewards from daily sign-in. Only
                        # genuinely new registrations create this marker; an
                        # existing account's relogin/import must not claim it.
                        s.add(Resource(id=uid(), kind="onboarding_pending", name=a.id,
                                       data=json.dumps({"status": "pending", "source": "registration", "session_id": session_id}),
                                       updated_at=self.store.clock()))
                    if saved.get("proxy_id"):
                        if saved.get("_proxy_origin") == "pool" and saved.get("_proxy_pool_id"):
                            # Retain the successful login's first egress as
                            # affinity. The normal pool resolver may replace
                            # an unhealthy node and ignores a stale pool ID if
                            # the platform route changed during login.
                            s.add(Resource(id=uid(), kind="proxy_affinity", name=a.id,
                                           data=json.dumps({"pool_id": saved["_proxy_pool_id"], "proxy_id": saved["proxy_id"], "platform": a.platform}),
                                           updated_at=self.store.clock()))
                        else:
                            # Explicit/fixed choices and old sessions without
                            # trusted provenance preserve their binding rule.
                            s.add(Resource(id=uid(),kind="proxy_binding",name=a.id,data=json.dumps({"proxy_id":saved["proxy_id"]}),updated_at=self.store.clock()))
                self.store.record_phone(s, a.platform, saved.get("phone"), "registered", a.id)
                previous_version = a.credential_version
                a.credential_version += 1
                a.credential_status, a.updated_at, a.relogin_failures = "pending", self.store.clock(), 0
                identity_row, identity = self.identity_book(s, a, saved)
                if identity_row:
                    identity["no_sms_failures"] = 0
                    identity_row.data, identity_row.updated_at = json.dumps(identity), self.store.clock()
                    for aid in identity["accounts"]:
                        linked = s.get(Account, aid)
                        if linked:
                            linked.relogin_failures = 0
                s.add(Credential(id=uid(), account_id=a.id, version=a.credential_version, ciphertext=self.store.cipher.encrypt(json.dumps(credential).encode()).decode(), created_at=self.store.clock()))
                from .native_identity import capture_identity
                capture_identity(a, credential)
                from .native_lifecycle import record_native_login
                record_native_login(self.store, s, a, data["mode"], credential, previous_version=previous_version)
                # One transaction: login receipt, credential, next maintenance job.
                self.store.after_login(s, state, a, f"registration:{session_id}")
                data.update({"status": "succeeded", "account_id": a.id, "finished_at": self.store.clock(), "result_unresolved":False})
                r.secret = None
            r.data, r.updated_at, r.version = json.dumps(data), self.store.clock(), r.version + 1
            self.store.emit(s, state, "registration", data["status"], data.get("account_id"))
            return {k: v for k, v in data.items() if k not in {"operation", "execution_owner"}}

    def expire_sms(self, session_id):
        """A single account failure only after the code-wait window expires."""
        with self.store.tx() as (s, state):
            r = s.get(Resource, session_id)
            if not r or r.kind != "registration":
                raise DomainError("registration_not_found", 404)
            data = json.loads(r.data)
            if data["status"] != "waiting_code" or data["expires_at"] > self.store.clock():
                raise DomainError("sms_wait_not_expired")
            material = json.loads(self.store.cipher.decrypt(r.secret.encode())) if r.secret else {}
            code=self.wait_expiry_error(s,data,material)
            data.update({"status": "failed", "error": code, "finished_at": self.store.clock()})
            if code=="no_sms" and data.get("account_id") and not data.get("failure_counted"):
                a = self.store.account(s, data["account_id"])
                self.count_no_sms(s,a,data,material)
            r.data, r.version, r.updated_at = json.dumps(data), r.version + 1, self.store.clock()
            self.store.emit(s, state, "registration", code, data.get("account_id"))

    async def run(self, session_id, code=None, continuation=False, selected_identity=None):
        async with self.execution(session_id, priority=not (code or continuation)) as token:
            return await self._run(session_id, code, continuation, selected_identity, token)

    async def poll_sms(self, session_id):
        if not self.registry.enabled:
            raise DomainError("network_execution_disabled")
        async with self.execution(session_id) as token:
            data, material = (await asyncio.to_thread(self.details, session_id))
            if data["status"] != "waiting_code" or not material.get("sms_lease_id"):
                raise DomainError("sms_lease_not_active")
            if data.get("expires_at") and data["expires_at"] <= self.store.clock():
                (await asyncio.to_thread(self.expire_sms, session_id))
                expired, _ = await asyncio.to_thread(self.details, session_id)
                return {"status": "failed", "error": expired["error"]}
            code = await self.sms.poll(material["sms_lease_id"])
            if not code:
                return {"status":"waiting_code"}
            result=await self._run(session_id,code,token=token)
            if result.get("error")=="verification_code_incorrect":
                await asyncio.to_thread(self.sms.reject_code,material["sms_lease_id"],code)
            return result

    async def _run(self, session_id, code=None, continuation=False, selected_identity=None, token=None):
        if not self.registry.enabled:
            raise DomainError("network_execution_disabled", 409)
        current, saved = (await asyncio.to_thread(self.details, session_id))
        verifying = bool(code) or continuation
        expected = current["status"] if verifying else "prepared"
        allowed = {"waiting_code", "waiting_authorization", "waiting_identity", "waiting_captcha", "waiting_ticket"}
        # A read-only WorkBuddy poll can resume after ambiguous network failure.
        if current["platform"].startswith("WorkBuddy") and (current["mode"] == "official" or saved.get("sms_verified")):
            allowed.add("uncertain")
        issued = any(saved.get(k) for k in ("issued_credential", "issued_token", "issued_result"))
        if issued:
            allowed.add("uncertain")
            allowed.add("failed")
        elif current["status"] == "uncertain" and not current.get("code_submitted"):
            # 码已经买到、还没提交过：只是执行器中途被重启，码在有效期内就继续验证，
            # 不必再买一次号、再发一次短信。
            allowed.add("uncertain")
        if verifying and expected not in allowed:
            raise DomainError("registration_state_conflict")
        if verifying and current.get("expires_at") and current["expires_at"] <= self.store.clock() and not issued:
            raise DomainError("verification_expired")
        data, material = (await asyncio.to_thread(self.transition, session_id, expected, "verifying" if verifying else "sending", token))
        transport = None
        try:
            if material.get("issued_result"):
                return (await asyncio.to_thread(self.finish, session_id, data["operation"], result=material["issued_result"]))
            if not verifying and material.get("sms_channel_id") and material.get("phone"):
                material["sms_lease_id"]=(await asyncio.to_thread(self.sms.existing_number, material["sms_channel_id"],session_id,material["phone"]))
                (await asyncio.to_thread(self.checkpoint, session_id,data["operation"],{"sms_lease_id":material["sms_lease_id"]}))
            if not verifying and material.get("sms_channel_id") and not material.get("phone"):
                lease_id,phone = await self.sms.acquire(material["sms_channel_id"],session_id)
                material.update({"phone":phone,"sms_lease_id":lease_id})
                (await asyncio.to_thread(self.checkpoint, session_id,data["operation"],{"phone":phone,"sms_lease_id":lease_id}))
            transport = self.registry.transport_factory(proxy=(await asyncio.to_thread(self.proxy, data,material)))
            adapter = self.factory(transport) if data["platform"] == "Loomy" else connector(data["platform"], transport, data["mode"])
            if selected_identity:
                material["selected_identity"] = selected_identity
                (await asyncio.to_thread(self.checkpoint, session_id, data["operation"], {"selected_identity": selected_identity}))
            async def checkpoint(patch):
                await asyncio.to_thread(self.checkpoint, session_id, data["operation"], patch)
            adapter.checkpoint = checkpoint
            if code:
                # Resuming an uncertain, not-yet-submitted challenge consumes
                # the same one-time code. Persist intent on every such path.
                (await asyncio.to_thread(self.checkpoint, session_id,data["operation"],{"code_submitted":True}))
            async with asyncio.timeout(90):
                result = await adapter.verify(material, data["mode"], code) if verifying else await adapter.start(material, data["mode"])
            if result.get("credential"):
                # Keep issued material if local identity/version commit fails.
                (await asyncio.to_thread(self.checkpoint, session_id, data["operation"], {"issued_result": result}))
            return (await asyncio.to_thread(self.finish, session_id, data["operation"], result=result, material=material))
        except (UpstreamError, DomainError, TimeoutError) as error:
            return (await asyncio.to_thread(self.finish, session_id, data["operation"], error=getattr(error, "code", "upstream_timeout")))
        except asyncio.CancelledError:
            try:
                await _complete_cleanup(asyncio.to_thread(self.finish, session_id, data["operation"], error="network"))
            except DomainError:
                pass
            raise
        except Exception:
            return (await asyncio.to_thread(self.finish, session_id, data["operation"], error="internal_error"))
        finally:
            if transport:
                try:
                    await _complete_cleanup(transport.close())
                except Exception:
                    # Closing a connection must not overwrite a persisted
                    # success, challenge or known upstream error.
                    pass


class RegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str
    label: str = Field(min_length=1, max_length=100)
    mode: str
    phone: SecretStr | None = Field(default=None, min_length=5, max_length=30)
    password: SecretStr | None = None
    account_id: str | None = None
    credential: SecretStr | None = Field(default=None, max_length=32768)
    identity_field: str | None = None
    sms_channel_id: str | None = None
    auto_run: bool = False
    proxy_id: str | None = None


class CodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr = Field(min_length=1, max_length=4096)


class ContinueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_identity: str | None = Field(default=None, max_length=300)


class AttachInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    account_id:str
    expected_version:int=Field(ge=0)


def mount_registration(app, store, registry):
    service = RegistrationService(store, registry)
    from .browser_login import BrowserHub, mount_browser
    browser = BrowserHub(store,registry,service)
    app.state.registration_browser = browser
    app.state.registration_service = service
    mount_browser(app,browser)
    from .oauth_callback import mount_oauth_callback
    mount_oauth_callback(app,service,browser)
    from .sms_channels import mount_sms_channels
    mount_sms_channels(app,service.sms)
    from .registration_runner import RegistrationRunner,mount_registration_runner
    runner=RegistrationRunner(service,app.state.worker)
    app.state.registration_runner=runner
    mount_registration_runner(app,runner)
    from .recovery import mount_recovery
    mount_recovery(app,service.recovery)
    from .task_receipts import mount_task_receipts
    mount_task_receipts(app,store,registry)

    @app.get("/api/registration")
    def sessions(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                 platform: str | None = None, status: str | None = None,
                 account_id: str | None = None, q: str | None = Query(None, max_length=100)):
        return {**registration_page(store, page, page_size, platform, status, account_id, q),
                "supported": SUPPORTED, "network_enabled": registry.enabled}

    @app.post("/api/registration", status_code=201)
    def prepare(body: RegistrationInput):
        material = {"phone": body.phone.get_secret_value()} if body.phone else {}
        if body.password:
            material["password"] = body.password.get_secret_value()
        if body.credential:
            try:
                material["credential"] = json.loads(body.credential.get_secret_value())
            except ValueError:
                raise DomainError("invalid_credential_format", 422) from None
        material["identity_field"] = body.identity_field
        material["sms_channel_id"],material["auto_run"] = body.sms_channel_id,body.auto_run
        material["proxy_id"]=body.proxy_id
        return {"id": service.create(body.platform, body.label, body.mode, material, body.account_id)}

    @app.post("/api/registration/{session_id}/start")
    async def start(session_id: str):
        return await service.run(session_id)

    @app.post("/api/registration/{session_id}/code")
    async def submit(session_id: str, body: CodeInput):
        return await service.run(session_id, body.code.get_secret_value())

    @app.get("/api/registration/{session_id}/interaction")
    def interaction(session_id: str):
        data, material = service.details(session_id)
        matches=[]
        if material.get("issued_result") and not data.get("account_id"):
            with store.read() as (s,_):
                matches=[{"id":a.id,"label":a.label,"version":a.credential_version} for a in s.scalars(select(Account).where(Account.platform==data["platform"],Account.external_id==str(material["issued_result"]["identity"]),Account.sandbox.is_(False)))]
        return {"status": data["status"], "expires_at": data.get("expires_at"), "authorization_url": material.get("authorization_url") if data["status"] == "waiting_authorization" else None, "identities": material.get("identities", []) if data["status"] == "waiting_identity" else [], "captcha_options": material.get("captcha_options", []) if data["status"] == "waiting_captcha" else [], "step": material.get("step"), "has_issued_result": bool(material.get("issued_result")), "can_verify_candidate": bool(material.get("issued_credential") or material.get("issued_token")),"matching_accounts":matches}

    @app.post("/api/registration/{session_id}/continue")
    async def continue_login(session_id: str, body: ContinueInput):
        return await service.run(session_id, continuation=True, selected_identity=body.selected_identity)

    @app.post("/api/registration/{session_id}/no-code")
    def no_code(session_id: str):
        service.expire_sms(session_id)
        return {"ok": True}

    @app.post("/api/registration/{session_id}/poll-sms")
    async def poll_sms(session_id:str):
        return await service.poll_sms(session_id)

    @app.post("/api/registration/{session_id}/finalize")
    async def finalize(session_id:str):
        async with service.execution(session_id) as token:
            data,material=(await asyncio.to_thread(service.details, session_id))
            result=material.get("issued_result")
            if not result or data["status"] not in {"uncertain","failed"}:
                raise DomainError("registration_result_not_available")
            claimed,_=(await asyncio.to_thread(service.transition, session_id,data["status"],"verifying",token))
            try:
                return (await asyncio.to_thread(service.finish, session_id,claimed["operation"],result=result))
            except DomainError as error:
                return (await asyncio.to_thread(service.finish, session_id,claimed["operation"],error=error.code))

    @app.post("/api/registration/{session_id}/cancel")
    async def cancel(session_id: str):
        service.cancel(session_id)
        async with browser.lock(session_id):
            await browser.close_session(session_id)
        return {"ok": True}

    @app.post("/api/registration/{session_id}/close-rejected")
    async def close_rejected(session_id: str):
        service.close_rejected(session_id)
        async with browser.lock(session_id):
            await browser.close_session(session_id)
        return {"ok": True}

    @app.post("/api/registration/{session_id}/attach")
    async def attach(session_id:str,body:AttachInput):
        async with service.execution(session_id) as token:
            data,material=(await asyncio.to_thread(service.details, session_id))
            result=material.get("issued_result")
            if not result or data.get("account_id") or data["status"] not in {"failed","uncertain"}:
                raise DomainError("registration_result_not_available")
            claimed,_=(await asyncio.to_thread(service.transition, session_id,data["status"],"verifying",token))
            try:
                with store.tx() as (s,state):
                    account=store.account(s,body.account_id)
                    if account.sandbox or account.platform!=data["platform"] or account.external_id!=str(result["identity"]):
                        raise DomainError("identity_mismatch")
                    if account.credential_version!=body.expected_version:
                        raise DomainError("credential_version_conflict")
                    row=s.get(Resource,session_id)
                    current=json.loads(row.data)
                    service.assert_execution(s,session_id,token)
                    current.update(account_id=account.id,credential_version=account.credential_version)
                    row.data,row.version,row.updated_at=json.dumps(current),row.version+1,store.clock()
                    store.emit(s,state,"registration","existing_identity_attached",account.id)
                return (await asyncio.to_thread(service.finish, session_id,claimed["operation"],result=result))
            except DomainError as error:
                return (await asyncio.to_thread(service.finish, session_id,claimed["operation"],error=error.code))
