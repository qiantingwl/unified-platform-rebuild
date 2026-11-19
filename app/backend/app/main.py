import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import re
from contextlib import asynccontextmanager
from uuid import uuid4
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .store import DomainError, Store
from .worker import MaintenanceWorker
from .adapters import Registry, ADAPTERS
from .gateway import Gateway, Collector, chunk
from .protocols import normalize, converted, StreamTranslator, sse
from .upstream import UpstreamError
from contextlib import aclosing
from .operations import Supervisor, mount_operations
from .auth import AdminAuth

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.3.0"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountInput(Body):
    platform: str = Field(min_length=1, max_length=40)
    external_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    label: str = Field(min_length=1, max_length=100)


class CredentialInput(Body):
    credential: SecretStr = Field(min_length=1, max_length=16384)
    expected_version: int = Field(ge=0)


class JobInput(Body):
    kind: str
    model: str | None = Field(default=None, min_length=1, max_length=100)


class KeyInput(Body):
    model: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    platform: str
    models: list[str] | None = Field(default=None,max_length=200)


class AccountSettingsInput(Body):
    enabled: bool | None = None
    concurrency: int | None = Field(default=None,ge=1,le=1024)
    priority: int | None = Field(default=None,ge=-100000,le=100000)
    balance_interval_seconds: int | None = Field(default=None,ge=60,le=2592000)
    reset_balance_interval: bool = False
    daily_checkin_enabled: bool | None = None
    daily_checkin_minute: int | None = Field(default=None,ge=0,le=1439)
    reset_daily_checkin_minute: bool = False


class KeyPolicyInput(Body):
    models: list[str] = Field(min_length=1,max_length=200)
    aliases: dict[str,str] = Field(default_factory=dict,max_length=200)
    expires_at: float | None = Field(default=None,ge=0,allow_inf_nan=False)
    max_concurrency: int = Field(default=0,ge=0,le=100000)
    rpm: int = Field(default=0,ge=0,le=1000000)
    daily_requests: int = Field(default=0,ge=0,le=1000000000)
    allowed_ips: list[str] = Field(default_factory=list,max_length=200)


class AdminCredentialsInput(Body):
    username: str = Field(min_length=1,max_length=200)
    password: SecretStr = Field(min_length=12,max_length=1024)


class SessionInput(Body):
    token: SecretStr | None = None
    username: str = Field(default="", max_length=200)
    password: SecretStr = Field(default=SecretStr(""), max_length=1024)


def classify_client(user_agent):
    if not user_agent: return None
    value=user_agent.lower()
    for name,markers in (("claude-code",("claude-code","claude_cli")),("codex",("codex",)),("cursor",("cursor",)),("roo-code",("roo-code",)),("cline",("cline",)),("cherry-studio",("cherrystudio","cherry-studio")),("openai-sdk",("openai-python","openai/node")),("anthropic-sdk",("anthropic-python","anthropic-typescript"))):
        if any(marker in value for marker in markers):return name
    return "other"


KEEPALIVE = object()
KEEPALIVE_SECONDS = 15
PING = 'event: ping\ndata: {"type": "ping"}\n\n'
COMMENT_KEEPALIVE = ": keepalive\n\n"


async def paced(frames, interval):
    """Yield KEEPALIVE whenever the stream is silent for `interval` seconds, without
    cancelling the pending upstream read (a cancelled read would break the stream)."""
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(frames))
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield KEEPALIVE
                continue
            task, pending = pending, None
            try:
                item = task.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def allowed_origins():
    """Extra console origins when TLS terminates at a reverse proxy (comma separated)."""
    return {o.strip().rstrip("/") for o in os.getenv("UNIFIED_PUBLIC_ORIGIN", "").split(",") if o.strip()}


ROLES = {"api", "tasks", "all"}


