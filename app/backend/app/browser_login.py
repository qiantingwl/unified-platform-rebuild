"""Registration browser, executed by the separate browser executor service.

The main program owns the task: registration state, encrypted material,
captured callbacks, credential collection and retries live in the database.
The executor container only runs Chromium. Every session start sends the saved
storage_state, so an executor crash or restart loses at most the last few
seconds of page state and never a credential. A captured OAuth callback is
fingerprinted before it is consumed, so a lost executor cannot cause a second
code exchange. Explicit automatic registration tasks can drive ordinary forms;
challenge solving stays with the official page and is never bypassed.
"""
import asyncio
import hashlib
import json
import os
from urllib.parse import urlsplit

import httpx
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .models import Resource
from .store import DomainError, uid
from .upstream import UpstreamError

RETRYABLE = {"account_maintenance_busy", "maintenance_capacity_busy", "registration_execution_busy", "maintenance_priority_pending"}


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    x: float = Field(default=0, ge=0, le=1280)
    y: float = Field(default=0, ge=0, le=900)
    text: SecretStr | None = Field(default=None, max_length=4096)
    delta: int = Field(default=500, ge=-900, le=900)
    end_x: float = Field(default=0, ge=0, le=1280)
    end_y: float = Field(default=0, ge=0, le=900)


class ExecutorClient:
    """Authenticated HTTP client for the browser executor; maps its errors to DomainError."""

    def __init__(self, url=None, token=None, client=None):
        self.url = (url if url is not None else os.getenv("UNIFIED_BROWSER_URL", "")).rstrip("/")
        self.token = token if token is not None else os.getenv("UNIFIED_BROWSER_TOKEN", "")
        self.client = client
        self.health = {"status": "not_configured" if not self.url else "unknown"}

    @property
    def configured(self):
        return bool(self.url and self.token)

    def http(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), headers={"Authorization": "Bearer " + self.token})
        return self.client

    async def call(self, method, path, body=None, raw=False):
        if not self.configured:
            raise DomainError("browser_runtime_unavailable", 503)
        try:
            response = await self.http().request(method, self.url + path, json=body)
        except httpx.HTTPError:
            self.health = {**self.health, "status": "unreachable"}
            raise DomainError("browser_executor_unreachable", 503) from None
        if response.status_code >= 400:
            try:
                code = response.json()["error"]["code"]
            except Exception:
                code = "browser_executor_error"
            raise DomainError(code, response.status_code if response.status_code < 500 else 502)
        return response.content if raw else response.json()

    async def check(self):
        try:
            result = await self.call("GET", "/health")
            self.health = {"status": "ok", **result}
        except DomainError as error:
            self.health = {**self.health, "status": "unreachable" if error.code != "browser_runtime_unavailable" else "not_configured"}
        return self.health

    async def close(self):
        if self.client is not None:
            await self.client.aclose()


