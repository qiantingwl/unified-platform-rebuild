"""Isolated browser executor service.

Runs in its own container with Chromium. It holds no database connection and no
encryption key: the main program owns tasks, accounts, credentials and retry
counts, and sends this service only what one browser session needs. Every call
is authenticated with a shared token, has a timeout, and sessions are reaped on
idle or deadline so a stuck page cannot hold resources forever.
"""
import asyncio
import hmac
import os
import time
import uuid
import re
import anyio
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

VIEWPORT = {"width": 1280, "height": 900}
CALLBACK_HOSTS = {("http", "127.0.0.1:4180", "/oauth/callback")}
CAPTURE_SCRIPT = """document.addEventListener('click',e=>{const a=e.target.closest?.('a');if(a?.href?.startsWith('office-raccoon:')){e.preventDefault();window.unifiedLoginCallback(a.href);}},true);const originalOpen=window.open;window.open=function(url,...rest){if(String(url).startsWith('office-raccoon:')){window.unifiedLoginCallback(String(url));return null;}return originalOpen.call(window,url,...rest);};"""
CALLBACK_PAGE = "<meta charset='utf-8'><p>已接收授权回调，请返回注册面板点击完成核验。</p>"
KEYS = {"Enter", "Tab", "Backspace", "Control+A", "Escape"}


class Fail(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class Cookie(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = True
    expires: float | None = None


class OpenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=8192)
    storage_state: dict | None = None
    cookies: list[Cookie] = Field(default_factory=list, max_length=500)
    proxy: str | None = Field(default=None, max_length=2048)
    capture_prefixes: list[str] = Field(default_factory=list, max_length=5)
    deadline: float | None = None


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    x: float = Field(default=0, ge=0, le=1280)
    y: float = Field(default=0, ge=0, le=900)
    end_x: float = Field(default=0, ge=0, le=1280)
    end_y: float = Field(default=0, ge=0, le=900)
    delta: int = Field(default=500, ge=-900, le=900)
    text: str | None = Field(default=None, max_length=4096)


class UrlsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[str] = Field(max_length=10)


class GotoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=8192)


class FlowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str
    action: str = "observe"
    phone: str | None = Field(default=None, max_length=30)
    code: str | None = Field(default=None, max_length=8)
    sms_attempted: bool = False
    code_attempted: bool = False
    operation: str | None = Field(default=None, max_length=100)
    registration_pending: bool = False
    phone_prepared: bool = False


FLOW_HOSTS = {"小浣熊": {"xiaohuanxiong.com", "account.sensetime.com"},
              "CodeArts": {"reg.huaweicloud.com", "auth.huaweicloud.com", "console.huaweicloud.com", "console.huaweicloud.com.hk", "codearts.huaweicloud.com"},
              "WPS 灵犀": {"account.wps.cn", "account.kdocs.cn", "lingxi.wps.cn", "lingxi.kdocs.cn", "www.kdocs.cn"}}


def sms_response_observation(status, value, host):
    """Store only protocol facts; never propagate upstream text or user values."""
    value = value if isinstance(value, dict) else {}
    code = value.get("code")
    ok_codes = {"0", "200", "200000", "OK", "SUCCESS", "ok", "success"}
    explicit_failure = value.get("success") is False or (code is not None and str(code) not in ok_codes)
    success = 200 <= status < 300 and not explicit_failure and (str(code) in ok_codes if code is not None else value.get("result") == "ok" or value.get("success") is True)
    if host == "reg.huaweicloud.com" and value:
        explicit_failure = bool(value.get("error_code")) or value.get("success") is False
        success = 200 <= status < 300 and not explicit_failure
    rejected = (400 <= status < 500 and status != 408) or (200 <= status < 300 and explicit_failure)
    category = "sms_send_rejected" if rejected else None if success else "sms_send_unconfirmed"
    return {"confirmed": success, "rejected": rejected, "status": status, "at": time.time(), "error": category}


async def visible_one(locators, enabled=False):
    for locator in locators:
        for i in range(min(await locator.count(), 8)):
            item = locator.nth(i)
            if await item.is_visible() and (not enabled or await item.is_enabled()):
                return item
    return None


