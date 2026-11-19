"""7863 OneID → Keycloak → Console → CLI contract, with durable checkpoints.

Reference: references/workbuddy7863/smslogin.go. Each login owns its cookie jar.
Captcha requires a real human/provider result; no fabricated ticket or bypass.
"""
import asyncio
import html
import json
import re
import time
from http.cookiejar import Cookie
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx

from .adapters import encode, required, workbuddy_identity
from .upstream import UpstreamError, classify, payload


class WorkBuddySMS:
    console = "https://www.codebuddy.cn"
    cli = "https://copilot.tencent.com"
    oneid = "https://oauth2.account.tencent.com"
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"

    def __init__(self, transport):
        self.t = transport
        self.material = {}

    def restore(self, material):
        self.material = dict(material)
        self.t.client.cookies.clear()
        for c in material.get("cookies", []):
            self.t.client.cookies.jar.set_cookie(Cookie(version=0, name=c["name"], value=c["value"], port=None, port_specified=False, domain=c["domain"], domain_specified=c["domain_specified"], domain_initial_dot=c["domain"].startswith("."), path=c["path"], path_specified=True, secure=c["secure"], expires=c.get("expires"), discard=c.get("expires") is None, comment=None, comment_url=None, rest={}))

    async def save(self, **values):
        self.material.update(values)
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "domain_specified": c.domain_specified, "path": c.path, "secure": c.secure, "expires": c.expires} for c in self.t.client.cookies.jar]
        self.material["cookies"] = cookies
        await self.checkpoint({**values, "cookies": cookies})

    def check_url(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in {urlsplit(x).netloc for x in (self.console, self.cli, self.oneid)} or parsed.username:
            raise UpstreamError("unexpected_redirect", 502)

    def repair_keycloak_cookies(self, response):
        """Port 7863's quoted Path repair without expanding cookie domains.

        httpx extracts Set-Cookie before returning from send(). Its stdlib jar
        retains a quoted Path as %22/.../%22, so the next broker navigation
        silently loses AUTH_SESSION_ID. Reparse only this trusted realm's
        response cookies after normalization, retaining their original scope.
        """
        parsed = urlsplit(str(response.url))
        if parsed.hostname != urlsplit(self.console).hostname:
            return
        realm = re.match(r"^(/auth/realms/[^/]+)(?:/|$)", parsed.path)
        if not realm:
            return
        original = response.headers.get_list("set-cookie")
        normalized, changed_names = [], set()
        for line in original:
            fixed = re.sub(r';\s*Path\s*=\s*"([^";]*)"', r'; Path=\1', line, flags=re.I)
            fixed = re.sub(r';\s*Version\s*=\s*[^;]*', '', fixed, flags=re.I)
            if not re.search(r';\s*Path\s*=', fixed, flags=re.I):
                fixed += '; Path=' + realm.group(1)
            normalized.append(fixed)
            if fixed != line:
                changed_names.add(line.split('=', 1)[0].strip())
        if not changed_names:
            return
        # Remove only cookies parsed from this response which will be
        # reinserted below. Existing unrelated realm/domain cookies survive.
        observed = httpx.Cookies()
        observed.extract_cookies(response)
        for cookie in observed.jar:
            if cookie.name in changed_names:
                self.t.client.cookies.delete(cookie.name, domain=cookie.domain, path=cookie.path)
        response.headers = httpx.Headers(
            [(k, v) for k, v in response.headers.multi_items() if k.lower() != 'set-cookie']
            + [('set-cookie', line) for line in normalized]
        )
        self.t.client.cookies.extract_cookies(response)

    async def raw(self, method, url, headers=None, body=None, no_cookie=False):
        self.check_url(url)
        headers = {"User-Agent": self.ua, **(headers or {})}
        try:
            async with asyncio.timeout(25):
                request = self.t.client.build_request(method, url, headers=headers, content=body)
                if no_cookie:
                    request.headers.pop("cookie", None)
                response = await self.t.client.send(request, stream=True, follow_redirects=False)
                try:
                    self.repair_keycloak_cookies(response)
                    raw = await self.t._read(response, 1024 * 1024)
                    return response.status_code, response.headers, raw
                finally:
                    await response.aclose()
        except (TimeoutError, httpx.TimeoutException):
            raise UpstreamError("upstream_timeout", 504) from None
        except httpx.HTTPError:
            raise UpstreamError("network", 502) from None

    async def json(self, method, url, headers=None, body=None, oneid=False, no_cookie=False):
        # Match 7863 commonJSON/browserHeaders. Navigation still uses raw()
        # without these XHR headers; Keycloak navigation must not inherit them.
        status, _, raw = await self.raw(method, url, {"Accept": "application/json, text/plain, */*", "Origin": self.console, "Referer": self.console + "/", "Content-Type": "application/json", **(headers or {})}, encode(body) if body is not None else None, no_cookie)
        try:
            data = json.loads(raw)
        except ValueError:
            raise UpstreamError("invalid_response") from None
        if not isinstance(data, dict):
            raise UpstreamError("invalid_response")
        error = data.get("errCode") if oneid else None
        if error:
            raise UpstreamError({"E0010028": "verification_code_incorrect", "E0010022": "rate_limit"}.get(str(error), "login_rejected"), 409)
        if status >= 400:
            raise classify(status, data.get("code"))
        if oneid:
            return {**data, **(data.get("data") or {})}
        return payload(data)

    async def start(self, material, mode):
        self.restore(material)
        phone = re.sub(r"[\s-]", "", required(material, "phone"))
        if re.fullmatch(r"1\d{10}", phone):
            phone = "+86" + phone
        if not re.fullmatch(r"\+861\d{10}", phone):
            raise UpstreamError("invalid_phone_number", 422)
        data = await self.json("POST", self.cli + "/v2/plugin/auth/state?platform=CLI", {"User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2"}, {})
        await self.save(cli_state=required(data, "state"), authorization_url=required(data,"authUrl"), phone=phone, step="sms_send_pending")
        return await self.send_code()

    async def send_code(self, captcha=None):
        # OneID's wire format has a separator after the country code. Keep
        # the stored number canonical for identity/ledger matching.
        phone = re.sub(r"[\s-]", "", self.material["phone"])
        if not re.fullmatch(r"\+861\d{10}", phone):
            raise UpstreamError("invalid_phone_number", 422)
        body = {"client_code": "codebuddy", "mobile": "+86 " + phone[3:], "scopes": ["openid", "mobile", "profile"]}
        if captcha:
            body["captchaVerification"] = captcha
        data = await self.json("POST", self.oneid + "/v1/auth/sms/code/send", body=body, oneid=True)
        if data.get("status") == "need_captcha":
            options = data.get("captcha") or []
            await self.save(step="captcha_required", captcha_options=options)
            return {"waiting": "waiting_captcha", "captcha_options": options}
        token = required(data, "state_token")
        await self.save(oneid_token=token, step="sms_sent")
        return {"challenge": token, "expires_in": min(int(data.get("expires_in") or 300), 600)}

    async def navigation(self, url, referer=None):
        for _ in range(10):
            status, headers, body = await self.raw("GET", url, {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", **({"Referer": referer} if referer else {})})
            if status not in {301, 302, 303, 307, 308}:
                return status, body
            location = headers.get("location")
            if not location:
                raise UpstreamError("invalid_response")
            next_url = urljoin(url, location)
            self.check_url(next_url)
            referer = None if headers.get("referrer-policy") == "no-referrer" else url
            url = next_url
        raise UpstreamError("redirect_limit")

    async def verify(self, material, mode, code):
        self.restore(material)
        if material.get("official_handoff"):
            from .login_connectors import WorkBuddyLogin
            official=WorkBuddyLogin(self.t,"WorkBuddy 国内")
            official.host=self.cli
            official.checkpoint=self.checkpoint
            return await official.verify({**material,"state":material["cli_state"]},"official",code)
        if material.get("step") == "captcha_required":
            try:
                captcha = json.loads(code or "{}")
            except ValueError:
                raise UpstreamError("invalid_captcha_result", 422) from None
            if not isinstance(captcha, dict) or not captcha.get("ticket") or not captcha.get("randStr"):
                raise UpstreamError("invalid_captcha_result", 422)
            return await self.send_code(captcha)
        if not self.material.get("sms_verified"):
            # Persist intent before consuming the code; a crash cannot resend it.
            await self.save(step="sms_verify_pending")
            data = await self.json("POST", self.oneid + "/v1/auth/sms/code/verify", body={"state_token": required(material, "oneid_token"), "code": code}, oneid=True)
            await self.save(sms_verified=True, oneid_token=required(data, "state_token"), step="sms_verified")
        if not self.material.get("broker_complete"):
            accounts = await self.json("GET", self.oneid + "/v1/auth/accounts?" + urlencode({"state_token": self.material["oneid_token"]}), oneid=True)
            if len(accounts.get("accounts") or []) > 1:
                await self.save(official_handoff=True,step="official_account_selection")
                return {"waiting":"waiting_authorization","authorization_url":required(self.material,"authorization_url"),"state":self.material["cli_state"],"official_handoff":True}
            oneid_code = required(accounts, "code")
            params = {"client_id": "console", "response_type": "code", "redirect_uri": self.console + "/login/?" + urlencode({"platform": "CLI", "state": self.material["cli_state"]}), "skip_redirect_confirm": "true", "platform": "CLI"}
            status, _, page = await self.raw("GET", self.console + "/auth/realms/copilot/protocol/openid-connect/auth?" + urlencode(params))
            if status != 200:
                raise UpstreamError("unexpected_sso_session")
            found = re.search(r'href="([^"]*broker/oneid/login[^"]*)"', html.unescape(page.decode("utf-8", "replace")))
            if not found:
                raise UpstreamError("broker_link_missing")
            login_url = urljoin(self.console, found[1])
            data = await self.json("GET", login_url + ("&" if "?" in login_url else "?") + "from_oneid_login=true", {"Origin": self.console, "Referer": self.console + "/"})
            callback = required(data, "redirect_uri")
            await self.navigation(callback + ("&" if "?" in callback else "?") + urlencode({"code": oneid_code, "state": required(data, "state")}))
            if not any(c.name.startswith("KEYCLOAK_IDENTITY") and c.domain.lstrip(".") in {"codebuddy.cn", "www.codebuddy.cn"} for c in self.t.client.cookies.jar):
                raise UpstreamError("sso_session_missing")
            await self.save(broker_complete=True, step="broker_complete")
        if not self.material.get("ticket_written"):
            status, _ = await self.navigation(self.console + "/console/accounts", self.console + "/")
            if status >= 400:
                raise classify(status)
            common = {"X-Domain": "www.codebuddy.cn", "X-Product-Code": "CLI", "Origin": self.console, "Referer": self.console + "/login/?" + urlencode({"platform": "CLI", "state": self.material["cli_state"]})}
            await self.json("POST", self.console + "/console/login/enterprise?" + urlencode({"state": self.material["cli_state"]}), common)
            url = self.console + "/console/auth/login?" + urlencode({"platform": "CLI", "state": self.material["cli_state"], "domain": "www.codebuddy.cn"})
            status, headers, raw = await self.raw("GET", url, {"X-Domain": "www.codebuddy.cn", "Referer": common["Referer"]})
            location = urlsplit(headers.get("location", ""))
            written = status in {302, 303} and location.path.rstrip("/") == "/login" and "force_login_type" in parse_qs(location.query, keep_blank_values=True)
            if status == 200:
                try:
                    written = json.loads(raw).get("code") == 0
                except (ValueError, AttributeError):
                    pass
            if not written:
                raise UpstreamError("ticket_not_written")
            await self.save(ticket_written=True, step="ticket_written")
        token = self.material.get("issued_token")
        if token is None:
            status, _, raw = await self.raw("GET", self.cli + "/v2/plugin/auth/token?" + urlencode({"state": self.material["cli_state"]}), {"User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2", "X-No-Authorization": "true"}, no_cookie=True)
            if status >= 400:
                raise classify(status)
            try:
                value = json.loads(raw)
            except ValueError:
                raise UpstreamError("invalid_response") from None
            if str(value.get("code")) == "11217":
                polls = self.material.get("ticket_pending_polls", 0) + 1
                rewrite = polls >= 3 and self.material.get("ticket_rewrites", 0) < 1
                await self.save(ticket_pending_polls=polls,
                                ticket_written=not rewrite,
                                ticket_rewrites=1 if rewrite else self.material.get("ticket_rewrites", 0),
                                step="ticket_rewrite_pending" if rewrite else "ticket_pending")
                return {"waiting": "waiting_ticket"}
            token = payload(value)
            required(token, "accessToken")
            await self.save(issued_token=token, step="token_received")
        account = await self.json("GET", self.cli + "/v2/plugin/login/account?" + urlencode({"state": self.material["cli_state"]}), {"Authorization": "Bearer " + token["accessToken"], "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2"}, no_cookie=True)
        account = account.get("account", account)
        identity = required(account, "uid", "userId", "id")
        auth = {**token, "domain": token.get("domain") or "www.codebuddy.cn"}
        if token.get("expiresIn"):
            auth["expiresAt"] = int(time.time() * 1000) + int(token["expiresIn"]) * 1000
        account = {**account, "uid": identity, "phoneNumber": self.material["phone"]}
        return {"identity": workbuddy_identity(account), "credential": {"auth": auth, "account": account}}