class BrowserHub:
    def __init__(self, store, registry, service, executor=None):
        self.store, self.registry, self.service = store, registry, service
        self.executor = executor or ExecutorClient()
        # sid -> executor instance that holds it; rebuilt from the executor on start.
        self.contexts, self.locks = {}, {}
        self.watch_tasks = {}

    def lock(self, sid):
        return self.locks.setdefault(sid, asyncio.Lock())

    def persist(self, sid, patch):
        with self.store.tx() as (s, state):
            row = s.get(Resource, sid)
            if not row or row.kind != "registration":
                raise DomainError("registration_not_found", 404)
            data = json.loads(row.data)
            if not data["status"].startswith("waiting_"):
                raise DomainError("registration_state_conflict")
            secret = json.loads(self.store.cipher.decrypt(row.secret.encode()))
            row.secret = self.store.cipher.encrypt(json.dumps({**secret, **patch}).encode()).decode()
            for key in ("browser_stage", "browser_error", "browser_stage_at", "browser_sms_confirmed", "browser_sms_http_status"):
                if key in patch:
                    data[key] = patch[key]
            row.data = json.dumps(data)
            row.updated_at, row.version = self.store.clock(), row.version + 1
            self.store.emit(s, state, "registration", "browser_checkpoint", data.get("account_id"))

    def capture_prefixes(self, platform):
        if platform == "Marvis":
            from .vendor.mirai.marvis import endpoints as marvis
            return [marvis.oauth_url()]
        return []

    async def context(self, sid):
        """Ensure the executor holds a session for sid; reopen from saved state when it does not."""
        if not self.registry.enabled:
            raise DomainError("network_execution_disabled")
        data, material = await asyncio.to_thread(self.service.details, sid)
        if not data["status"].startswith("waiting_") or (data.get("expires_at") or 0) <= self.store.clock():
            raise DomainError("verification_expired")
        if sid in self.contexts:
            try:
                state = await self.executor.call("GET", f"/sessions/{sid}")
                if state["instance"] == self.contexts[sid] and not state["closed"]:
                    await self.absorb(sid, state, material)
                    return state
            except DomainError as error:
                if error.code not in {"browser_session_missing", "browser_page_closed"}:
                    raise
            self.contexts.pop(sid, None)
        link = material.get("authorization_url")
        parsed = urlsplit(link or "")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise DomainError("invalid_authorization_url", 422)
        proxy = await asyncio.to_thread(self.service.proxy, data, material)
        body = {"url": link, "proxy": proxy, "capture_prefixes": self.capture_prefixes(data["platform"]), "deadline": data.get("expires_at")}
        if material.get("browser_state"):
            body["storage_state"] = material["browser_state"]
        elif material.get("cookies"):
            body["cookies"] = [{k: c.get(k) for k in ("name", "value", "domain", "path", "secure", "expires")} for c in material["cookies"]]
        state = await self.executor.call("POST", f"/sessions/{sid}", body)
        self.contexts[sid] = state["instance"]
        await self.absorb(sid, state, material)
        return state

    async def absorb(self, sid, state, material):
        """Persist callbacks captured by the executor before anything consumes them."""
        captured = state.get("captured") or []
        if captured and captured[-1] != material.get("captured_callback"):
            await asyncio.to_thread(self.persist, sid, {"captured_callback": captured[-1]})
        observation=state.get("sms_observation")
        if observation and observation != material.get("browser_sms_observation"):
            await asyncio.to_thread(self.persist,sid,{"browser_sms_observation":observation,"browser_sms_confirmed":bool(observation.get("confirmed"))})

    async def checkpoint(self, sid, _context=None):
        state = await self.executor.call("GET", f"/sessions/{sid}/storage-state")
        await asyncio.to_thread(self.persist, sid, {"browser_state": state})

    def release_unperformed_intent(self, sid, request, outcome):
        """An explicit no-action reply can release intent; lost replies cannot."""
        if outcome.get("action") is not None or outcome.get("stage") not in {
            "waiting_captcha", "waiting_official_page", "waiting_official_review",
            "verification_code_incorrect", "registration_confirmed",
        }:
            return
        with self.store.tx() as (s, state):
            row = s.get(Resource, sid)
            data = json.loads(row.data)
            if not data["status"].startswith("waiting_"):
                return
            material = json.loads(self.store.cipher.decrypt(row.secret.encode()))
            action = request["action"]
            if action == "send_sms":
                intent = material.get("browser_sms_intent") or {}
                if intent.get("operation") != request["operation"]:
                    return
                material.pop("browser_sms_intent", None)
            elif action == "submit_sms":
                expected = hashlib.sha256(request["code"].encode()).hexdigest()
                if material.get("browser_code_fingerprint") != expected:
                    return
                material.pop("browser_code_fingerprint", None)
            elif action == "prepare_phone":
                material.pop("browser_phone_prepared", None)
            elif action in {"continue", "enter_login"}:
                step = request.get("_step_key")
                material["browser_navigation_steps"] = [x for x in material.get("browser_navigation_steps", []) if x != step]
            else:
                return
            row.secret = self.store.cipher.encrypt(json.dumps(material).encode()).decode()
            row.version, row.updated_at = row.version + 1, self.store.clock()
            self.store.emit(s, state, "registration", "browser_action_not_performed", data.get("account_id"))

    def reject_sms(self, sid, token, observation):
        """Terminal official refusal is not missing SMS and never counts as no_sms."""
        with self.store.tx() as (s, state):
            self.service.assert_execution(s, sid, token)
            row = s.get(Resource, sid)
            data = json.loads(row.data)
            material = json.loads(self.store.cipher.decrypt(row.secret.encode()))
            if not data["status"].startswith("waiting_"):
                return False
            if any(material.get(k) for k in ("issued_result", "issued_credential", "issued_token", "captured_callback")):
                return False
            data.update(status="failed", error="sms_send_rejected", browser_stage="sms_send_rejected", browser_error="sms_send_rejected", browser_sms_confirmed=False,
                        browser_sms_http_status=observation.get("status"), result_unresolved=False, finished_at=self.store.clock(), browser_stage_at=self.store.clock())
            material.update(browser_stage="sms_send_rejected", browser_error="sms_send_rejected", browser_sms_observation=observation)
            row.data = json.dumps(data)
            row.secret = self.store.cipher.encrypt(json.dumps(material).encode()).decode()
            row.updated_at, row.version = self.store.clock(), row.version + 1
            self.store.emit(s, state, "registration", "sms_send_rejected", data.get("account_id"))
            return True

    async def close_session(self, sid):
        if self.contexts.pop(sid, None) is not None:
            try:
                await self.executor.call("DELETE", f"/sessions/{sid}")
            except DomainError:
                pass  # the executor reaps idle sessions on its own

    async def adopt(self):
        """After a main-program restart, take back sessions the executor still holds."""
        try:
            listing = await self.executor.call("GET", "/sessions")
        except DomainError:
            return
        for item in listing["sessions"]:
            try:
                data, _ = await asyncio.to_thread(self.service.details, item["sid"])
            except DomainError:
                data = None
            if data and data["status"].startswith("waiting_"):
                self.contexts[item["sid"]] = listing["instance"]
            else:
                try:
                    await self.executor.call("DELETE", f"/sessions/{item['sid']}")
                except DomainError:
                    pass

    async def reap(self):
        await self.adopt()
        while True:
            await self.executor.check()
            for sid in list(self.contexts):
                async with self.lock(sid):
                    try:
                        data, _ = await asyncio.to_thread(self.service.details, sid)
                        if not data["status"].startswith("waiting_") or (data.get("expires_at") or 0) <= self.store.clock():
                            await self.close_session(sid)
                    except DomainError:
                        await self.close_session(sid)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue
            await asyncio.sleep(30)

    async def watch_one(self, sid):
        """Continue authorized ordinary forms, then capture and verify credentials."""
        async with self.lock(sid):
            data, material = await asyncio.to_thread(self.service.details, sid)
            automatic = material.get("auto_run") and data["platform"] in {"WPS 灵犀", "小浣熊", "CodeArts"}
            if data["status"] == "waiting_authorization" and sid not in self.contexts and material.get("browser_state"):
                await self.context(sid)
            if automatic and data["status"] == "waiting_authorization":
                await self.context(sid)
                data, material = await asyncio.to_thread(self.service.details, sid)
                if material.get("codearts_registration_confirmed") and not material.get("codearts_oauth_opened"):
                    await self.executor.call("POST",f"/sessions/{sid}/goto",{"url":material["codearts_oauth_url"]})
                    await self.checkpoint(sid)
                    await asyncio.to_thread(self.persist,sid,{"codearts_oauth_opened":True})
                if material.get("phone") and material.get("sms_lease_id") and not material.get("codearts_registration_confirmed"):
                    await self.auto_step(sid, data, material)
                    data, material = await asyncio.to_thread(self.service.details, sid)
                    if data["status"] in {"failed", "cancelled", "succeeded"}:
                        await self.close_session(sid)
                        return
            if sid not in self.contexts or data["status"] != "waiting_authorization":
                return
            try:
                state = await self.executor.call("GET", f"/sessions/{sid}")
            except DomainError as error:
                if error.code == "browser_session_missing":
                    # Executor restarted or reaped it; the saved state reopens on next use.
                    self.contexts.pop(sid, None)
                return
            if state["instance"] != self.contexts[sid] or state["closed"]:
                self.contexts.pop(sid, None)
                return
            await self.absorb(sid, state, material)
            data, material = await asyncio.to_thread(self.service.details, sid)
            evidence = material.get("captured_callback")
            if data["platform"] == "WPS 灵犀":
                host = urlsplit(state.get("url") or "").hostname
                if host not in {"lingxi.wps.cn", "lingxi.kdocs.cn"}:
                    return
                cookies = (await self.executor.call("POST", f"/sessions/{sid}/cookies", {"urls": ["https://" + host + "/"]}))["cookies"]
                evidence = next((c["value"] for c in cookies if c["name"] == "wps_sid"), None)
            if not evidence:
                return
            fingerprint = hashlib.sha256(evidence.encode()).hexdigest()
            if material.get("browser_collected_fingerprint") == fingerprint:
                return
            # Checkpoint before consuming an OAuth code, including on crashes.
            storage = await self.executor.call("GET", f"/sessions/{sid}/storage-state")
            await asyncio.to_thread(self.persist, sid, {"browser_collected_fingerprint": fingerprint, "browser_state": storage})
            try:
                await self.collect(sid)
            except DomainError as error:
                if error.code in RETRYABLE:
                    await asyncio.to_thread(self.persist, sid, {"browser_collected_fingerprint": None})
                raise

    async def auto_step(self, sid, data, material):
        """One bounded form step per tick under the shared account execution lease."""
        async with self.service.execution(sid) as token:
            observation = material.get("browser_sms_observation") or {}
            http_status = observation.get("status")
            # Older persisted observations lack `rejected`; HTTP 4xx is still
            # definitive except a timeout, whose side effect remains uncertain.
            rejected = observation.get("rejected") or (isinstance(http_status, int) and 400 <= http_status < 500 and http_status != 408)
            if material.get("browser_sms_intent") and rejected:
                ended = await asyncio.to_thread(self.reject_sms, sid, token, observation)
                if ended:
                    try:
                        await self.service.sms.retire(material["sms_lease_id"], "sms_send_rejected")
                    except (DomainError, UpstreamError):
                        pass  # RegistrationRunner retries durable terminal leases.
                    # A generic HTTP/business refusal does not prove that the
                    # phone itself is unusable. Keep the session evidence but
                    # do not turn rate limits or challenges into a phone ban.
                return
            flags={"platform":data["platform"], "sms_attempted":bool(material.get("browser_sms_intent")), "code_attempted":bool(material.get("browser_code_fingerprint")),"registration_pending":bool(material.get("codearts_registration_pending")),"phone_prepared":bool(material.get("browser_phone_prepared"))}
            observed=await self.executor.call("POST", f"/sessions/{sid}/flow", flags)
            stage=observed.get("stage", "waiting_official_page")
            if stage == "waiting_code" and material.get("browser_sms_intent") and not material.get("browser_sms_confirmed"):
                await asyncio.to_thread(self.persist, sid, {"browser_stage":"sms_send_unconfirmed", "browser_error":"sms_send_unconfirmed", "browser_stage_at":self.store.clock(), "browser_sms_http_status":http_status})
                return
            if stage=="registration_confirmed" and data["platform"]=="CodeArts" and material.get("codearts_registration_pending"):
                link=material["codearts_oauth_url"]
                # Registration evidence never creates an account. OAuth token
                # exchange and caller-identity verification still must succeed.
                await asyncio.to_thread(self.persist,sid,{"codearts_registration_pending":False,"codearts_registration_confirmed":True,"authorization_url":link,"browser_stage":"waiting_codearts_oauth","browser_stage_at":self.store.clock()})
                await self.executor.call("POST",f"/sessions/{sid}/goto",{"url":link})
                await self.checkpoint(sid)
                await asyncio.to_thread(self.persist,sid,{"codearts_oauth_opened":True})
                return
            if stage=="verification_code_incorrect":
                await asyncio.to_thread(self.service.sms.reject_code,material["sms_lease_id"],
                                        fingerprint=material.get("browser_code_fingerprint"))
                await asyncio.to_thread(self.persist,sid,{"browser_code_fingerprint":None,"browser_stage":stage,"browser_stage_at":self.store.clock()})
                return
            request={**flags,"action":observed.get("action"),"operation":uid()}
            patch={"browser_stage":stage,"browser_error":None,"browser_stage_at":self.store.clock()}
            if request["action"] == "prepare_phone":
                request["phone"]=material["phone"]
                patch["browser_phone_prepared"]=True
            elif request["action"] == "send_sms":
                if material.get("browser_sms_intent"):
                    return
                request["phone"]=material["phone"]
                patch["browser_sms_intent"]={"operation":request["operation"],"at":self.store.clock()}
            elif request["action"] == "submit_sms":
                if not material.get("browser_sms_confirmed"):
                    await asyncio.to_thread(self.persist, sid, {**patch, "browser_stage":"sms_send_unconfirmed", "browser_error":"sms_send_unconfirmed", "browser_sms_http_status":http_status})
                    return
                code=await self.service.sms.poll(material["sms_lease_id"])
                if not code:
                    await asyncio.to_thread(self.persist,sid,patch)
                    return
                fingerprint=hashlib.sha256(code.encode()).hexdigest()
                if material.get("browser_code_fingerprint")==fingerprint:
                    return
                request["code"]=code
                patch["browser_code_fingerprint"]=fingerprint
            elif request["action"] in {"continue", "enter_login"}:
                step_key=hashlib.sha256(observed["step_key"].encode()).hexdigest()
                completed=material.get("browser_navigation_steps",[])
                if step_key in completed:
                    await asyncio.to_thread(self.persist,sid,{**patch,"browser_stage":"waiting_page_transition"})
                    return
                patch["browser_navigation_steps"]=[*completed,step_key]
            elif request["action"] == "agree_terms":
                # 协议确认层每次新会话都会重新弹，点了也不该记成「做过就不再做」。
                patch["browser_stage"]="agreement_accepting"
            else:
                await asyncio.to_thread(self.persist,sid,patch)
                return
            # Save intent before the browser action. Lost replies never trigger
            # a second SMS or another consumption of the same verification code.
            await asyncio.to_thread(self.persist,sid,patch)
            outcome=await self.executor.call("POST",f"/sessions/{sid}/flow",request)
            # The page can change between observe and execute. These replies
            # come before any action in the executor. A timeout/error never
            # reaches this path and retains the original durable intent.
            await asyncio.to_thread(self.release_unperformed_intent, sid,
                                    {**request, "_step_key":step_key} if request["action"] in {"continue", "enter_login"} else request,
                                    outcome)
            await self.checkpoint(sid)
            await asyncio.to_thread(self.persist,sid,{"browser_stage":outcome["stage"],"browser_stage_at":self.store.clock()})

    async def watch(self):
        async def one(sid):
            try:
                await self.watch_one(sid)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                try:
                    await asyncio.to_thread(self.persist,sid,{"browser_error":getattr(error,"code","browser_step_failed"),"browser_stage_at":self.store.clock()})
                except DomainError:
                    pass
        try:
            last_adopt = 0
            while True:
                if self.store.clock()-last_adopt >= 15:
                    await self.adopt()
                    last_adopt=self.store.clock()
                pending=await asyncio.to_thread(self.store.resources,"registration")
                automatic=[]
                for row in pending:
                    if row["data"]["status"]=="waiting_authorization":
                        _,material=await asyncio.to_thread(self.service.details,row["id"])
                        if material.get("browser_state") or (material.get("auto_run") and row["data"]["platform"] in {"WPS 灵犀","小浣熊","CodeArts"}):
                            automatic.append(row["id"])
                for sid in set(self.contexts) | set(automatic):
                    if sid in self.watch_tasks or self.lock(sid).locked():
                        continue
                    task = asyncio.create_task(one(sid))
                    self.watch_tasks[sid] = task
                    task.add_done_callback(lambda _, key=sid: self.watch_tasks.pop(key, None))
                await asyncio.sleep(3)
        finally:
            pending = list(self.watch_tasks.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def collect(self, sid):
        state = await self.context(sid)
        data, material = await asyncio.to_thread(self.service.details, sid)
        if data["platform"] == "WPS 灵犀":
            base = "https://lingxi.wps.cn" if urlsplit(state.get("url") or "").hostname == "lingxi.wps.cn" else "https://lingxi.kdocs.cn"
            cookies = (await self.executor.call("POST", f"/sessions/{sid}/cookies", {"urls": [base + "/"]}))["cookies"]
            if not any(c["name"] == "wps_sid" and c["value"] for c in cookies):
                raise DomainError("wps_sso_incomplete")
            credential = {"cookie": "; ".join(c["name"] + "=" + c["value"] for c in cookies), "base": base}
            await asyncio.to_thread(self.persist, sid, {"credential": credential, "issued_credential": credential})
            result = await self.service.run(sid, continuation=True)
        else:
            result = await self.service.run(sid, code=material.get("captured_callback"), continuation=True)
        if result["status"] == "succeeded":
            await self.close_session(sid)
        elif result["status"] == "waiting_authorization":
            _, saved = await asyncio.to_thread(self.service.details, sid)
            if saved.get("authorization_url") != material.get("authorization_url"):
                await self.executor.call("POST", f"/sessions/{sid}/goto", {"url": saved["authorization_url"]})
                await self.checkpoint(sid)
        return result

    async def close(self):
        # Sessions stay in the executor; a restarted main program adopts them.
        await self.executor.close()


def mount_browser(app, hub):
    @app.post("/api/registration/{sid}/browser/start")
    async def start(sid: str):
        async with hub.lock(sid):
            await hub.context(sid)
            await hub.checkpoint(sid)
            return {"ok": True, "width": 1280, "height": 900}

    @app.get("/api/registration/{sid}/browser/image")
    async def screenshot(sid: str):
        async with hub.lock(sid):
            await hub.context(sid)
            image = await hub.executor.call("GET", f"/sessions/{sid}/screenshot", raw=True)
            return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/registration/{sid}/browser/action")
    async def action(sid: str, body: BrowserAction):
        async with hub.lock(sid):
            await hub.context(sid)
            text = body.text.get_secret_value() if body.text else None
            if body.action == "phone":
                _, material = await asyncio.to_thread(hub.service.details, sid)
                if not material.get("phone"):
                    raise DomainError("missing_material", 422)
                request = {"action": "type", "text": material["phone"]}
            elif body.action == "sms":
                async with hub.service.execution(sid):
                    _, material = await asyncio.to_thread(hub.service.details, sid)
                    if not material.get("sms_lease_id"):
                        raise DomainError("sms_lease_not_active")
                    code = await hub.service.sms.poll(material["sms_lease_id"])
                if not code:
                    return {"ok": True, "waiting_code": True}
                request = {"action": "type", "text": code}
            else:
                request = {"action": body.action, "x": body.x, "y": body.y, "end_x": body.end_x, "end_y": body.end_y, "delta": body.delta, "text": text}
            await hub.executor.call("POST", f"/sessions/{sid}/action", request)
            await hub.checkpoint(sid)
            return {"ok": True}

    @app.post("/api/registration/{sid}/browser/collect")
    async def collect(sid: str):
        async with hub.lock(sid):
            return await hub.collect(sid)

    @app.delete("/api/registration/{sid}/browser")
    async def close(sid: str):
        async with hub.lock(sid):
            if sid in hub.contexts:
                data, _ = await asyncio.to_thread(hub.service.details, sid)
                if data["status"].startswith("waiting_"):
                    try:
                        await hub.checkpoint(sid)
                    except DomainError:
                        pass
            await hub.close_session(sid)
            return {"ok": True}