#: 平台首次进入时弹出的「隐私政策/服务协议」确认层。它挡在表单前面，
#: 不点掉就会让后面的发码/登录全部失败（小熊直接回 HTTP 400 captcha_token
#: invalid，WPS 连手机号输入框都不出现）。这是普通页面控件，按用户操作点掉。
#: 只认「有拒绝按钮的同级同意按钮」，避免误点业务里的「同意并登录」。
_AGREEMENT_TEXT = re.compile(
    r"服务协议及隐私保护|继续登录前，请阅读并同意|隐私保护政策和.*服务协议|请务必充分阅读")
_AGREEMENT_DECLINE = re.compile(r"^(不同意|取消)$")


async def agreement_accept(frames):
    for frame in frames:
        marker = await visible_one([frame.get_by_text(_AGREEMENT_TEXT)])
        if marker is None:
            continue
        # 两种形态：radix 之类用真 button（小熊），WPS 用 div.dialog-footer-*
        decline = await visible_one([frame.get_by_role("button", name=_AGREEMENT_DECLINE)])
        accept = await visible_one([frame.get_by_role("button", name="同意", exact=True)])
        if accept is not None and decline is not None:
            return accept
        cancel = await visible_one([frame.locator(".dialog-footer-cancel")])
        ok = await visible_one([frame.locator(".dialog-footer-ok")])
        if ok is not None and cancel is not None:
            return ok
    return None