def create_app(store=None, start_worker=True, admin_token=None, registry=None, role=None):
    """api: console, management API and forwarding; it selects accounts itself and never needs
    the task service online. tasks: every scheduled/background job (registration, sign-in,
    balance, renewal, relogin, recovery checks, invitation readback, browser supervision).
    all: both in one process, for tests and single-box use."""
    role = role or os.getenv("UNIFIED_ROLE", "all")
    if role not in ROLES:
        raise RuntimeError("UNIFIED_ROLE must be api, tasks or all")
    admin_token = admin_token if admin_token is not None else os.getenv("UNIFIED_ADMIN_TOKEN", "")
    origins = allowed_origins()
    if store is None:
        # Service boot. Injected stores are the test harness and skip these rules.
        if role != "tasks" and len(admin_token) < 16:
            raise RuntimeError("UNIFIED_ADMIN_TOKEN is required and must be at least 16 characters")
        local = ROOT / ".local"
        local.mkdir(exist_ok=True)
        key = os.getenv("UNIFIED_ENCRYPTION_KEY")
        url = os.getenv("UNIFIED_DATABASE_URL", "sqlite:///" + str(local / "platform.db"))
        if key is None and not url.startswith("sqlite"):
            # A key file inside a replaceable container would orphan every stored credential.
            raise RuntimeError("UNIFIED_ENCRYPTION_KEY is required with an external database")
        if key is None:
            key_path = local / "credential.key"
            try:
                with key_path.open("xb") as f:
                    f.write(Fernet.generate_key())
            except FileExistsError:
                pass
            key = key_path.read_bytes()
        store = Store(url, key)
        if store.has_synthetic_accounts():
            raise RuntimeError("Database contains demo accounts from the old local sandbox; start the service on a fresh database")
    # Upstream execution is on by default; UNIFIED_ENABLE_UPSTREAM=0 is the maintenance stop switch.
    from .encrypted_session_cache import EncryptedSessionCache
    registry = registry or Registry(enabled=os.getenv("UNIFIED_ENABLE_UPSTREAM", "1") != "0", session_cache=EncryptedSessionCache(store))
    store.real_capabilities = {p: (cls.capabilities & {"chat", "balance", "checkin", "renew", "onboarding"}) | {"verify"} for p, cls in ADAPTERS.items()} if registry.enabled else {}
    for platform, cls in ADAPTERS.items():
        if registry.enabled and "invite_read" in cls.capabilities:
            store.real_capabilities[platform].add("invitation")
    if registry.enabled:
        store.real_capabilities.setdefault('WorkBuddy 国内',set()).add('benefits')
    gateway = Gateway(store, registry)
    worker = MaintenanceWorker(store, registry=registry, capacity=int(os.getenv("UNIFIED_MAINTENANCE_CAPACITY", "64")))
    if worker.capacity < 1 or worker.capacity > 1024:
        raise ValueError("Invalid maintenance execution capacity")
    auth = AdminAuth(store, admin_token, os.getenv("UNIFIED_ADMIN_USERNAME", ""), os.getenv("UNIFIED_ADMIN_PASSWORD", ""))
    started_at = time.time()

    supervisor = Supervisor(store, worker, gateway, ROOT)
    from .proxy_kernel import ProxyKernel
    kernel = ProxyKernel(store, enabled=os.getenv('UNIFIED_PROXY_KERNEL_ENABLED', '0') == '1')

    instance = uuid4().hex

    def status_record():
        runner, invitations, browser = app.state.registration_runner, app.state.invitations, app.state.registration_browser
        return {"instance": instance, "version": VERSION, "started_at": started_at,
                "worker": {"running": worker.running, "last_tick": worker.last_tick, "last_error": worker.last_error, "active": len(worker.tasks)},
                "supervisor": {"running": supervisor.running, "last_tick": supervisor.last_tick, "last_error": supervisor.last_error},
                "registration": {"last_tick": runner.last_tick, "last_error": runner.last_error, "active": len(runner.tasks)},
                "invitation": {"last_tick": invitations.last_tick, "last_error": invitations.last_error},
                "browser_executor": browser.executor.health, "browser_sessions": len(browser.contexts),
                "proxy_subscriptions": {"running":app.state.subscription_scheduler.running,
                    "last_tick":app.state.subscription_scheduler.last_tick,"last_error":app.state.subscription_scheduler.last_error},
                "proxy_targets": {"running":app.state.proxy_target_scheduler.running,
                    "last_tick":app.state.proxy_target_scheduler.last_tick,"last_error":app.state.proxy_target_scheduler.last_error}}

    async def heartbeat():
        while True:
            try:
                await asyncio.to_thread(store.service_heartbeat, "tasks", status_record())
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # the console then shows the task service as offline
            await asyncio.sleep(10)

    async def follow_settings():
        # The api role has no supervisor; forwarding limits still follow the shared settings.
        from .operations import settings
        while True:
            try:
                config = (await asyncio.to_thread(settings, store))["values"]
                gateway.startup, gateway.idle, gateway.max_attempts = config["startup_seconds"], config["idle_seconds"], config["max_attempts"]
                gateway.placeholder_enabled = config['placeholder_enabled']
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        if start_worker and role in {"tasks", "all"}:
            loops = [worker.run(), supervisor.run(), app.state.registration_runner.run(), app.state.registration_browser.reap(), app.state.registration_browser.watch(), app.state.invitations.run(), heartbeat(), app.state.subscription_scheduler.run(), app.state.proxy_target_scheduler.run()]
            tasks = [asyncio.create_task(loop) for loop in loops]
        elif start_worker:
            tasks = [asyncio.create_task(follow_settings())]
        if start_worker and role in {'api','all'} and kernel.enabled:
            tasks.append(asyncio.create_task(kernel.run()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.registration_browser.close()

    app = FastAPI(title="Unified account platform", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    from .snapshot_compression import SnapshotGZipMiddleware
    app.add_middleware(SnapshotGZipMiddleware)
    app.state.store, app.state.worker, app.state.gateway = store, worker, gateway
    app.state.role = role
    app.state.proxy_kernel = kernel

    @app.get('/api/proxy-kernel')
    def proxy_kernel_status():
        return kernel.status

    @app.exception_handler(DomainError)
    async def domain_error(_, error):
        return JSONResponse({"error": {"code": error.code}}, status_code=error.status)

    @app.exception_handler(UpstreamError)
    async def upstream_error(_, error):
        return JSONResponse({"error": {"code": error.code}}, status_code=error.status)

    # Validation errors must not echo submitted credential fields or request bodies.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_, error):
        return JSONResponse({"error": {"code": "invalid_input", "fields": [list(e["loc"]) for e in error.errors()]}}, status_code=422)

    @app.middleware("http")
    async def guard(request, call_next):
        if role == "tasks" and request.url.path != "/api/health":
            # The task service has no console or API surface; it only reports health.
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        host = request.url.hostname
        if not admin_token and host not in {"127.0.0.1", "localhost", "testserver"}:
            # Unreachable in a booted service (a token is mandatory); guards the test harness.
            return JSONResponse({"error": {"code": "admin_token_required"}}, status_code=403)
        if request.url.path.startswith("/api"):
            origin = request.headers.get("origin")
            # Cross-site pages must not drive the console with the admin cookie.
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/") and origin.rstrip("/") not in origins:
                return JSONResponse({"error": {"code": "origin_not_allowed"}}, status_code=403)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("x-console-client") != "unified-local":
                return JSONResponse({"error": {"code": "console_header_required"}}, status_code=403)
            public = request.url.path in {"/api/session", "/api/session/config", "/api/health"}
            if admin_token and not public:
                token = request.cookies.get("unified_session", "")
                if not await asyncio.to_thread(auth.valid, token):
                    return JSONResponse({"error": {"code": "login_required"}}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(("/api", "/v1")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        if role == "tasks":
            return {"status": "ok" if worker.running and supervisor.running else "starting", "role": role, "version": VERSION}
        return {"status": "ok", "role": role, "version": VERSION, "upstream_enabled": registry.enabled, "auth_required": bool(admin_token)}

    @app.post("/api/session")
    def login(body: SessionInput, request: Request):
        token = auth.login(body.token.get_secret_value() if body.token else "", request.client.host if request.client else "unknown", body.username, body.password.get_secret_value())
        response = JSONResponse({"ok": True})
        response.set_cookie("unified_session", token, httponly=True, samesite="strict", max_age=8*3600, secure=os.getenv("UNIFIED_SECURE_COOKIE", "1") != "0")
        return response

    @app.get('/api/session/config')
    def session_config():
        auth.refresh_identity()
        return {'mode': auth.mode}

    @app.put('/api/admin/credentials')
    def configure_admin(body:AdminCredentialsInput):
        auth.configure(body.username.strip(),body.password.get_secret_value())
        response=JSONResponse({'ok':True,'login_required':True})
        response.delete_cookie('unified_session')
        return response

    @app.delete("/api/session")
    def logout(request: Request):
        auth.logout(request.cookies.get("unified_session", ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie("unified_session")
        return response

    @app.get("/api/snapshot")
    def snapshot():
        from .console_snapshot import snapshot as console_snapshot
        result = console_snapshot(store, worker.capacity)
        result["adapter_catalog"] = registry.capabilities()
        tasks_status = store.service_status("tasks") if role == "api" else {**status_record(), "online": worker.running, "seen_at": store.clock()}
        worker_view = tasks_status.get("worker") or {}
        result["runtime"] = {"started_at": started_at, "role": role, "task_service": tasks_status, "worker_running": bool(tasks_status["online"] and worker_view.get("running")), "last_tick": worker_view.get("last_tick"), "last_error": worker_view.get("last_error"), "capacity": result["execution"]["capacity"], "active": result["execution"]["total_active"], "database": store.engine.dialect.name, "version": VERSION, "upstream_enabled": registry.enabled, "browser_executor": tasks_status.get("browser_executor") or {"status": "unknown"}}
        return result

    @app.get("/api/events")
    async def events(request: Request):
        async def generate():
            revision = -1
            while not await request.is_disconnected():
                try:
                    # Lock-free read off the event loop: one console tab must not
                    # queue on the write lock or stall streaming requests.
                    current = await asyncio.to_thread(store.revision)
                    if current != revision:
                        revision = current
                        yield f"id: {revision}\nevent: revision\ndata: {revision}\n\n"
                    else:
                        yield ": heartbeat\n\n"
                except Exception:
                    yield "event: unavailable\ndata: database_unavailable\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/accounts", status_code=201)
    def account_create(body: AccountInput):
        return {"id": store.create_account(body.platform, body.external_id, body.label)}

    @app.put("/api/accounts/{account_id}/credential")
    def credential_put(account_id: str, body: CredentialInput):
        return {"version": store.put_credential(account_id, body.credential.get_secret_value(), body.expected_version)}

    @app.post("/api/accounts/{account_id}/jobs", status_code=201)
    def job_create(account_id: str, body: JobInput):
        if body.kind not in {"checkin", "balance", "verify", "renew", "onboarding", "benefits"}:
            raise DomainError("adapter_unavailable", 501)
        return {"id": store.plan(account_id, body.kind, body.model)}

    @app.get("/api/accounts/{account_id}/onboarding")
    def onboarding_receipt(account_id: str):
        from sqlalchemy import select
        from .models import Resource, Job
        with store.read() as (s, _):
            account = store.account(s, account_id)
            name = hashlib.sha256(f"{account_id}:onboarding".encode()).hexdigest()
            row = s.scalar(select(Resource).where(Resource.kind == "operation_ledger", Resource.name == name))
            receipt = json.loads(row.data) if row else {}
            latest_job = s.scalar(select(Job).where(Job.account_id == account_id, Job.kind == "onboarding").order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
            return {"account_id": account_id, "supported": "onboarding" in store.real_capabilities.get(account.platform, set()),
                    "readback_supported": account.platform == "Loomy", "job_id": latest_job.id if latest_job else None,
                    "job_status": latest_job.status if latest_job else None,
                    "settled": receipt.get("settled"), "last_checked_at": receipt.get("last_checked_at"),
                    "tasks": [{k: task.get(k) for k in ("key", "title", "done", "status", "error", "points")} for task in receipt.get("tasks", []) if isinstance(task, dict)]}

    @app.post("/api/jobs/{job_id}/reconcile-onboarding")
    def reconcile_onboarding(job_id: str):
        store.reconcile_onboarding(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/reconcile-checkin")
    def reconcile_checkin(job_id:str):
        store.reconcile_checkin(job_id)
        return {"ok":True}

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str):
        store.retry(job_id)
        return {"ok": True}

    @app.post("/api/maintenance/plan-today")
    def plan():
        return {"scheduled": store.schedule_today()}

    @app.post("/api/keys", status_code=201)
    def key_create(body: KeyInput):
        return store.create_key(body.name, body.platform, body.model, body.models)

    @app.post("/api/accounts/{account_id}/recheck", status_code=201)
    def account_recheck(account_id: str):
        """把「上游停用」的账户放回核验：一次只读余额成功才重回候选池。"""
        return {"id": store.recheck(account_id)}

    @app.patch('/api/accounts/{account_id}/settings')
    def account_settings(account_id:str, body:AccountSettingsInput):
        with store.tx() as (session,state):
            account=store.account(session,account_id)
            for name,value in body.model_dump(exclude_none=True,exclude={'reset_balance_interval','reset_daily_checkin_minute'}).items():setattr(account,name,value)
            if body.reset_balance_interval: account.balance_interval_seconds=None
            if body.reset_daily_checkin_minute: account.daily_checkin_minute=None
            account.updated_at=store.clock()
            store.emit(session,state,'account','settings_updated',account.id)
        return {'ok':True}

    @app.get('/api/keys/{key_id}/policy')
    def key_policy(key_id:str):
        from .models import ApiKey
        from .key_policy import read
        with store.read() as (session,_):
            key=session.get(ApiKey,key_id)
            if not key:raise DomainError('key_not_found',404)
            return read(session,key)

    @app.put('/api/keys/{key_id}/policy')
    def key_policy_put(key_id:str,body:KeyPolicyInput):
        from sqlalchemy import select
        from .models import ApiKey,Resource
        from .key_policy import validate
        from .store import uid
        policy=body.model_dump();validate(policy)
        with store.tx() as (session,state):
            key=session.get(ApiKey,key_id)
            if not key:raise DomainError('key_not_found',404)
            row=session.scalar(select(Resource).where(Resource.kind=='key_policy',Resource.name==key_id))
            if row is None:
                row=Resource(id=uid(),kind='key_policy',name=key_id,data='{}',version=0,updated_at=store.clock());session.add(row)
            row.data=json.dumps(policy);row.version+=1;row.updated_at=store.clock()
            store.emit(session,state,'api_key','policy_updated')
        return policy

    @app.get('/api/accounts/{account_id}/wallet')
    def wallet(account_id:str):
        from sqlalchemy import select
        from .models import Observation,Job
        with store.read() as (session,_):
            account=store.account(session,account_id)
            rows=list(session.scalars(select(Observation).where(Observation.account_id==account_id).order_by(Observation.created_at.desc()).limit(100)))
            latest=session.scalar(select(Job).where(Job.account_id==account_id,Job.kind=='balance',Job.status.in_(['succeeded','failed'])).order_by(Job.finished_at.desc(),Job.finished_revision.desc().nulls_last(),Job.created_at.desc(),Job.id.desc()))
            observations=[{'amount':r.amount,'created_at':r.created_at,'job_id':r.job_id,'delta':r.amount-rows[i+1].amount if i+1<len(rows) else None} for i,r in enumerate(rows)]
            return {'account_id':account.id,'platform':account.platform,'balance':account.balance,'balance_at':account.balance_at,'balance_status':'query_failed' if latest and latest.status=='failed' else 'never_queried' if account.balance is None else 'credential_changed' if account.balance_version!=account.credential_version else 'stale' if store.clock()-(account.balance_at or 0)>3600 else 'verified','observations':observations,'note':'余额差值包含期间全部收入与消耗，不能作为单条请求费用或奖励到账证明。'}

    @app.delete("/api/keys/{key_id}")
    def key_revoke(key_id: str):
        store.revoke_key(key_id)
        return {"ok": True}

    async def forward(request, protocol):
        authorization = request.headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else request.headers.get("x-api-key", "")
        if not token:
            raise DomainError("invalid_api_key", 401)
        if request.headers.get('x-platform'):
            from sqlalchemy import select
            from .models import ApiKey
            def check_platform():
                with store.read() as (session,_):
                    key=session.scalar(select(ApiKey).where(ApiKey.digest==hashlib.sha256(token.encode()).hexdigest(),ApiKey.enabled.is_(True)))
                    if key is None: raise DomainError('invalid_api_key',401)
                    if request.headers['x-platform'] != key.platform: raise DomainError('platform_mismatch',422)
            await asyncio.to_thread(check_platform)
        chunks, size = [], 0
        async for part in request.stream():
            size += len(part)
            if size > int(os.getenv("UNIFIED_MAX_REQUEST_BYTES", str(32 * 1024 * 1024))):
                raise DomainError("request_too_large", 413)
            chunks.append(part)
        try:
            raw = json.loads(b"".join(chunks))
            history_context = None
            if protocol == 'responses':
                from .response_history import prepare
                raw, history_context = await asyncio.to_thread(prepare, store, token, raw, request.client.host if request.client else None)
            body = normalize(raw, protocol)
            if history_context:
                from .response_history import apply_history
                body = apply_history(body, history_context)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DomainError("invalid_input", 422) from None
        conversation = raw.get("conversation_id") or request.headers.get("x-conversation-id") or raw.get("prompt_cache_key")
        if history_context and history_context.conversation:
            conversation = history_context.conversation
        metadata=raw.get('metadata') if isinstance(raw.get('metadata'),dict) else {}
        conversation=conversation or metadata.get('conversation_id') or metadata.get('conversationId')
        if conversation is None and isinstance(metadata.get('user_id'),str) and re.fullmatch(r'user_.+_account_.+_session_[0-9a-fA-F-]{36}',metadata['user_id']):
            conversation='client-session:'+hashlib.sha256(metadata['user_id'].encode()).hexdigest()
        if conversation is not None and (not isinstance(conversation, str) or len(conversation) > 512):
            raise DomainError("invalid_conversation_id", 422)
        if conversation is None:
            prefix = [m for m in body["messages"] if m.get("role") in {"system", "developer"}]
            first_user = next((m for m in body["messages"] if m.get("role") == "user"), None)
            if first_user:
                prefix.append(first_user)
            # Prompt material never enters the DB. Explicit conversation IDs
            # remain preferable when identical initial prompts are separate chats.
            conversation = "derived:" + hashlib.sha256(json.dumps(prefix, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Admission owns bounded waiting and cleanup of a late database commit.
        current = await gateway.acquire(token, body["model"], conversation=conversation,
            client_ip=request.client.host if request.client else None,
            is_disconnected=request.is_disconnected,client_source=classify_client(request.headers.get("user-agent", "")))
        collector = Collector(current["id"], body["model"])
        if not raw.get("stream"):
            try:
                async with aclosing(gateway.frames(token, body, current, conversation)) as frames:
                    async for item in frames:
                        collector.add(item)
                result = collector.result()
                if history_context:
                    from .response_history import complete
                    return await asyncio.to_thread(complete, store, history_context, result, conversation)
                thinking_display = raw.get('thinking', {}).get('display') if isinstance(raw.get('thinking'), dict) else None
                return converted(result, protocol, thinking_display=thinking_display)
            except (DomainError, UpstreamError) as failure:
                if failure.code in {"invalid_tool_arguments", "invalid_stream_frame", "response_too_large", "unsupported_protocol_field",'response_context_too_large','response_history_unavailable','response_history_conflict'}:
                    await asyncio.to_thread(store.fail_delivery, current["id"], failure.code)
                raise

        async def generate():
            thinking_display = raw.get('thinking', {}).get('display') if isinstance(raw.get('thinking'), dict) else None
            translator = StreamTranslator(protocol, collector, thinking_display=thinking_display) if protocol != "chat" else None
            try:
                if translator:
                    for event in translator.start():
                        yield event
                async with aclosing(gateway.frames(token, body, current, conversation)) as upstream, aclosing(paced(upstream, KEEPALIVE_SECONDS)) as frames:
                    async for item in frames:
                        if item is KEEPALIVE:
                            # Byte-level liveness for proxies and clients; not content, not a frame.
                            yield PING if protocol == "messages" else COMMENT_KEEPALIVE
                            continue
                        if "error" in item:
                            yield sse({"error": item["error"]}) if not translator else translator.event("error", error=item["error"])
                            return
                        collector.add(item)
                        if translator:
                            for event in translator.feed(item):
                                yield event
                        else:
                            yield sse(chunk(current["id"], body["model"], item))
                final_response = None
                if history_context:
                    from .response_history import complete
                    final_response = await asyncio.to_thread(complete, store, history_context, collector.result(), conversation, response=translator.response_result())
                if translator:
                    for event in translator.finish(final_response=final_response):
                        yield event
                else:
                    yield "data: [DONE]\n\n"
            except (DomainError, UpstreamError) as failure:
                await asyncio.to_thread(store.fail_delivery, current["id"], failure.code)
                yield sse({"error": {"code": failure.code}}, "error" if translator else None)
            finally:
                # Also covers errors before the gateway generator starts.
                import anyio
                with anyio.CancelScope(shield=True):
                    await asyncio.to_thread(store.end_request, current["id"], "cancelled", "cancelled")
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await forward(request, "chat")

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await forward(request, "responses")

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await forward(request, "messages")

    @app.get("/v1/models")
    def models(request: Request):
        authorization = request.headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else request.headers.get("x-api-key", "")
        return {"object": "list", "data": store.key_models(token)}

    from .management import mount_management
    mount_management(app, store, registry)
    from .account_directory import mount_account_directory
    mount_account_directory(app, store)
    from .key_directory import mount_key_directory
    mount_key_directory(app, store)
    from .admin_queries import mount_admin_queries
    mount_admin_queries(app, store)
    from .response_history import mount_response_history
    mount_response_history(app, store)
    from .model_routes import mount_model_routes
    mount_model_routes(app, store)
    from .dispatch_policy import mount_dispatch_policies
    mount_dispatch_policies(app, store)
    from .imports import mount_imports
    mount_imports(app, store, registry)
    from .registration import mount_registration
    mount_registration(app, store, registry)
    from .registration_automation import mount_registration_automation
    mount_registration_automation(app, app.state.registration_runner)
    from .registration_preflight import mount_registration_preflight
    mount_registration_preflight(app, app.state.registration_service)
    mount_operations(app, store, supervisor)
    from .subscriptions import mount_subscriptions
    mount_subscriptions(app, store, registry)
    from .proxy_target_health import ProxyTargetHealth
    from .proxy_target_scheduler import register_routes as mount_proxy_targets
    mount_proxy_targets(app, store, ProxyTargetHealth(store, enabled=registry.enabled))

    dist = ROOT / "web" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def frontend():
        if (dist / "index.html").exists():
            return FileResponse(dist / "index.html")
        return JSONResponse({"status": "frontend_not_built", "command": "cd web && npm ci && npm run build"}, status_code=503)

    return app

