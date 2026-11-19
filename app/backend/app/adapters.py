"""Platform contracts share transport, not the source projects' DB/schedulers."""
import base64
import asyncio
import hashlib
import json
import time
from contextlib import aclosing
from urllib.parse import quote, urlencode
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4

from .upstream import Transport, UpstreamError, amount, classify, payload, frame_failure
from .vendor.mirai.loomy import endpoints as loomy
from .vendor.mirai.raccoon import endpoints as raccoon
from .vendor.mirai.wps_lingxi import endpoints as wps, protocol as wps_protocol
from .vendor.mirai.codearts import endpoints as codearts, signer
from .vendor.mirai.marvis import endpoints as marvis
from .model_catalog import normalize as normalize_catalog


def encode(body):
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def required(data, *keys):
    for key in keys:
        if data.get(key):
            return str(data[key])
    raise UpstreamError("missing_material", 422)


class Adapter:
    capabilities = {"chat", "balance", "models"}

    def __init__(self, transport, credential, identity, version=1):
        self.t, self.c, self.identity, self.version = transport, credential, identity, version

    async def request(self, method, url, headers, body=None):
        headers = {**headers, "Content-Type": "application/json"}
        return payload(await self.t.json(method, url, headers, encode(body) if body is not None else None))

    async def chat(self, body, session):
        url, headers, outgoing = await self.prepare(body, session)
        async with aclosing(self.t.sse(url, headers, encode(outgoing))) as frames:
            async for frame in frames:
                failure = frame_failure(frame)
                if failure:
                    raise failure
                yield frame

    async def validate_renewal(self, credential):
        raise UpstreamError("identity_verifier_unavailable", 501)

    async def checkin_receipt(self,day):
        raise UpstreamError("checkin_readback_unavailable",501)


class Loomy(Adapter):
    capabilities = Adapter.capabilities | {"checkin", "invite_read", "invite_bind", "onboarding"}
    base = "https://loomyad.xunfei.cn/api/v1"

    def headers(self):
        return loomy.points_headers(required(self.c, "session", "token"))

    async def prepare(self, body, session):
        return self.base + loomy.CHAT_PATH, {**loomy.chat_headers(required(self.c, "session", "token"), session_id=session), "Content-Type": "application/json"}, {**body, "stream": True}

    async def balance(self):
        data = await self.request("GET", self.base + loomy.POINTS_BALANCE_PATH, self.headers())
        if not isinstance(data, dict):
            raise UpstreamError("invalid_balance")
        if "availableBalance" in data:
            return amount(data["availableBalance"])
        return amount(data.get("balance")) + amount(data.get("dailyBalance", 0))

    async def checkin(self, day):
        # A successful claim response is the receipt; generic HTTP200 is not.
        data = await self.t.json("POST", self.base + loomy.POINTS_FIRST_LOGIN_PATH, {**self.headers(), "Content-Type": "application/json"}, b"{}")
        receipt = payload(data)
        if str(data.get("code")) not in {"000000", "0", "200"} or not isinstance(receipt, dict) or receipt.get("dailyCycleDate") != day:
            raise UpstreamError("unconfirmed_checkin")
        return {"day": day}

    async def checkin_receipt(self, day):
        # first-login is idempotent (a repeat returns alreadyProcessed with the same
        # dailyCycleDate, measured 2026-10-29 in MiraiHub loomy/services/checkin.py), so a
        # lost claim is reconciled by asking again instead of inferring from the balance.
        return await self.checkin(day)

    async def onboarding(self):
        from .onboarding import loomy_claim
        return await loomy_claim(self)

    async def onboarding_receipt(self):
        from .onboarding import loomy_receipt
        return await loomy_receipt(self)

    async def models(self):
        return await self.request("GET", self.base + loomy.MODELS_PATH, loomy.auth_headers(required(self.c, "session", "token")))

    async def invite_read(self):
        return await self.request("GET", self.base + loomy.POINTS_ACTIVATION_PATH, self.headers())

    async def invite_codes(self):
        return await self.request("GET", self.base + loomy.POINTS_INVITATION_CODES_PATH, self.headers())

    async def invite_submit(self, code):
        return await self.request("POST", self.base + loomy.POINTS_ACTIVATION_PATH, self.headers(), {"inviteCode": code})

    async def invite_bind(self, code):
        await self.invite_submit(code)
        return await self.invite_read()