async def advance_login(item, body):
    """Drive ordinary official form controls. Never solve or bypass a challenge.

    Mutating calls carry a durable operation identity from the task service.
    A restarted executor receives only observe calls for already-attempted steps.
    """
    page = item.current()
    hosts = FLOW_HOSTS.get(body.platform)
    if not hosts or urlsplit(page.url).hostname not in hosts:
        return {"stage":"waiting_official_page", "action":None}
    frames = [f for f in page.frames if urlsplit(f.url).hostname in hosts]
    if not frames:
        frames = [page]
    phone = code = send = login = consent = continuation = entry = None
    agreement_input=agreement_control=None
    continuation_name = None
    for frame in frames:
        challenge = await visible_one([frame.locator('.geetest_panel, .geetest_box, [class*="captcha-popup"], iframe[src*="captcha"], iframe[src*="geetest"], [role="dialog"][aria-label*="验证"]'),frame.get_by_text(re.compile(r"拖动滑块|完成拼图|请完成安全验证|请点击.*验证"))])
        if challenge is not None:
            return {"stage":"waiting_captcha", "action":None}
        if body.platform=="CodeArts":
            risk=await visible_one([frame.get_by_text(re.compile(r"环境存在风险|访问过于频繁|账号数量已达到上限|该手机号注册的账号已达上限|请选择登录账号"))])
            if risk is not None:
                return {"stage":"waiting_official_review", "action":None}
            if body.registration_pending:
                confirmed=await visible_one([frame.get_by_text(re.compile(r"注册成功|账号已创建|欢迎使用华为云"))])
                if confirmed is not None or urlsplit(page.url).hostname in {"console.huaweicloud.com","console.huaweicloud.com.hk"}:
                    return {"stage":"registration_confirmed", "action":None}
        rejected = await visible_one([frame.get_by_text(re.compile(r"验证码错误|验证码不正确|验证码已过期"))])
        if rejected is not None and body.code_attempted:
            return {"stage":"verification_code_incorrect", "action":None}
        phone = phone or await visible_one([frame.locator('#phone-register-phone input, #login-identifier, input[type="tel"], input[autocomplete="tel"], input[placeholder*="手机号"], input[placeholder*="手机号码"]')], True)
        code = code or await visible_one([frame.locator('#phone-register-sms-code input, #login-code, input[autocomplete="one-time-code"], input[placeholder*="验证码"]')], True)
        # Form buttons commonly enable only after fill/consent. Locate them
        # while disabled; Playwright click still waits for actual enablement.
        send = send or await visible_one([frame.get_by_role("button",name=re.compile(r"^(发送验证码|获取验证码)$")), frame.get_by_text(re.compile(r"^(发送验证码|获取验证码)$"))])
        # Role selectors embed regexes in /.../; escape literal slash labels.
        login = login or await visible_one([frame.get_by_role("button",name=re.compile(r"^(登录|立即登录|登录\/注册|注册\/登录|登录或注册|登录 \/ 注册)$"))])
        if body.platform=="CodeArts" and body.registration_pending:
            send=send or await visible_one([frame.locator('#phone-register-send-sms')])
            login=await visible_one([frame.locator('#phone-register-submit')])
            native=frame.locator('.register-agreement input[type="checkbox"]')
            if await native.count():
                agreement_input=native.nth(0)
                agreement_control=await visible_one([frame.locator('.register-agreement .tiny-checkbox__inner')])
        # Login entry and post-login consent are separate from the SMS send button.
        entry_pattern=re.compile(r"^(手机号登录|短信登录|登录\/注册|立即登录|登录)$")
        entry = entry or await visible_one([frame.get_by_role("button",name=entry_pattern),frame.get_by_role("link",name=entry_pattern)], True)
        consent = consent or await visible_one([frame.get_by_role("checkbox")], True)
        if body.platform == "WPS 灵犀":
            # The WPS QR landing page uses a span entry and two checkboxes.
            # keepOnline is not the agreement; select its precise control.
            # 「手机」这个 span 的父级是 a.js_toProtocolDialog：点它会先弹
            # 隐私协议确认层，同意后才切出手机号表单，所以这里点 a 更可靠。
            entry = entry or await visible_one([frame.locator('a.js_toProtocolDialog').filter(has_text=re.compile(r'手机'))], True)
            entry = entry or await visible_one([frame.locator('span.f_icon_txt').filter(has_text=re.compile(r'^手机$'))], True)
            consent = await visible_one([frame.locator('#loginProtocal')], True)
            phone = phone or await visible_one([frame.locator('input#phone')], True)
            code = code or await visible_one([frame.locator('input#code')], True)
            login = login or await visible_one([frame.get_by_role('link', name='立即登录/注册', exact=True)])
        for name in ("确认登录", "同意并继续", "进入灵犀", "确认授权", "允许授权", "继续"):
            button = await visible_one([frame.get_by_role("button",name=name,exact=True),frame.get_by_text(name,exact=True)], True)
            if button is not None:
                continuation,continuation_name=button,name
                break
    prompt = await agreement_accept(frames)
    if body.action == "observe" and prompt is not None:
        return {"stage":"ready_to_agree", "action":"agree_terms",
                "step_key":page.url.split('?')[0]+":agreement"}
    if body.action == "observe":
        if phone is not None and send is not None and not body.sms_attempted:
            if body.platform=="CodeArts" and body.registration_pending and not body.phone_prepared:
                return {"stage":"ready_phone_check", "action":"prepare_phone"}
            return {"stage":"ready_to_send", "action":"send_sms"}
        if code is not None and body.sms_attempted and not body.code_attempted:
            return {"stage":"waiting_code", "action":"submit_sms"}
        if phone is None and continuation is not None:
            return {"stage":"ready_to_continue", "action":"continue", "step_key":page.url.split('?')[0]+":"+continuation_name}
        if phone is None and code is None and entry is not None and not body.sms_attempted:
            return {"stage":"ready_for_login", "action":"enter_login", "step_key":page.url.split('?')[0]+":login"}
        return {"stage":"waiting_authorization" if body.code_attempted else "waiting_code" if body.sms_attempted else "waiting_official_page", "action":None}
    if not body.operation:
        raise Fail("browser_operation_required",422)
    if not hasattr(item,"flow_operations"):
        item.flow_operations={}
    if body.operation in item.flow_operations:
        return item.flow_operations[body.operation]
    # Even a click timeout is ambiguous: don't re-click it using the same ID.
    item.flow_operations[body.operation]={"stage":"browser_step_uncertain", "action":None}
    async def agree():
        if agreement_input is not None:
            if not await agreement_input.is_checked():
                if agreement_control is None:raise Fail("browser_agreement_unavailable",422)
                await agreement_control.click(timeout=5000)
                if not await agreement_input.is_checked():raise Fail("browser_agreement_unconfirmed",422)
        elif consent is not None and not await consent.is_checked():
            await consent.check()
    if body.action in {"send_sms","prepare_phone"} and phone is not None and send is not None and body.phone:
        value=re.sub(r"[\s-]", "", body.phone)
        if re.fullmatch(r"\+861\d{10}",value):value=value[3:]
        if not re.fullmatch(r"1\d{10}",value):raise Fail("invalid_phone_number",422)
        if body.action=="prepare_phone" or not body.phone_prepared:
            await phone.fill(value)
        await agree()
        # 商汤小浣熊首次发码会弹「服务协议及隐私保护」确认层（不同意 / 同意）。
        # 不点「同意」直接请求 /sms/send 会拿到 HTTP 400，看起来像号码被拒，
        # 实际是协议未确认。这是普通页面控件，按正常用户操作点掉。
        dialog = await visible_one([f.get_by_text(re.compile(r"服务协议及隐私保护|继续登录前，请阅读并同意")) for f in frames])
        if dialog is not None:
            accept = await visible_one(
                [f.get_by_role("button", name="同意", exact=True) for f in frames]
                + [f.get_by_text("同意", exact=True) for f in frames], True)
            if accept is not None:
                await accept.click(timeout=5000)
                try:
                    await dialog.wait_for(state="hidden", timeout=5000)
                except Exception:
                    pass
        if body.action=="send_sms":
            await send.click(timeout=10000)
        result={"stage":"phone_check_pending" if body.action=="prepare_phone" else "sms_send_attempted", "action":None}
    elif body.action == "submit_sms" and code is not None and body.code and re.fullmatch(r"\d{4,8}",body.code):
        await code.fill(body.code)
        await agree()
        if login is None:raise Fail("browser_login_button_missing",422)
        await login.click(timeout=10000)
        result={"stage":"sms_verify_attempted", "action":None}
    elif body.action == "continue" and continuation is not None:
        await continuation.click(timeout=10000)
        result={"stage":"authorization_attempted", "action":None}
    elif body.action == "enter_login" and entry is not None:
        if body.platform == "WPS 灵犀":
            await agree()
        await entry.click(timeout=10000)
        result={"stage":"login_page_opened", "action":None}
    elif body.action == "agree_terms":
        if prompt is None:
            raise Fail("browser_form_changed",409)
        await prompt.click(timeout=8000)
        await asyncio.sleep(0.5)
        result={"stage":"agreement_accepted", "action":None}
    else:
        raise Fail("browser_form_changed",409)
    item.flow_operations[body.operation]=result
    return result


