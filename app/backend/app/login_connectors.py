"""Interactive login contracts. No background browser/SMS side effects on import."""
import base64
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from .adapters import CodeArts, Raccoon, WPS, encode, required, workbuddy_identity
from .upstream import UpstreamError, payload
from .vendor.mirai.marvis import endpoints as marvis
from .vendor.mirai.codearts import endpoints as codearts
from .vendor.mirai.wps_lingxi import endpoints as wps


SUPPORTED = {"Loomy": ["sms", "password", "register"], "WorkBuddy 国内": ["sms", "official"], "WorkBuddy 国际": ["official"], "Marvis": ["oauth_qq", "oauth_wechat"], "WPS 灵犀": ["browser", "verified_import"], "小浣熊": ["official", "verified_import"], "CodeArts": ["official", "register", "verified_import"]}


def official_profile(value):
    """Retain only actual upstream name fields, inside encrypted credentials."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in ("nickname", "nickName", "displayName", "display_name", "name", "username")
            if isinstance(value.get(key), str) and value[key].strip()}


async def raccoon_identity(transport, credential):
    adapter = Raccoon(transport, credential, "registration")
    url = adapter.base + "/api/web/auth/v1/user_info"
    value = await transport.json("GET", url, adapter.headers())
    if str(value.get("code")) == "200901":
        payload(await transport.json("POST", adapter.base + "/api/web/auth/v1/iam/confirm", {**adapter.headers(), "Content-Type":"application/json"}, b"{}"))
        value = await transport.json("GET", url, adapter.headers())
    profile = payload(value)
    return required(profile, "id"), profile


class RaccoonLogin:
    def __init__(self, transport):
        self.t = transport

    async def start(self, material, mode):
        state = secrets.token_urlsafe(32)
        return {"waiting":"waiting_authorization", "state":state, "authorization_url":"https://xiaohuanxiong.com/authorize?" + urlencode({"login_source":"desktop", "appname":"办公小浣熊客户端", "state":state})}

    async def verify(self, material, mode, code):
        credential = material.get("issued_credential")
        if credential is None:
            url = urlsplit(code or "")
            query = parse_qs(url.query)
            if (url.scheme,url.netloc.lower(),url.path) != ("office-raccoon","auth","/callback") or len(query.get("code",[])) != 1:
                raise UpstreamError("invalid_oauth_callback",422)
            if query.get("state") != [material["state"]]:
                raise UpstreamError("oauth_state_mismatch",422)
            data = payload(await self.t.json("POST","https://xiaohuanxiong.com/api/web/auth/v1/login_with_authorization_code",{"Content-Type":"application/json"},encode({"authorization_code":query["code"][0]})))
            credential = {"access_token":required(data,"access_token"),"refresh_token":data.get("refresh_token"),"officeIdentity":data.get("office_identity") or "personal"}
            await self.checkpoint({"issued_credential":credential})
        user_id, profile = await raccoon_identity(self.t, credential)
        return {"identity":user_id,"credential":{**credential,"userId":user_id,"profile":official_profile(profile),"officeIdentity":profile.get("officeIdentity") or profile.get("office_identity") or credential["officeIdentity"]}}


class WorkBuddyLogin:
    def __init__(self, transport, platform):
        self.t = transport
        self.host = "https://www.workbuddy.cn" if platform == "WorkBuddy 国内" else "https://www.workbuddy.ai"
        self.platform = "workbuddy" if platform == "WorkBuddy 国内" else "workbuddy-ai"

    async def start(self, material, mode):
        data = payload(await self.t.json("POST", self.host + "/v2/plugin/auth/state?" + urlencode({"platform": self.platform}), {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "X-No-Authorization": "true"}, b"{}"))
        state, link = required(data, "state"), required(data, "authUrl")
        parsed = urlsplit(link)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise UpstreamError("invalid_authorization_url")
        return {"waiting": "waiting_authorization", "state": state, "authorization_url": link}

    async def verify(self, material, mode, code):
        token = material.get("issued_token")
        if not token:
            # Each operation owns a fresh no-cookie transport. Do not turn a
            # network failure or arbitrary nonzero code into 'still waiting'.
            self.t.client.cookies.clear()
            value = await self.t.json("GET", self.host + "/v2/plugin/auth/token?" + urlencode({"state": required(material, "state")}), {"X-No-Authorization": "true", "Accept": "application/json"})
            if str(value.get("code")) == "11217":
                return {"waiting": "waiting_authorization"}
            token = payload(value)
            required(token, "accessToken")
            # Persist the issued token before the second network call.
            await self.checkpoint({"issued_token": token})
        domain = token.get("domain") or urlsplit(self.host).hostname
        headers = {"Authorization": "Bearer " + required(token, "accessToken"), "X-Domain": str(domain), "X-No-User-Id": "true", "X-No-Enterprise-Id": "true", "X-No-Department-Info": "true"}
        data = payload(await self.t.json("GET", self.host + "/v2/plugin/accounts", headers))
        accounts = [a for a in data.get("accounts", []) if isinstance(a, dict) and a.get("uid")]
        identity = material.get("expected_identity")
        if identity:
            accounts = [a for a in accounts if workbuddy_identity(a) == str(identity)]
        selection=material.get("selected_identity")
        def selection_key(a):
            return json.dumps([str(a["uid"]),str(a.get("enterpriseId") or "")],separators=(",",":"))
        if selection:
            accounts=[a for a in accounts if selection_key(a)==selection]
        if len(accounts) != 1:
            if not accounts:
                raise UpstreamError("identity_mismatch")
            return {"waiting": "waiting_identity", "identities": [{"id": str(a["uid"]), "enterprise_id": str(a.get("enterpriseId") or ""),"selection_key":selection_key(a)} for a in accounts]}
        account = accounts[0]
        auth = {**token, "domain": domain, "lastRefreshTime": int(time.time() * 1000)}
        for source, dest in (("expiresIn", "expiresAt"), ("refreshExpiresIn", "refreshExpiresAt")):
            if token.get(source):
                auth[dest] = int(time.time() * 1000) + int(token[source]) * 1000
        return {"identity": workbuddy_identity(account), "credential": {"account": account, "auth": auth}}


class MarvisLogin:
    def __init__(self, transport):
        self.t = transport

    async def start(self, material, mode):
        guid = uuid4().hex
        login_type = "QC" if mode == "oauth_qq" else "WX"
        state = guid if login_type == "WX" else f"{secrets.randbelow(100)}.{secrets.randbits(96):029d}"
        return {"waiting": "waiting_authorization", "guid": guid, "state": state, "login_type": login_type, "authorization_url": marvis.authorize_url(login_type, guid, state)}

    async def verify(self, material, mode, code):
        parsed = urlsplit(code or "")
        target = urlsplit(marvis.oauth_url())
        if parsed.scheme != target.scheme or parsed.netloc != target.netloc or parsed.path != target.path:
            raise UpstreamError("invalid_oauth_callback", 422)
        values = parse_qs(parsed.query)
        if any(len(values.get(k, [])) != 1 for k in ("code", "state")) or values["state"][0] != material["state"]:
            raise UpstreamError("oauth_state_mismatch", 422)
        params = {"login_type": material["login_type"], "code": values["code"][0], "state": material["state"]}
        value, cookies = await self.t.oauth_envelope(marvis.oauth_url() + "?" + urlencode(params), {"Ual-Access-Guid": material["guid"], "Accept": "application/json", "User-Agent": marvis.BROWSER_UA})
        # Check the envelope AND nested business codes, including 302 bodies.
        data = payload(value)
        data = payload(data.get("user_info", data))
        source = {**cookies, **data}
        credential = {"openid": required(source, "openid", "open_id", "openId"), "access_token": required(source, "accesstoken", "access_token", "accessToken"), "refresh_token": source.get("refreshtoken") or source.get("refresh_token") or source.get("refreshToken") or "", "guid": material["guid"], "login_type": "1" if material["login_type"] == "QC" else "2"}
        seconds = source.get("expires_in", source.get("expiresIn"))
        if seconds is not None:
            credential["expires_at"] = time.time() + float(seconds)
        elif source.get("expires_at") or source.get("expiresAt"):
            credential["expires_at"] = float(source.get("expires_at") or source["expiresAt"])
        credential["profile"] = official_profile(source)
        return {"identity": credential["openid"], "credential": credential}


class VerifiedImport:
    """Official client/browser handoff; never invent an SMS/SSO contract."""
    def __init__(self, transport, platform):
        self.t, self.platform = transport, platform

    async def start(self, material, mode):
        credential = material.get("credential")
        if not isinstance(credential, dict):
            raise UpstreamError("invalid_credential_format", 422)
        if self.platform == "WPS 灵犀":
            cookie = required(credential, "cookie")
            if not any(part.strip().startswith("wps_sid=") and part.strip()[8:] for part in cookie.split(";")):
                raise UpstreamError("wps_sso_incomplete", 422)
            base = credential.get("base", wps.KDOCS_BASE)
            if base not in {wps.BASE, wps.KDOCS_BASE}:
                raise UpstreamError("invalid_credential_format", 422)
            profile = payload(await self.t.json("GET", base + wps.PROFILE_PATH, wps.account_headers(cookie, base)))
            identity = required(profile, "id")
            credential = {**credential, "base": base, "userid": identity, "profile": official_profile(profile)}
        elif self.platform == "小浣熊":
            identity, profile = await raccoon_identity(self.t, credential)
            credential = {**credential,"userId":identity,"profile":official_profile(profile),"officeIdentity":profile.get("officeIdentity") or profile.get("office_identity") or credential.get("officeIdentity") or credential.get("office_identity") or "personal"}
        elif self.platform == "CodeArts":
            from .codearts_login import identity as codearts_identity
            observed = await codearts_identity(self.t,credential)
            identity = str(observed["account_id"]) + ":" + str(observed["principal_id"])
            credential = {**credential, "verified_identity": observed}
        else:
            raise UpstreamError("registration_adapter_unavailable", 501)
        if material.get("phone"):
            credential["phone"] = material["phone"]
        return {"identity": identity, "credential": credential}


def connector(platform, transport, mode=None):
    if platform == "WPS 灵犀" and mode == "browser":
        return WPSLogin(transport)
    if platform == "CodeArts" and mode in {"official", "register"}:
        from .codearts_login import CodeArtsLogin
        return CodeArtsLogin(transport)
    if platform == "小浣熊" and mode == "official":
        return RaccoonLogin(transport)
    if platform == "WorkBuddy 国内" and mode == "sms":
        from .workbuddy_sms import WorkBuddySMS
        return WorkBuddySMS(transport)
    if platform.startswith("WorkBuddy"):
        return WorkBuddyLogin(transport, platform)
    if platform == "Marvis":
        return MarvisLogin(transport)
    return VerifiedImport(transport, platform)


class WPSLogin:
    def __init__(self,transport):
        self.t = transport

    async def start(self,material,mode):
        callback = "https://account.kdocs.cn/passport/singlesign?" + urlencode({"cb":"https://lingxi.kdocs.cn/"})
        return {"waiting":"waiting_authorization", "authorization_url":"https://account.wps.cn/?" + urlencode({"protocolnames":"lingxi_privacy,lingxi_agreements", "cb":callback})}

    async def verify(self,material,mode,code):
        return await VerifiedImport(self.t,"WPS 灵犀").start(material,"verified_import")