class Raccoon(Adapter):
    capabilities = Adapter.capabilities | {"renew", "checkin", "onboarding"}
    base = "https://xiaohuanxiong.com"

    def access_token(self):
        token = required(self.c, "access_token", "accessToken", "token").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise UpstreamError("missing_material", 422)
        return token

    def organization(self):
        return self.c.get("officeIdentity") or self.c.get("office_identity")

    def headers(self):
        headers = raccoon.auth_headers(self.access_token())
        org = self.organization()
        if org and org != "personal":
            headers["X-Org-Code"] = str(org)
        return headers

    async def daily_grant(self, day):
        cursor, seen = "", set()
        for _ in range(100):
            query = {"paging.limit": 100, "type": "all"}
            if cursor:
                query["cursor"] = cursor
            data = await self.request("GET", self.base + "/api/web/points/v2/bills?" + urlencode(query), self.headers())
            rows = data.get("items")
            if not isinstance(rows, list):
                raise UpstreamError("unconfirmed_checkin")
            older = False
            for row in rows:
                try:
                    stamp = datetime.fromisoformat((row.get("grant_at") or row.get("created_at") or "").replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                    date = stamp.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                except ValueError:
                    continue
                if row.get("biz_type") == "daily_grant" and date == day and amount(row.get("points")) > 0:
                    return True
                # Use creation date for paging; grants may be backdated.
                created = str(row.get("created_at", ""))[:10]
                older |= bool(created and created < day)
            paging = data.get("paging") or {}
            if not rows or older or data.get("has_more", paging.get("has_more")) is False:
                return False
            next_cursor = data.get("next_cursor") or data.get("cursor") or paging.get("next_cursor") or paging.get("cursor")
            if not next_cursor:
                if data.get("has_more", paging.get("has_more")) is True:
                    raise UpstreamError("unconfirmed_checkin")
                return False
            if next_cursor in seen:
                raise UpstreamError("unconfirmed_checkin")
            cursor = next_cursor
            seen.add(cursor)
        raise UpstreamError("unconfirmed_checkin")

    async def checkin(self, day):
        if not await self.daily_grant(day):
            org = self.organization()
            path = "/api/web/org/user/office/v1/setting_info" if org and org != "personal" else "/api/web/office/v3/setting_info"
            await self.request("GET", self.base + path, self.headers())
            if not await self.daily_grant(day):
                raise UpstreamError("unconfirmed_checkin")
        return {"day": day}

    async def checkin_receipt(self,day):
        if not await self.daily_grant(day):
            raise UpstreamError("unconfirmed_checkin")
        return {"day":day}

    async def onboarding(self):
        from .onboarding import raccoon_claim
        return await raccoon_claim(self)

    async def prepare(self, body, session):
        return self.base + "/api/web/llm/v2/chat/completions", {**self.headers(), "Content-Type": "application/json"}, {**body, "stream": True}

    async def balance(self):
        data = await self.request("GET", self.base + raccoon.POINTS_BALANCE_PATH, self.headers())
        for key in ("available_points", "total_chargeable_points"):
            if isinstance(data, dict) and key in data:
                return amount(data[key])
        raise UpstreamError("invalid_balance")

    async def models(self):
        return normalize_catalog("小浣熊",await self.request("GET", self.base + "/api/web/llm/v2/model_catalog", self.headers()))

    async def renew(self):
        token = self.access_token()
        try:
            part = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (IndexError, ValueError):
            claims = {}
        if str(claims.get("iss", "")).endswith("/iam"):
            raise UpstreamError("iam_refresh_material_required", 422)
        data = await self.request("POST", self.base + raccoon.AUTH_REFRESH_PATH, self.headers(), {"refresh_token": required(self.c, "refresh_token")})
        new = {**self.c, "access_token": required(data, "access_token", "token"), "refresh_token": data.get("refresh_token") or self.c["refresh_token"]}
        try:
            old_part, new_part = token.split(".")[1], new["access_token"].split(".")[1]
            old_claims = json.loads(base64.urlsafe_b64decode(old_part + "=" * (-len(old_part) % 4)))
            new_claims = json.loads(base64.urlsafe_b64decode(new_part + "=" * (-len(new_part) % 4)))
        except (ValueError, IndexError):
            raise UpstreamError("identity_verifier_unavailable") from None
        if not old_claims.get("sub") or old_claims["sub"] != new_claims.get("sub"):
            raise UpstreamError("identity_mismatch")
        new["expires_at"] = amount(new_claims.get("exp"))
        return new

    async def validate_renewal(self, credential):
        candidate = Raccoon(self.t, credential, self.identity, self.version)
        await candidate.models()
        return True


def workbuddy_identity(account):
    """Platform identity key. The same uid under different enterprises is a different
    identity and must not be merged; personal accounts (no enterprise) keep the bare uid,
    so existing records stay valid."""
    uid = str(required(account, "uid", "userId", "id"))
    enterprise = str(account.get("enterpriseId") or account.get("enterprise_id") or "")
    return f"{uid}:{enterprise}" if enterprise else uid


class WorkBuddy(Adapter):
    supports_thinking_control = True
    capabilities = Adapter.capabilities | {"checkin", "renew"}
    base = "https://copilot.tencent.com"

    @staticmethod
    def outbound(body):
        """Translate supported wire shapes without injecting or editing prompts.

        WorkBuddy's Go request struct predates developer, object tool_choice and
        max_completion_tokens. Keep message order/text and explicit cache keys;
        reject unknown shapes rather than silently dropping client constraints.
        """
        from copy import deepcopy
        outgoing = deepcopy(body)
        if 'thinking' in outgoing:
            thinking = outgoing['thinking']
            # Display is a client presentation choice, not a WorkBuddy wire
            # parameter. Adaptive reasoning uses its enabled thinking switch;
            # do not invent or downgrade an explicit numeric token budget.
            thinking.pop('display', None)
            if thinking['type'] == 'adaptive':
                thinking['type'] = 'enabled'
            if thinking['type'] == 'disabled':
                outgoing.pop('reasoning_effort', None)
            elif str(outgoing.get('model', '')).lower().startswith('deepseek'):
                # WorkBuddy's DeepSeek switch also needs an effort (7864
                # thinking.go, sourced from the native client's configure
                # fallback). Only fill it when the client requested thinking.
                outgoing.setdefault('reasoning_effort', 'high')
        for message in outgoing.get("messages", []):
            if message.get("role") == "developer":
                message["role"] = "system"
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    if isinstance(part, dict) and part.get("type") == "image_url" and isinstance(part.get("image_url"), str):
                        part["image_url"] = {"url": part["image_url"]}
        # WorkBuddy consumes images from user messages, not nested tool results
        # (MiraiHub #49). Preserve image bytes/URLs rather than stringify or
        # truncate them. Keep all consecutive tool replies together before
        # emitting their images, so a parallel tool call sequence stays valid.
        messages, tool_images = [], []
        for message in outgoing.get("messages", []):
            if message.get("role") != "tool" and tool_images:
                messages.append({"role": "user", "content": tool_images})
                tool_images = []
            parts = message.get("content")
            if message.get("role") == "tool" and isinstance(parts, list):
                images = [part for part in parts if isinstance(part, dict) and part.get("type") == "image_url"]
                if images:
                    message["content"] = [part for part in parts if not (isinstance(part, dict) and part.get("type") == "image_url")] or ""
                    tool_images.extend(images)
            messages.append(message)
        if tool_images:
            messages.append({"role": "user", "content": tool_images})
        if "messages" in outgoing:
            outgoing["messages"] = messages
        if "max_completion_tokens" in outgoing:
            value = outgoing.pop("max_completion_tokens")
            if "max_tokens" not in outgoing:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise UpstreamError("unsupported_protocol_field", 422)
                outgoing["max_tokens"] = value
        if "tool_choice" in outgoing:
            choice = outgoing["tool_choice"]
            if isinstance(choice, dict):
                kind = choice.get("type")
                if kind in {"auto", "required", "none"}:
                    choice = kind
                elif kind == "function":
                    function = choice.get("function")
                    choice = function.get("name") if isinstance(function, dict) else choice.get("name")
                else:
                    raise UpstreamError("unsupported_protocol_field", 422)
            if not isinstance(choice, str) or not choice.strip():
                raise UpstreamError("unsupported_protocol_field", 422)
            if choice == "none":
                # Official contract: declarations must also be absent to enforce
                # no tools. This does not alter historical tool messages/results.
                outgoing.pop("tool_choice", None)
                outgoing.pop("tools", None)
                outgoing.pop("functions", None)
            else:
                outgoing["tool_choice"] = choice
        outgoing.setdefault("stream_options", {"include_usage": True})
        return outgoing

    def headers(self):
        auth, account = self.c.get("auth", {}), self.c.get("account", {})
        h = {"Authorization": "Bearer " + required(auth, "accessToken"), "X-User-Id": required(account, "uid"), "X-Domain": required(auth, "domain"), "User-Agent": "WorkBuddy/5.9.3 WorkBuddy/5.9.3 CLI/2.165.0", "Content-Type": "application/json", "X-Product": "SaaS", "X-IDE-Type": "WorkBuddy", "X-Requested-With": "XMLHttpRequest", "X-Agent-Intent": "craft", "X-Agent-Purpose": "conversation", "X-IDE-Name": "WorkBuddy", "X-IDE-Version": "5.9.3"}
        enterprise = account.get("enterpriseId") or account.get("enterprise_id")
        if enterprise:
            h.update({"X-Enterprise-Id": str(enterprise), "X-Tenant-Id": str(enterprise)})
        return h

    async def prepare(self, body, session):
        headers = {**self.headers(), "X-Conversation-ID": session, "X-Conversation-Request-ID": str(uuid4())}
        return self.base + "/v2/chat/completions", headers, {**self.outbound(body), "stream": True, "prompt_cache_key": body.get("prompt_cache_key") or session}

    async def balance(self):
        data = await self.request("POST", self.base + "/v2/billing/meter/get-user-resource", self.headers(), {})
        rows = data.get("Response", {}).get("Data", {}).get("Accounts")
        if not isinstance(rows, list):
            raise UpstreamError("invalid_balance")
        credits = [r for r in rows if r.get("CapacityUnit") == "credits"]
        if not credits:
            raise UpstreamError("invalid_balance")
        return sum(amount(r.get("CycleCapacityRemain")) for r in credits)

    async def models(self):
        data=await self.request("GET",self.base+"/v3/config",self.headers())
        rows=normalize_catalog("WorkBuddy",data)
        eid=self.c.get("account",{}).get("enterpriseId") or self.c.get("account",{}).get("enterprise_id")
        if eid:
            custom=await self.request("GET",self.base+"/console/enterprises/"+quote(str(eid),safe="")+"/config/models",self.headers())
            if isinstance(custom,list) and custom:
                rows+=normalize_catalog("WorkBuddy",{"models":custom})
        return rows

    async def checkin(self, day):
        from .store import day_at
        if day != day_at(time.time()):
            raise UpstreamError('checkin_readback_unavailable')
        path = self.base + "/v2/billing/meter/checkin-activity-status"
        data = await self.request("POST", path, self.headers(), {})
        if day != day_at(time.time()):
            raise UpstreamError('unconfirmed_checkin')
        if data.get("today_checked_in",data.get("todayCheckedIn")) is not True:
            if data.get("active") is not True:
                raise UpstreamError("checkin_inactive")
            await self.request("POST", self.base + "/v2/billing/meter/daily-checkin", self.headers(), {})
            if day != day_at(time.time()):
                raise UpstreamError('unconfirmed_checkin')
            data = await self.request("POST", path, self.headers(), {})
        if day != day_at(time.time()) or data.get("today_checked_in",data.get("todayCheckedIn")) is not True:
            raise UpstreamError("unconfirmed_checkin")
        return {"day": day}

    async def checkin_receipt(self,day):
        from .store import day_at
        if day != day_at(time.time()):
            raise UpstreamError("checkin_readback_unavailable")
        data=await self.request("POST",self.base+"/v2/billing/meter/checkin-activity-status",self.headers(),{})
        if day != day_at(time.time()) or data.get("today_checked_in",data.get("todayCheckedIn")) is not True:
            raise UpstreamError("unconfirmed_checkin")
        return {"day":day}

    async def renew(self):
        h = {**self.headers(), "X-Refresh-Token": required(self.c.get("auth", {}), "refreshToken"), "X-Auth-Refresh-Source": "plugin"}
        data = await self.request("POST", self.base + "/v2/plugin/auth/token/refresh", h, {})
        auth = data.get("auth", data)
        required(auth, "accessToken")
        identity = (data.get("account") or {}).get("uid") or data.get("uid")
        if identity and str(identity) != str(self.c.get("account", {}).get("uid")):
            raise UpstreamError("identity_mismatch")
        updated={**self.c["auth"], **{k: v for k, v in auth.items() if v is not None and v != ""},"lastRefreshTime":int(time.time()*1000)}
        for source,dest in (("expiresIn","expiresAt"),("refreshExpiresIn","refreshExpiresAt")):
            if auth.get(source):
                updated[dest]=int(time.time()*1000)+int(auth[source])*1000
        return {**self.c, "auth": updated, "account": {**self.c.get("account", {}), **data.get("account", {})}}

    async def validate_renewal(self, credential):
        candidate = type(self)(self.t, credential, self.identity, self.version)
        data=await candidate.request("GET",candidate.base+"/v2/plugin/accounts",{**candidate.headers(),"X-No-User-Id":"true","X-No-Enterprise-Id":"true","X-No-Department-Info":"true"})
        expected=self.c.get("account",{})
        matches=[a for a in data.get("accounts",[]) if str(a.get("uid"))==str(expected.get("uid")) and str(a.get("enterpriseId") or "")==str(expected.get("enterpriseId") or "")]
        if len(matches)!=1:
            raise UpstreamError("identity_mismatch")
        return True


class Marvis(Adapter):
    capabilities = Adapter.capabilities | {"renew"}

    async def prepare(self, body, session):
        model = marvis.canonical_model_id(body["model"])
        if marvis.is_unavailable_model(model):
            raise UpstreamError("model_not_found", 404)
        return marvis.chat_url(), marvis.llm_headers(self.c, conversation_id=session, response_id=str(uuid4())), {**body, "model": model, "stream": True}

    async def balance(self):
        data = await self.t.json("POST", marvis.wallet_url(), marvis.business_headers(self.c, query="{}", method="POST"), b"{}")
        data = payload(data)
        if data is None:
            raise UpstreamError("invalid_credentials", 401)
        return max(0, amount(data.get("total_tokens")) - amount(data.get("used_tokens")))

    async def models(self):
        return marvis.models_catalog()

    async def renew(self):
        required(self.c, "refresh_token")
        required(self.c, "access_token")
        expected = required(self.c, "openid")
        text = marvis.refresh_body(self.c)
        raw = await self.t.json("POST", marvis.refresh_url(), marvis.business_headers(self.c, query=text, method="POST"), text.encode())
        # MiraiHub v1.1.4 records these only for the Marvis refresh envelope.
        # A request-shape error must not turn a working account into a relogin.
        code = str(raw.get("code")) if isinstance(raw, dict) else ""
        if code in {"-107", "-101", "-114"}:
            raise UpstreamError("invalid_credentials", 401)
        if code in {"-109", "-7", "-5"}:
            raise UpstreamError("request_rejected", 400)
        data = payload(raw)
        if not isinstance(data, dict):
            raise UpstreamError("invalid_response")
        user = data.get("user_info") or data.get("userInfo") or data
        if not isinstance(user, dict):
            raise UpstreamError("invalid_response")
        access = required(user, "access_token", "accessToken", "accesstoken")
        identities = [str(user[key]) for key in ("openid", "openId") if user.get(key) not in (None, "")]
        if any(identity != expected for identity in identities):
            raise UpstreamError("identity_mismatch")
        # The official refresh contract can omit openid. Only an explicit
        # successful authenticated refresh may retain the submitted identity.
        if not identities and str(raw.get("code")) not in {"0", "200", "000000", "success"}:
            raise UpstreamError("identity_verifier_unavailable")
        seconds = 0
        for key in ("expires_in", "expiresIn", "access_token_expire_time"):
            try:
                value = user.get(key)
                if isinstance(value, bool):
                    continue
                candidate_seconds = int(float(value))
            except (TypeError, ValueError, OverflowError):
                continue
            # These official fields are relative seconds, not absolute timestamps.
            if candidate_seconds > 1e9:
                break
            if candidate_seconds > 0:
                seconds = candidate_seconds
                break
        return {**self.c, "openid": expected, "access_token": access,
                "refresh_token": user.get("refresh_token") or user.get("refreshtoken") or user.get("refreshToken") or self.c["refresh_token"],
                "expires_in": seconds, "expires_at": int(time.time()) + seconds if seconds else 0}

    async def validate_renewal(self, credential):
        await Marvis(self.t, credential, self.identity, self.version).balance()
        return True


class WorkBuddyInternational(WorkBuddy):
    base = "https://www.workbuddy.ai"
    # Magpie's international contract has billing, but no daily check-in hook.
    capabilities = {"chat", "models", "renew", "balance"}

    async def prepare(self, body, session):
        url, headers, outgoing = await super().prepare(body, session)
        # The global chat client has a different documented identity than CN.
        # Apply only wire headers; never inject/replace a client's prompt.
        request_id = headers["X-Conversation-Request-ID"]
        message_id, trace_id, span_id = str(uuid4()), uuid4().hex, uuid4().hex[:16]
        headers.update({"User-Agent": "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0", "X-IDE-Version": "5.8.4",
                        "X-Domain": "www.workbuddy.ai", "Accept": "text/event-stream", "X-Agent-Type": "main",
                        "X-Private-Data": "true", "X-Root-Request-ID": request_id,
                        "X-Conversation-Message-ID": message_id, "X-Request-ID": message_id,
                        "X-Trace-ID": trace_id, "traceparent": f"00-{trace_id}-{span_id}-01", "b3": f"{trace_id}-{span_id}-1",
                        "X-B3-TraceId": trace_id, "X-B3-SpanId": span_id, "X-B3-Sampled": "1"})
        return url, headers, outgoing

    async def balance(self):
        summary=await self.request("POST",self.base+"/billing/meter/get-user-resource-summary",self.headers(),{})
        if not isinstance(summary,dict):
            raise UpstreamError("invalid_balance")
        groups=summary.get("CreditGroups",[])
        packages=summary.get("Packages",[])
        if not isinstance(groups,list) or not isinstance(packages,list):
            raise UpstreamError("invalid_balance")
        def has_total(row):
            return isinstance(row,dict) and row.get("CycleTotalCapacity") not in (None,"")
        # Groups aggregate packages: count one representation, never both.
        totals=[r for r in groups if has_total(r)] or [r for r in packages if has_total(r)]
        prepaid=[r for r in packages if isinstance(r,dict) and not has_total(r) and r.get("CycleRemainCapacity") not in (None,"")]
        if not totals and not prepaid:
            if self.c.get("account",{}).get("enterpriseId"):
                ent=await self.request("POST",self.base+"/billing/meter/get-enterprise-user-usage",self.headers(),{})
                return max(0,amount(ent.get("limitNum"))-amount(ent.get("credit")))
            # Empty/unknown quota does not prove a zero balance.
            raise UpstreamError("invalid_balance")
        return sum(max(0,amount(r["CycleTotalCapacity"])-amount(r.get("CycleUsedCapacity"))) for r in totals)+sum(amount(r["CycleRemainCapacity"]) for r in prepaid)

    def headers(self):
        headers = super().headers()
        headers["User-Agent"] = "WorkBuddy/5.9.3 workbuddy-ai/5.9.3 CLI/2.165.0"
        return headers


class CodeArts(Adapter):
    capabilities = Adapter.capabilities | {"checkin", "renew"}

    async def _checkin_delivery(self):
        data = await self.read("/v1/ops/delivery?channel=IDE")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise UpstreamError("unconfirmed_checkin")
        return [x for x in data["items"] if isinstance(x, dict)
                and x.get("type") == "USER_LOGIN" and x.get("benefitUnit") == "CREDIT"]

    @staticmethod
    def _checkin_confirmed(item):
        return item.get("claimable") is False and item.get("status") in {"CONFIRMED", "CONSUMED"}

    async def checkin_receipt(self, day):
        ledger = getattr(self, "ledger", None)
        if ledger is None:
            raise UpstreamError("durable_ledger_required")
        receipt = await ledger("checkin:" + day)
        if receipt and receipt.get("day") == day:
            return {"day": day}
        # The delivery status is undated. It proves today's claim only when tied
        # to a durable observation of a claimable campaign in today's operation.
        # This also recovers a crash after confirm but before saving the receipt.
        plan = await ledger("checkin_plan:" + day)
        if not isinstance(plan, dict) or plan.get("day") != day or not plan.get("claim_ids"):
            raise UpstreamError("unconfirmed_checkin")
        items = await self._checkin_delivery()
        expected = plan.get("campaign_ids") or []
        if not expected or not all(any(x.get("campaignId") == cid and self._checkin_confirmed(x)
                                       for x in items) for cid in expected):
            raise UpstreamError("unconfirmed_checkin")
        await ledger("checkin:" + day, {"day": day})
        return {"day": day}

    def signed(self, method, path, body=None):
        content = encode(body) if body is not None else b""
        headers = {"Content-Type": "application/json", "X-Security-Token": required(self.c, "security_token", "securityToken"), "X-Language": "zh-cn", "Agent-Type": "PromptCenter"}
        return codearts.BASE + path, signer.sign(method, codearts.BASE + path, headers, content, required(self.c, "access_key", "access_key_id", "accessKeyId"), required(self.c, "secret_key", "secret_access_key", "secretAccessKey")), content

    async def renew(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        context = self.c.get("oauth_context") or {}
        verifier = self.c.get("pkce_code_verifier") or (context.get("pkce_pair") or {}).get("code_verifier")
        pair = context.get("dpop_key_pair") or {}
        private = self.c.get("dpop_private_key") or pair.get("private_key_jwk")
        public = self.c.get("dpop_public_key") or pair.get("public_key_jwk")
        refresh = required(self.c, "refresh_token", "refreshToken")
        if not verifier or not private or not public:
            raise UpstreamError("missing_material", 422)
        if isinstance(private, str):
            private = json.loads(private)
        if isinstance(public, str):
            public = json.loads(public)
        if private.get("kty") != "EC" or private.get("crv") != "P-256":
            raise UpstreamError("invalid_credential_format", 422)
        def b64(value):
            return base64.urlsafe_b64encode(value).rstrip(b"=")
        url = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
        header = {"alg": "ES256", "typ": "dpop+jwt", "jwk": {k: public[k] for k in ("kty", "crv", "x", "y")}}
        claims = {"htm": "POST", "htu": url, "iat": int(time.time()), "jti": uuid4().hex}
        signing_input = b64(encode(header)) + b"." + b64(encode(claims))
        scalar = int.from_bytes(base64.urlsafe_b64decode(private["d"] + "=" * (-len(private["d"]) % 4)), "big")
        key = ec.derive_private_key(scalar, ec.SECP256R1())
        r, sig_s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        proof = (signing_input + b"." + b64(r.to_bytes(32, "big") + sig_s.to_bytes(32, "big"))).decode()
        body = urlencode({"client_id": self.c.get("client_id") or "vscode-codebot", "code_verifier": verifier, "grant_type": "refresh_token", "refresh_token": refresh}).encode()
        data = payload(await self.t.json("POST", url, {"Content-Type":"application/x-www-form-urlencoded", "DPoP": proof}, body))
        issued = data.get("credentials") or {}
        expiry = required(issued, "expiration")
        try:
            expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise UpstreamError("invalid_response") from None
        return {**self.c, "access_key": required(issued, "access_key_id", "access_key"), "secret_key": required(issued, "secret_access_key", "secret_key"), "security_token": required(issued, "security_token"), "expires_at": expiry, "refresh_token": data.get("refresh_token") or refresh}

    async def validate_renewal(self, credential):
        from .codearts_login import identity as read_identity
        identity = await read_identity(self.t, credential)
        # The imported identity is an explicit piece of registration evidence;
        # a successful STS exchange alone cannot prove which user was returned.
        expected = self.c.get("verified_identity")
        if not isinstance(expected, dict) or not expected:
            raise UpstreamError("identity_verifier_unavailable", 422)
        if any(identity.get(k) != v for k, v in expected.items()):
            raise UpstreamError("identity_mismatch")
        return True

    async def prepare(self, body, session):
        outgoing = {**body, "stream": True}
        url, headers, _ = self.signed("POST", codearts.CHAT, outgoing)
        return url, headers, outgoing

    async def read(self, path):
        url, headers, body = self.signed("GET", path)
        return payload(await self.t.json("GET", url, headers))

    async def post(self, path, data):
        url, headers, body = self.signed("POST", path, data)
        return payload(await self.t.json("POST", url, headers, body))

    async def checkin(self, day):
        ledger = getattr(self, "ledger", None)
        if ledger is None:
            raise UpstreamError("durable_ledger_required")
        receipt = await ledger("checkin:" + day)
        if receipt and receipt.get("day") == day:
            return {"day": day}
        items = await self._checkin_delivery()
        if not items:
            raise UpstreamError("unconfirmed_checkin")
        plan = await ledger("checkin_plan:" + day)
        if not plan:
            for item in items:
                cid = item.get("campaignId")
                if not isinstance(cid, (str, int)) or isinstance(cid, bool):
                    raise UpstreamError("invalid_response")
            claim_ids = [x["campaignId"] for x in items if x.get("claimable") is True
                         and not self._checkin_confirmed(x)]
            if not claim_ids:
                # CLAIMED/CONFIRMED without a dated local observation may be
                # yesterday's activity. Do not confirm it as a fresh daily claim.
                raise UpstreamError("unconfirmed_checkin")
            plan = {"day": day, "campaign_ids": [x["campaignId"] for x in items], "claim_ids": claim_ids}
            await ledger("checkin_plan:" + day, plan)
        if plan.get("day") != day or not plan.get("claim_ids"):
            raise UpstreamError("unconfirmed_checkin")
        for cid in plan["claim_ids"]:
            item = next((x for x in items if x.get("campaignId") == cid), None)
            if item is None:
                raise UpstreamError("unconfirmed_checkin")
            if self._checkin_confirmed(item):
                continue
            key = "campaign:" + day + ":" + str(cid)
            progress = await ledger(key) or {"idempotentKey": "claim_" + uuid4().hex}
            if item.get("status") == "CLAIMED":
                progress["claimed"] = True
            if item.get("claimable") is True and not progress.get("claimed"):
                await ledger(key, progress)
                result = await self.post("/v1/ops/claim", {"campaignId": cid, "idempotentKey": progress["idempotentKey"], "channel": "IDE"})
                if result.get("campaignId") != cid:
                    raise UpstreamError("identity_mismatch")
                progress["claimed"] = True
                await ledger(key, progress)
            if progress.get("claimed"):
                await self.post("/v1/ops/confirm", {"campaignId": cid})
        return await self.checkin_receipt(day)

    async def models(self):
        return normalize_catalog("CodeArts",await self.read(codearts.MODELS))

    async def balance(self):
        data = await self.read(codearts.QUOTA)
        metrics = data.get("metrics", data)
        if isinstance(metrics, list):
            metrics = {r.get("name"): r for r in metrics if isinstance(r, dict)}
        value = metrics.get("usageTotalPackageCredit", {})
        return amount(value.get("package_credit_remain") if isinstance(value, dict) else None)


class WPS(Adapter):
    capabilities = Adapter.capabilities | {"checkin"}
    base = "https://lingxi.wps.cn"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .session_cache import SessionLocks
        self.cache, self.cache_locks, self.shared_cache = {}, SessionLocks(), None
        if self.c.get("base") in {wps.BASE, wps.KDOCS_BASE}:
            self.base = self.c["base"]

    def headers(self):
        return wps.account_headers(required(self.c, "cookie"), self.base)

    async def balance(self):
        data = await self.request("GET", self.base + wps.BALANCE_PATH, self.headers())
        return amount(data.get("total_balance") if "total_balance" in data else data.get("balance"))

    async def models(self):
        return normalize_catalog("WPS 灵犀",await self.request("GET", self.base + wps.PLANS_PATH, self.headers()))

    async def checkin_receipt(self,day):
        from .store import day_at
        data=await self.request("GET",self.base+wps.TASKS_PATH,self.headers())
        task=next((x for x in data.get("tasks",[]) if x.get("task_key")=="daily_check_in"),{})
        days=(task.get("extra") or {}).get("check_in_days")
        dated=isinstance(days,list) and any(x.get("date")==day and x.get("status")=="claimed" for x in days)
        current=day==day_at(time.time()) and task.get("status")=="claimed"
        if not dated and not current:
            raise UpstreamError("unconfirmed_checkin")
        return {"day":day}

    async def checkin(self, day):
        async def daily():
            data = await self.request("GET", self.base + wps.TASKS_PATH, self.headers())
            task = next((x for x in data.get("tasks", []) if x.get("task_key") == "daily_check_in"), None)
            if task is None:
                raise UpstreamError("unconfirmed_checkin")
            return task
        task = await daily()
        if task.get("status") != "claimed":
            await self.request("POST", self.base + (wps.CLAIM_PATH % "daily_check_in") + "?" + urlencode({"date": day}), self.headers(), {})
            task = await daily()
        if task.get("status") != "claimed":
            raise UpstreamError("unconfirmed_checkin")
        days = (task.get("extra") or {}).get("check_in_days")
        if isinstance(days, list) and not any(x.get("date") == day and x.get("status") == "claimed" for x in days):
            raise UpstreamError("unconfirmed_checkin")
        return {"day": day}

    async def prepare(self, body, session):
        if any(isinstance(m.get("content"), list) and any(p.get("type") != "text" for p in m["content"]) for m in body["messages"]):
            raise UpstreamError("unsupported_image_input", 422)
        if any(k in body for k in ("response_format", "top_p", "presence_penalty", "frequency_penalty", "seed", "stop")):
            raise UpstreamError("unsupported_protocol_field", 422)
        # Explicit size rejection, never silently truncate user's messages.
        if len(encode(body)) > 320 * 1024 or any(len(encode(m)) > 48 * 1024 for m in body["messages"]):
            raise UpstreamError("prompt_too_large", 413)
        # Validate the complete native shape before creating an upstream
        # session/run. Unsupported cache boundaries must not cause HTTP work.
        native = dict(body)
        if "max_tokens" not in native and "max_completion_tokens" in native:
            native["max_tokens"] = native["max_completion_tokens"]
        from .wps_request import build as build_wps_body
        outgoing = build_wps_body(native)
        if outgoing.get("tools"):
            # WPS rejects local $ref / union schema nodes. Reuse the pinned
            # official schema adapter without invoking context trimming.
            outgoing["tools"] = wps_protocol.filter_tools(outgoing["tools"])
        effort = body.get("reasoning_effort")
        if isinstance(effort, str) and effort:
            outgoing["reasoning_effort"] = effort
            outgoing["llm_arguments"] = {"reasoning_effort": effort}
        headers = wps.agent_headers(required(self.c, "cookie"), self.base)
        key = (self.identity, self.version, body["model"], session)
        async def create_session():
            data = await self.request("POST", self.base + wps.SESSIONS_PATH, headers, {"session_type": "cowork_client", "agent_version": 2, "env": {"device_id": hashlib.sha256(self.identity.encode()).hexdigest(), "device_name": "Windows", "work_space_dir": ""}})
            sid = required(data, "session_id")
            last = next((m.get("content", "") for m in reversed(body["messages"]) if m.get("role") == "user"), "")
            if not isinstance(last, str):
                last = "".join(p.get("text", "") for p in last if isinstance(p, dict))
            run = await self.request("POST", self.base + wps.RUNS_PATH, headers, {"client_request_id": str(uuid4()), "session_id": sid, "model_provider": "lingxi", "model_id": body["model"], "agent_input": {"text": last, "files": [], "file_ids": [], "expect_model": body["model"], "refs": [], "permission_mode": "default", "current_mode": "agent", "browser_plugin_enabled": False}})
            return {"session_id": sid, "run_id": required(run, "run_id"), "created": time.time()}
        from .wps_sessions import resolve as resolve_session
        async with self.cache_locks.hold(key):
            cached = await resolve_session(self, key, hashlib.sha256(encode(key)).hexdigest(), create_session)
        headers.update({"x-cc-session-id": cached["session_id"], "x-cc-run-id": cached["run_id"]})
        return self.base + wps.CHAT_PATH, headers, outgoing

    async def chat(self, body, session):
        key = (self.identity, self.version, body["model"], session)
        try:
            async with aclosing(self._chat(body, session)) as frames:
                async for frame in frames:
                    yield frame
        except UpstreamError as failure:
            # A transient network/queue failure does not invalidate a native
            # session. In particular, a late creator must never delete the
            # winning process's newly published session after losing its lease.
            if failure.code in {"invalid_credentials", "identity", "permission"}:
                self.cache.pop(key, None)
                if getattr(self, 'shared_cache', None):
                    await asyncio.to_thread(self.shared_cache.delete, hashlib.sha256(encode(key)).hexdigest())
            raise

    async def _chat(self, body, session):
        url, headers, outgoing = await self.prepare(body, session)
        translator = wps_protocol.FrameTranslator(body["model"])
        finish_deadline = None
        reported_usage = {}
        async with aclosing(self.t.sse(url, headers, encode(outgoing))) as frames:
            while True:
                try:
                    if finish_deadline is None:
                        frame = await anext(frames)
                    else:
                        remaining = finish_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        frame = await asyncio.wait_for(anext(frames), remaining)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    if translator.finished:
                        break
                    raise
                except UpstreamError as failure:
                    if translator.finished and failure.code in {"network", "truncated_stream", "upstream_timeout"}:
                        break
                    raise
                failure = frame_failure(frame)
                if failure:
                    raise failure
                classified_frame = frame
                if str(frame.get("code")).lower() in {"0", "200", "000000", "success"}:
                    classified_frame = {k: v for k, v in frame.items() if k != "code"}
                error = wps_protocol.frame_error(classified_frame)
                if error or str(frame.get("code")) == "40331":
                    raise classify(business=error[0] if error else "40331")
                if frame.get("_done"):
                    break
                for raw in translator.feed(frame):
                    for line in raw.decode().splitlines():
                        if line.startswith("data: ") and line[6:] != "[DONE]":
                            yield json.loads(line[6:])
                raw_usage = frame.get("usage")
                if isinstance(raw_usage, dict):
                    for name, detail in (("prompt_tokens", None), ("completion_tokens", None),
                                         ("cached_tokens", "prompt_tokens_details"),
                                         ("reasoning_tokens", "completion_tokens_details")):
                        nested = raw_usage.get(detail)
                        value = raw_usage.get(name, nested.get(name) if isinstance(nested, dict) else None)
                        # Usage snapshots can arrive in different frames. Only
                        # a reported counter, including explicit zero, updates it.
                        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18:
                            value = int(value)
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            reported_usage[name] = value
                if translator.finished and finish_deadline is None:
                    finish_deadline = time.monotonic() + 1
        if not translator.finished:
            raise UpstreamError("truncated_stream", 502, True)
        translator.usage = {name: reported_usage[name] for name in ("prompt_tokens", "completion_tokens") if name in reported_usage}
        if len(translator.usage) == 2:
            translator.usage["total_tokens"] = sum(translator.usage.values())
        for counter, detail in (("cached_tokens", "prompt_tokens_details"),
                                ("reasoning_tokens", "completion_tokens_details")):
            if counter in reported_usage:
                translator.usage[detail] = {counter: reported_usage[counter]}
        for raw in translator.tail():
            for line in raw.decode().splitlines():
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    yield json.loads(line[6:])
        yield {"_done": True}


ADAPTERS = {"Loomy": Loomy, "小浣熊": Raccoon, "WPS 灵犀": WPS, "WorkBuddy 国内": WorkBuddy, "WorkBuddy 国际": WorkBuddyInternational, "CodeArts": CodeArts, "Marvis": Marvis}


class Registry:
    def __init__(self, enabled=False, transport_factory=None, session_cache=None):
        self.enabled = enabled
        self.transport_factory = transport_factory or Transport
        self.wps_cache = {}
        from .session_cache import SessionLocks
        self.wps_cache_locks = SessionLocks()
        self.session_cache = session_cache

    def capabilities(self):
        return {name: {"implemented": sorted(cls.capabilities), "network_enabled": self.enabled, "live_verified": False} for name, cls in ADAPTERS.items()}

    def make(self, platform, credential, identity, version=1, proxy=None):
        if not self.enabled:
            raise UpstreamError("network_execution_disabled", 503)
        cls = ADAPTERS.get(platform)
        if cls is None:
            raise UpstreamError("adapter_unavailable", 501)
        adapter = cls(self.transport_factory(proxy=proxy), credential, identity, version)
        if isinstance(adapter, WPS):
            adapter.cache = self.wps_cache
            adapter.cache_locks = self.wps_cache_locks
            adapter.shared_cache = self.session_cache
        return adapter