def https_url(value):
    parsed = urlsplit(value or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise Fail("invalid_authorization_url", 422)
    return value


class Session:
    def __init__(self, context, page, deadline):
        self.context, self.page = context, page
        self.lock = asyncio.Lock()
        self.captured = []
        self.opened_at = self.last_used = time.time()
        self.deadline = deadline
        self.sms_observation = None

    def current(self):
        if self.page.is_closed():
            pages = [p for p in self.context.pages if not p.is_closed()]
            if not pages:
                raise Fail("browser_page_closed")
            self.page = pages[-1]
        return self.page


class Executor:
    def __init__(self, max_sessions, idle_seconds, op_timeout):
        self.instance = str(uuid.uuid4())
        self.max_sessions, self.idle_seconds, self.op_timeout = max_sessions, idle_seconds, op_timeout
        self.playwright = self.browser = None
        self.sessions = {}
        self.opening = {}
        self.launch_lock = asyncio.Lock()
        self.restarts = 0
        self.last_error = None

    async def ensure_browser(self):
        async with self.launch_lock:
            if self.browser is not None and self.browser.is_connected():
                return self.browser
            # A crashed Chromium takes all contexts with it; the main program
            # reopens from its saved storage_state.
            self.sessions.clear()
            if self.browser is not None:
                self.restarts += 1
            try:
                if self.playwright is None:
                    from playwright.async_api import async_playwright
                    self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-first-run"])
            except Exception:
                self.last_error = "browser_launch_failed"
                raise Fail("browser_runtime_unavailable", 503) from None
            return self.browser

    def get(self, sid):
        item = self.sessions.get(sid)
        if item is None:
            raise Fail("browser_session_missing", 404)
        item.last_used = time.time()
        return item

    async def open(self, sid, body):
        if sid in self.sessions:
            return self.sessions[sid]
        if sid in self.opening:
            # A retry joins the original creation, without cancelling it when
            # that retry disconnects. Never open two contexts for one session.
            return await asyncio.shield(self.opening[sid])
        if len(self.sessions) + len(self.opening) >= self.max_sessions:
            raise Fail("browser_capacity_busy", 429)
        pending = asyncio.get_running_loop().create_future()
        pending.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        self.opening[sid] = pending
        try:
            result = await self._open(sid, body)
            pending.set_result(result)
            return result
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                pending.cancel()
            else:
                pending.set_exception(error)
            raise
        finally:
            self.opening.pop(sid, None)

    async def _open(self, sid, body):
        https_url(body.url)
        options = {"viewport": VIEWPORT, "locale": "zh-CN", "accept_downloads": False}
        if body.storage_state:
            options["storage_state"] = body.storage_state
        if body.proxy:
            try:
                p = urlsplit(body.proxy)
                if p.scheme not in {"http", "https", "socks5"} or not p.hostname or not p.port:
                    raise ValueError()
                host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
                options["proxy"] = {"server": f"{p.scheme}://{host}:{p.port}", **({"username": unquote(p.username), "password": unquote(p.password or "")} if p.username else {})}
            except ValueError:
                raise Fail("invalid_proxy_url", 422) from None
        browser = await self.ensure_browser()
        context = await browser.new_context(**options)
        session = Session(context, None, body.deadline)
        try:
            if not body.storage_state and body.cookies:
                now = time.time()
                cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure, "expires": c.expires or -1} for c in body.cookies if not c.expires or c.expires > now]
                if cookies:
                    await context.add_cookies(cookies)

            async def capture(_source, url):
                value = urlsplit(str(url))
                if value.scheme == "office-raccoon" or (value.hostname == "127.0.0.1" and value.path == "/oauth/callback"):
                    session.captured.append(str(url))

            await context.expose_binding("unifiedLoginCallback", capture)
            await context.add_init_script(CAPTURE_SCRIPT)

            async def route_callback(route):
                session.captured.append(route.request.url)
                await route.fulfill(status=200, content_type="text/html", body=CALLBACK_PAGE)

            await context.route("http://127.0.0.1:4180/oauth/callback*", route_callback)
            for prefix in body.capture_prefixes:
                target = urlsplit(prefix)
                if target.scheme != "https" or not target.hostname:
                    raise Fail("invalid_capture_prefix", 422)

                async def route_prefix(route, target=target):
                    incoming = urlsplit(route.request.url)
                    if (incoming.scheme, incoming.netloc, incoming.path) == (target.scheme, target.netloc, target.path):
                        await route_callback(route)
                    else:
                        await route.continue_()

                await context.route(prefix + "*", route_prefix)

            def select_page(page):
                session.page = page

            context.on("page", select_page)
            async def observe_response(response):
                parsed=urlsplit(response.url)
                if response.request.method != "POST" or parsed.hostname not in set().union(*FLOW_HOSTS.values()):
                    return
                if not (parsed.path.endswith("/sms/send") or parsed.path == "/p/sms" or parsed.hostname=="reg.huaweicloud.com" and parsed.path.endswith("/util/send_verify_code")):
                    return
                try:
                    raw=await response.body()
                    if len(raw)>128*1024:
                        session.sms_observation=sms_response_observation(response.status,None,parsed.hostname)
                        return
                    import json
                    value=json.loads(raw)
                    session.sms_observation=sms_response_observation(response.status,value,parsed.hostname)
                except Exception:
                    session.sms_observation=sms_response_observation(response.status,None,parsed.hostname)
            context.on("response", observe_response)
            session.page = await context.new_page()
            await session.page.goto(body.url, wait_until="domcontentloaded", timeout=45000)
            self.sessions[sid] = session
            return session
        except BaseException as error:
            self.sessions.pop(sid, None)
            # Timeouts and HTTP disconnects raise cancellation, not Exception.
            # Cleanup must complete even under the server's cancelled scope.
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.wait_for(context.close(), 15)
                except Exception:
                    self.last_error = "context_close_failed"
            if isinstance(error, (Fail, asyncio.CancelledError)) or not isinstance(error, Exception):
                raise
            raise Fail("browser_navigation_failed", 502) from None

    async def close(self, sid):
        item = self.sessions.pop(sid, None)
        if item:
            try:
                await asyncio.wait_for(item.context.close(), 15)
            except Exception:
                self.last_error = "context_close_failed"

    async def reap(self):
        while True:
            await asyncio.sleep(15)
            now = time.time()
            for sid, item in list(self.sessions.items()):
                if item.lock.locked():
                    continue
                if now - item.last_used > self.idle_seconds or (item.deadline and item.deadline <= now):
                    await self.close(sid)
            if self.browser is not None and not self.browser.is_connected():
                self.sessions.clear()

    async def shutdown(self):
        for sid in list(self.sessions):
            await self.close(sid)
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            await self.playwright.stop()


def create_app(token=None, executor=None):
    token = token if token is not None else os.getenv("BROWSER_EXECUTOR_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("BROWSER_EXECUTOR_TOKEN is required and must be at least 16 characters")
    ex = executor or Executor(int(os.getenv("BROWSER_MAX_SESSIONS", "8")), int(os.getenv("BROWSER_IDLE_SECONDS", "900")), int(os.getenv("BROWSER_OP_TIMEOUT", "60")))

    @asynccontextmanager
    async def lifespan(_):
        reaper = asyncio.create_task(ex.reap())
        try:
            yield
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
            await ex.shutdown()

    app = FastAPI(title="browser executor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.executor = ex

    @app.exception_handler(Fail)
    async def failed(_, error):
        return JSONResponse({"error": {"code": error.code}}, status_code=error.status)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            return JSONResponse({"error": {"code": "executor_unauthorized"}}, status_code=401)
        try:
            # One stuck page operation must not hold a request (or the caller) forever.
            return await asyncio.wait_for(call_next(request), ex.op_timeout + 30)
        except TimeoutError:
            return JSONResponse({"error": {"code": "browser_operation_timeout"}}, status_code=504)

    async def locked(sid, work):
        item = ex.get(sid)
        async with item.lock:
            try:
                return await asyncio.wait_for(work(item), ex.op_timeout)
            except TimeoutError:
                raise Fail("browser_operation_timeout", 504) from None
            except Fail:
                raise
            except Exception:
                if ex.browser is None or not ex.browser.is_connected():
                    ex.sessions.clear()
                    raise Fail("browser_crashed", 503) from None
                raise Fail("browser_operation_failed", 502) from None

    def describe(sid, item):
        closed = item.page is None or item.page.is_closed()
        return {"sid": sid, "url": None if closed else item.page.url, "closed": closed, "captured": list(item.captured), "opened_at": item.opened_at, "last_used": item.last_used,"sms_observation":getattr(item,"sms_observation",None)}

    @app.get("/health")
    def health():
        connected = ex.browser is not None and ex.browser.is_connected()
        return {"instance": ex.instance, "sessions": len(ex.sessions), "max_sessions": ex.max_sessions, "browser_connected": connected, "restarts": ex.restarts, "last_error": ex.last_error}

    @app.get("/sessions")
    def sessions():
        return {"instance": ex.instance, "sessions": [describe(sid, item) for sid, item in ex.sessions.items()]}

    @app.post("/sessions/{sid}")
    async def open_session(sid: str, body: OpenInput):
        item = await asyncio.wait_for(ex.open(sid, body), ex.op_timeout + 15)
        return {"instance": ex.instance, **describe(sid, item)}

    @app.get("/sessions/{sid}")
    def state(sid: str):
        return {"instance": ex.instance, **describe(sid, ex.get(sid))}

    @app.get("/sessions/{sid}/screenshot")
    async def screenshot(sid: str):
        image = await locked(sid, lambda item: item.current().screenshot(type="jpeg", quality=75))
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/sessions/{sid}/action")
    async def action(sid: str, body: ActionInput):
        async def run(item):
            page = item.current()
            if body.action == "click":
                await page.mouse.click(body.x, body.y)
            elif body.action == "drag":
                await page.mouse.move(body.x, body.y)
                await page.mouse.down()
                try:
                    await page.mouse.move(body.end_x, body.end_y, steps=20)
                finally:
                    await page.mouse.up()
            elif body.action == "type" and body.text:
                await page.keyboard.insert_text(body.text)
            elif body.action == "key" and body.text in KEYS:
                await page.keyboard.press(body.text)
            elif body.action == "scroll":
                await page.mouse.wheel(0, body.delta)
            else:
                raise Fail("invalid_browser_action", 422)
            return {"ok": True}
        return await locked(sid, run)

    @app.post("/sessions/{sid}/flow")
    async def flow(sid: str, body: FlowInput):
        return await locked(sid, lambda item: advance_login(item, body))

    @app.get("/sessions/{sid}/storage-state")
    async def storage_state(sid: str):
        return await locked(sid, lambda item: item.context.storage_state())

    @app.post("/sessions/{sid}/cookies")
    async def cookies(sid: str, body: UrlsInput):
        for url in body.urls:
            https_url(url)
        result = await locked(sid, lambda item: item.context.cookies(body.urls))
        return {"cookies": result}

    @app.post("/sessions/{sid}/goto")
    async def goto(sid: str, body: GotoInput):
        https_url(body.url)
        await locked(sid, lambda item: item.current().goto(body.url, wait_until="domcontentloaded", timeout=45000))
        return {"ok": True}

    @app.delete("/sessions/{sid}")
    async def close(sid: str):
        await ex.close(sid)
        return {"ok": True}

    return app
