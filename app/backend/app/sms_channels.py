"""Durable SMS channel/number ledger; no purchase on configuration or listing."""
import asyncio
import hashlib
import json
import re
from contextlib import nullcontext
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from .adapters import required
from .models import Resource
from .store import DomainError, uid
from .upstream import UpstreamError, amount


class TokenProject(BaseModel):
    model_config=ConfigDict(extra="forbid")
    token: SecretStr
    project: str=Field(min_length=1,max_length=100)


class ChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1,max_length=100)
    provider: str
    base_url: str
    token: SecretStr | None = None
    tokens: list[SecretStr] | None = Field(default=None, max_length=100)
    pairs: list[TokenProject] | None = Field(default=None,max_length=500)
    projects: list[str] = Field(default_factory=list, max_length=100)
    project: str = ""
    keyword: str = ""
    keyword_get: str = ""
    keyword_msg: str = ""
    card_type: str = "全部"
    isp: str = ""
    author: str = ""
    upstream_uid: str = ""


class ChannelUpdate(ChannelInput):
    token: SecretStr | None = None
    expected_version: int = Field(ge=1)
    enabled: bool = True


class CredentialChannelInput(ChannelInput):
    user: SecretStr = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=1024)


class ProviderLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: SecretStr = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=1024)
    expected_version: int = Field(ge=1)
    remember: bool = False


CODE_WORDS = r"(?:验证码|校验码|动态码|确认码)"


def extract_code(text):
    """Same order as the live clients: bare code; code after the keyword; code before it
    (Tencent: "【签名】123456为您的登录验证码"); a lone 4-8 digit run only as the last resort."""
    text = (text or "").strip()
    found = re.fullmatch(r"(\d{4,8})", text)
    found = found or re.search(CODE_WORDS + r"[^\d]{0,12}?(?<!\d)(\d{4,8})(?!\d)", text)
    found = found or re.search(r"(?<!\d)(\d{4,8})(?!\d)[^\d]{0,12}?" + CODE_WORDS, text)
    if not found:
        runs = re.findall(r"(?<!\d)\d{4,8}(?!\d)", text)
        if len(runs) == 1 and not re.search(r"\d{9,}", text):
            return runs[0]
        return None
    return found.group(1)


class SmsChannels:
    def __init__(self,store,registry):
        self.store,self.registry = store,registry
        #: 同一通道两次取号之间的最小间隔。eomsg 对高频取号会直接拒绝（sms_rate_limit），
        #: 批量注册并行开 4 路时很容易撞上；串行化只发生在取号这一步，不影响后续等待收码。
        self.buy_interval = 2.0

    async def credential_login(self, channel_id, body):
        """Exchange credentials only on explicit request, never purchase a number.

        The network request runs outside the transaction. CAS prevents a late
        login response replacing a channel edited while the request was running.
        """
        channel = await asyncio.to_thread(self.read, channel_id)
        if channel["provider"] != "haozhuma":
            raise DomainError("sms_provider_login_unavailable", 422)
        with self.store.read() as (s, _):
            row = s.get(Resource, channel_id)
            if row.version != body.expected_version:
                raise DomainError("resource_version_conflict")
        if channel.get("pairs") or len(channel.get("tokens", [])) > 1:
            raise DomainError("sms_login_requires_single_token_channel", 422)
        user, password = body.user.get_secret_value().strip(), body.password.get_secret_value()
        if not user or not password:
            raise DomainError("missing_material", 422)
        result = await self.call({**channel, "token": ""}, "login", user=user, **{"pass": password})
        token = result.get("token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise UpstreamError("sms_login_token_missing")
        with self.store.tx() as (s, state):
            row = s.get(Resource, channel_id)
            if not row or row.kind != "sms_channel":
                raise DomainError("sms_channel_not_found", 404)
            if row.version != body.expected_version:
                raise DomainError("resource_version_conflict")
            private = json.loads(self.store.cipher.decrypt(row.secret.encode()))
            # An account login cannot safely replace a multi-account pair set.
            if private.get("pairs") or len(private.get("tokens", [])) > 1:
                raise DomainError("sms_login_requires_single_token_channel", 422)
            private.update(token=token.strip(), tokens=[token.strip()])
            if body.remember:
                private["provider_login"] = {"user": user, "password": password}
            else:
                private.pop("provider_login", None)
            row.secret = self.store.cipher.encrypt(json.dumps(private).encode()).decode()
            data = json.loads(row.data)
            data.update(token_count=1, provider_login_saved=body.remember, token_updated_at=self.store.clock())
            row.data, row.updated_at, row.version = json.dumps(data), self.store.clock(), row.version+1
            self.store.emit(s, state, "sms", "provider_token_updated")
            return {"id": row.id, "version": row.version, "status": "token_updated"}

    def reserve_buy(self, channel_id):
        """Reserve a provider call slot across app/task processes atomically."""
        with self.store.tx() as (s, _):
            now = self.store.clock()
            row = s.scalar(select(Resource).where(Resource.kind == "sms_buy_cursor", Resource.name == channel_id))
            next_at = json.loads(row.data).get("next_at", now) if row else now
            slot = max(now, next_at)
            if row is None:
                row = Resource(id=uid(), kind="sms_buy_cursor", name=channel_id, data="{}", updated_at=now)
                s.add(row)
            row.data, row.updated_at = json.dumps({"next_at": slot + self.buy_interval}), now
            return max(0, slot - now)

    async def throttle(self, channel_id):
        """Wait outside the DB transaction; concurrent callers get distinct slots."""
        if self.buy_interval <= 0:
            return
        wait = await asyncio.to_thread(self.reserve_buy, channel_id)
        if wait > 0:
            await asyncio.sleep(wait)

    def configure(self,body,channel_id=None,transaction=None):
        base = urlsplit(body.base_url)
        if body.provider not in {"haozhuma","eomsg"} or base.scheme != "https" or not base.hostname or base.username or base.query or base.fragment:
            raise DomainError("invalid_sms_channel",422)
        if body.provider == "haozhuma" and not body.project and not body.pairs or body.provider == "eomsg" and not (body.keyword or body.keyword_get and body.keyword_msg):
            raise DomainError("missing_material",422)
        secret = body.model_dump(exclude={"token","tokens","pairs","expected_version","enabled","user","password"})
        with (nullcontext(transaction) if transaction is not None else self.store.tx()) as (s,state):
            row=s.get(Resource,channel_id) if channel_id else None
            if channel_id:
                if not row or row.kind!="sms_channel":
                    raise DomainError("sms_channel_not_found",404)
                if row.version!=body.expected_version:
                    raise DomainError("resource_version_conflict")
                previous=json.loads(self.store.cipher.decrypt(row.secret.encode()))
                if previous.get("provider_login") and previous.get("provider")==body.provider and previous.get("base_url")==body.base_url:
                    secret["provider_login"]=previous["provider_login"]
                for key in ("author","upstream_uid","keyword_get","keyword_msg"):
                    if key not in body.model_fields_set:
                        secret[key]=previous.get(key,"")
                changed=any(previous.get(k," ").strip()!=secret.get(k," ").strip() for k in ("provider","base_url","project","keyword","keyword_get","keyword_msg"))
                if changed:
                    for lease in s.scalars(select(Resource).where(Resource.kind=="sms_lease")):
                        data=json.loads(lease.data)
                        if data.get("channel_id")==channel_id and data["status"] not in {"retired","provider_auto_release"}:
                            raise DomainError("sms_channel_has_active_numbers")
                if "projects" not in body.model_fields_set:
                    secret["projects"] = previous.get("projects", [])
                secret["tokens"] = ([t.get_secret_value().strip() for t in body.tokens] if body.tokens is not None else [body.token.get_secret_value().strip()] if body.token else previous.get("tokens") or [previous["token"]])
                secret["pairs"]=previous.get("pairs",[]) if "pairs" not in body.model_fields_set else []
                row.version+=1
            else:
                if not body.token and not body.tokens and not body.pairs:
                    raise DomainError("missing_material",422)
                secret["tokens"] = [t.get_secret_value().strip() for t in body.tokens] if body.tokens is not None else [body.token.get_secret_value().strip()] if body.token else []
                row=Resource(id=uid(),kind="sms_channel",name=body.name,data="{}",updated_at=self.store.clock())
                s.add(row)
            if body.pairs is not None:
                if body.provider!="haozhuma":raise DomainError("sms_pairs_require_haozhuma",422)
                secret["pairs"]=[{"token":p.token.get_secret_value().strip(),"project":p.project} for p in body.pairs]
            if secret.get("pairs"):
                secret["tokens"]=[p["token"] for p in secret["pairs"]]
                secret["project"]=secret["pairs"][0]["project"]
            secret["tokens"] = list(dict.fromkeys(secret["tokens"]))
            if not secret["tokens"] or not all(secret["tokens"]):
                raise DomainError("missing_material",422)
            secret["token"] = secret["tokens"][0]
            row.name=body.name
            row.data=json.dumps({k:v for k,v in secret.items() if k not in {"token","tokens","pairs","author","upstream_uid","provider_login"}}|{"host":base.hostname,"enabled":getattr(body,"enabled",True),"token_count":len(secret["tokens"]),"pair_count":len(secret.get("pairs",[])),"provider_login_saved":bool(secret.get("provider_login")),"paired_projects":list(dict.fromkeys(p["project"] for p in secret.get("pairs",[])))})
            row.secret=self.store.cipher.encrypt(json.dumps(secret).encode()).decode()
            row.updated_at=self.store.clock()
            self.store.emit(s,state,"sms","channel_saved")
            return row.id

    def read(self,channel_id,for_new=False):
        with self.store.read() as (s,_):
            row = s.get(Resource,channel_id)
            if not row or row.kind != "sms_channel":
                raise DomainError("sms_channel_not_found",404)
            if for_new and json.loads(row.data).get("enabled") is False:
                raise DomainError("sms_channel_disabled")
            return {**json.loads(self.store.cipher.decrypt(row.secret.encode())), "_channel_id": channel_id,
                    "_channel_version": row.version}

    async def refresh_provider_token(self, channel):
        """Single account reauthentication across processes, with no purchase.

        A provider's explicit credential rejection permits one replay only after
        the replacement token is durably stored. Network ambiguity never enters
        this path. Existing multi-token channels retain their rotation policy.
        """
        channel_id = channel.get("_channel_id")
        if not channel_id or channel.get("provider") != "haozhuma" or not channel.get("provider_login"):
            raise UpstreamError("sms_channel_credentials_invalid", 401)
        operation = uid()
        with self.store.tx() as (s, _):
            row = s.get(Resource, channel_id)
            current = json.loads(self.store.cipher.decrypt(row.secret.encode()))
            if current.get("token") != channel.get("token"):
                return {**current, "_channel_id": channel_id}
            credentials = current.get("provider_login")
            if not credentials or current.get("pairs") or len(current.get("tokens", [])) > 1:
                raise UpstreamError("sms_channel_credentials_invalid", 401)
            lock = s.scalar(select(Resource).where(Resource.kind == "sms_login_lock", Resource.name == channel_id))
            lock_data = json.loads(lock.data) if lock else {}
            if lock_data.get("lease_until", 0) > self.store.clock():
                raise UpstreamError("sms_provider_login_busy", 409, True)
            if lock is None:
                lock = Resource(id=uid(), kind="sms_login_lock", name=channel_id, data="{}", updated_at=self.store.clock())
                s.add(lock)
            lock.data = json.dumps({"owner": operation, "lease_until": self.store.clock()+40})
            version = row.version
        try:
            response = await self._call({**current, "token": ""}, "login", user=credentials["user"], **{"pass": credentials["password"]})
            token = response.get("token") if isinstance(response, dict) else None
            if not isinstance(token, str) or not token.strip():
                raise UpstreamError("sms_login_token_missing")
            with self.store.tx() as (s, state):
                row = s.get(Resource, channel_id)
                if row.version != version:
                    raise DomainError("resource_version_conflict")
                lock = s.scalar(select(Resource).where(Resource.kind == "sms_login_lock", Resource.name == channel_id))
                saved_lock = json.loads(lock.data) if lock else {}
                if saved_lock.get("owner") != operation or saved_lock.get("lease_until", 0) <= self.store.clock():
                    raise DomainError("sms_provider_login_lease_lost")
                current.update(token=token.strip(), tokens=[token.strip()])
                row.secret = self.store.cipher.encrypt(json.dumps(current).encode()).decode()
                row.version, row.updated_at = row.version+1, self.store.clock()
                data = json.loads(row.data)
                data["token_updated_at"] = self.store.clock()
                row.data = json.dumps(data)
                self.store.emit(s, state, "sms", "provider_token_updated")
            return {**current, "_channel_id": channel_id}
        finally:
            with self.store.tx() as (s, _):
                lock = s.scalar(select(Resource).where(Resource.kind == "sms_login_lock", Resource.name == channel_id))
                if lock and json.loads(lock.data).get("owner") == operation:
                    lock.data = "{}"
                    lock.updated_at = self.store.clock()

    def token_balances(self, channel_id, channel):
        """最近一次余额查询里每个令牌的余额（index 对应 tokens 顺序）。

        eomsg 按令牌账户计费：能取到号不代表能取到码，余额几分的令牌取码会返回
        「账户余额不足收取此项目信息」。取号和取码都优先用有钱的令牌。
        """
        tokens = list(dict.fromkeys(channel.get("tokens") or [channel["token"]]))
        row = next((r for r in self.store.resources("sms_balance") if r["name"] == channel_id), None)
        if not row or row["data"].get("channel_version") != channel.get("_channel_version"):
            return {}
        result = {}
        for entry in (row["data"].get("tokens") or []):
            index = entry.get("index")
            if entry.get("status") == "verified" and isinstance(index, int) and 1 <= index <= len(tokens):
                result[tokens[index - 1]] = float(entry.get("balance") or 0)
        return result

    def candidates(self, channel_id, channel, rotate=False):
        tokens = channel.get("tokens") or [channel["token"]]
        projects = list(dict.fromkeys([channel["project"], *channel.get("projects", [])])) if channel["provider"] == "haozhuma" else [channel["project"]]
        variants = [{**channel,**pair} for pair in channel["pairs"]] if channel.get("pairs") else [{**channel, "token": token, "project": project} for token in tokens for project in projects]
        balances = self.token_balances(channel_id, channel) if channel["provider"] == "eomsg" else {}
        if balances:
            # 有钱的先试；余额未知的排后但保留（可能只是还没跑过余额查询）。
            variants.sort(key=lambda v: -(balances.get(v["token"], -1.0)))
        if rotate and len(variants) > 1:
            with self.store.tx() as (s, _):
                row = s.scalar(select(Resource).where(Resource.kind == "sms_cursor", Resource.name == channel_id))
                cursor = json.loads(row.data).get("next", 0) if row else 0
                if row is None:
                    row = Resource(id=uid(), kind="sms_cursor", name=channel_id, data="{}", updated_at=self.store.clock())
                    s.add(row)
                row.data = json.dumps({"next": (cursor+1) % len(variants)})
                # 轮转只在前几个有钱令牌之间进行，避免又转回穷令牌。
                paid = [v for v in variants if balances.get(v["token"], 0) >= 1.0]
                pool = paid or variants
                # Ranking is a preference, not proof that other tokens are
                # unusable. Retain unknown/depleted tokens as fallbacks after
                # explicit refusals (the last balance sample may be stale).
                tail = [v for v in variants if v not in pool]
                variants = pool[cursor % len(pool):] + pool[:cursor % len(pool)] + tail
        return variants

    async def balances(self, channel_id):
        """Query each credential once; project IDs share the same provider wallet."""
        channel=await asyncio.to_thread(self.read,channel_id)
        started_at=self.store.clock()
        tokens=list(dict.fromkeys(channel.get("tokens") or [channel["token"]]))
        rows=[]
        for index,token in enumerate(tokens,1):
            try:
                value=await self.call({**channel,"token":token},"getSummary" if channel["provider"]=="haozhuma" else "leftAmount")
                balance=amount(value.get("money") if isinstance(value,dict) else value)
                if balance is None:
                    raise UpstreamError("sms_balance_invalid")
                rows.append({"index":index,"status":"verified","balance":balance,"checked_at":self.store.clock()})
            except UpstreamError as error:
                rows.append({"index":index,"status":"failed","balance":None,"error":error.code,"checked_at":self.store.clock()})
        verified=[row for row in rows if row["status"]=="verified"]
        subtotal=sum(row["balance"] for row in verified)
        result={"status":"verified" if len(verified)==len(rows) else "partial" if verified else "failed",
                "balance":subtotal if len(verified)==len(rows) else None,"verified_subtotal":subtotal if verified else None,
                "verified_tokens":len(verified),"token_count":len(rows),"tokens":rows,"checked_at":self.store.clock(),
                "channel_version":channel["_channel_version"],"started_at":started_at}
        return await asyncio.to_thread(self.save_balance,channel_id,result)

    def save_balance(self,channel_id,result):
        """Fence token order changes and older, slower wallet observations."""
        with self.store.tx() as (s,state):
            channel=s.get(Resource,channel_id)
            if not channel or channel.kind!="sms_channel":
                raise DomainError("sms_channel_not_found",404)
            if channel.version!=result["channel_version"]:
                raise DomainError("resource_version_conflict")
            row=s.scalar(select(Resource).where(Resource.kind=="sms_balance",Resource.name==channel_id))
            previous=json.loads(row.data) if row else {}
            if previous.get("channel_version")==channel.version and previous.get("started_at",0)>result["started_at"]:
                return previous
            if row is None:
                row=Resource(id=uid(),kind="sms_balance",name=channel_id,version=1,data="{}",updated_at=self.store.clock())
                s.add(row)
            else:
                row.version+=1
            row.data,row.updated_at=json.dumps(result),self.store.clock()
            self.store.emit(s,state,"sms_balance","balance_observed")
            return result

    def listing(self):
        channels=self.store.resources("sms_channel")
        versions={row["id"]:row["version"] for row in channels}
        balances=self.store.resources("sms_balance")
        for row in balances:
            if row["data"].get("channel_version")!=versions.get(row["name"]):
                # Keep the historical timestamp, never show another token's
                # wallet as the current balance after credentials are edited.
                row["data"]={**row["data"],"status":"stale","balance":None,"verified_subtotal":None,
                             "verified_tokens":0,"tokens":[],"error":"channel_changed"}
        return {"channels":channels,"balances":balances}

    @staticmethod
    def provider_error(message):
        # These providers expose textual errors rather than stable business codes.
        # Keep the original SMS/provider text out of public records.
        message = str(message).lower()
        if "等待" in message or "尚未收到" in message:
            return "sms_waiting"
        if "释放" in message or "占用" in message or "上限" in message:
            return "sms_number_limit"
        if "频繁" in message or "频率" in message or "too many" in message:
            return "sms_rate_limit"
        if "余额" in message or "充值" in message or "欠费" in message:
            return "sms_balance_insufficient"
        if "无号" in message or "没有号码" in message or "暂无" in message:
            return "sms_no_numbers"
        if "token" in message or "登录" in message or "令牌" in message:
            return "sms_channel_credentials_invalid"
        return "sms_provider_rejected"

    async def call(self,channel,action,**params):
        try:
            return await self._call(channel, action, **params)
        except UpstreamError as error:
            if error.code != "sms_channel_credentials_invalid" or action == "login" or not channel.get("provider_login"):
                raise
        replacement = await self.refresh_provider_token(channel)
        return await self._call(replacement, action, **params)

    async def _call(self,channel,action,**params):
        if not self.registry.enabled:
            raise DomainError("network_execution_disabled")
        transport = self.registry.transport_factory()
        query = {("api" if channel["provider"]=="haozhuma" else "code"):action,"token":channel["token"],**params}
        url = channel["base_url"] + "?" + urlencode({k:v for k,v in query.items() if v != ""})
        try:
            async with asyncio.timeout(25):
                async with transport.client.stream("GET",url,follow_redirects=False) as response:
                    if response.status_code in {401,403}:
                        raise UpstreamError("sms_channel_credentials_invalid",401)
                    if response.status_code == 429:
                        raise UpstreamError("sms_rate_limit",429)
                    await transport._check(response)
                    if 300 <= response.status_code < 400:
                        raise UpstreamError("unexpected_redirect")
                    raw = await transport._read(response,128*1024)
                    if channel["provider"] == "eomsg":
                        text = raw.decode("utf-8","replace").strip()
                        if text.startswith("ERROR:"):
                            code = self.provider_error(text)
                            if code == "sms_waiting" and action == "getMsg":
                                return None
                            raise UpstreamError(code)
                        return text
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        raise UpstreamError("invalid_response") from None
                    if not isinstance(value, dict):
                        raise UpstreamError("invalid_response")
                    if str(value.get("code")) not in {"0","200"}:
                        code = self.provider_error(value.get("msg", ""))
                        if action == "getMessage" and code == "sms_waiting":
                            return None
                        raise UpstreamError(code)
                    return value
        except (TimeoutError,httpx.TimeoutException):
            raise UpstreamError("upstream_timeout",504) from None
        except httpx.HTTPError:
            raise UpstreamError("network",502) from None
        finally:
            await transport.close()

    def update(self,lease_id,patch,secret_patch=None):
        with self.store.tx() as (s,state):
            row = s.get(Resource,lease_id)
            if not row or row.kind != "sms_lease":
                raise DomainError("sms_lease_not_found",404)
            row.data = json.dumps({**json.loads(row.data),**patch})
            if secret_patch is not None:
                old = json.loads(self.store.cipher.decrypt(row.secret.encode())) if row.secret else {}
                row.secret = self.store.cipher.encrypt(json.dumps({**old,**secret_patch}).encode()).decode()
            row.version,row.updated_at = row.version+1,self.store.clock()
            self.store.emit(s,state,"sms",patch.get("status","updated"))

    async def acquire(self,channel_id,session_id):
        channel = (await asyncio.to_thread(self.read, channel_id,for_new=True))
        # CAS is before the provider action: no double purchase on double click.
        with self.store.tx() as (s,state):
            existing = s.scalar(select(Resource).where(Resource.kind=="sms_lease",Resource.name==session_id))
            if existing:
                data = json.loads(existing.data)
                if data["status"] == "reserved":
                    return existing.id,json.loads(self.store.cipher.decrypt(existing.secret.encode()))["phone"]
                raise DomainError("sms_acquisition_requires_review")
            row = Resource(id=uid(),kind="sms_lease",name=session_id,data=json.dumps({"channel_id":channel_id,"session_id":session_id,"status":"acquiring","created_at":self.store.clock()}),updated_at=self.store.clock())
            s.add(row)
            lease_id=row.id
            self.store.emit(s,state,"sms","acquiring")
        try:
            candidates = await asyncio.to_thread(self.candidates, channel_id, channel, True)
            for index, selected in enumerate(candidates):
                try:
                    await self.throttle(channel_id)
                    if selected["provider"]=="haozhuma":
                        result = await self.call(selected,"getPhone",sid=selected["project"],isp=selected["isp"],author=selected["author"],uid=selected["upstream_uid"])
                        phone = required(result,"phone")
                    else:
                        phone = await self.call(selected,"getPhone",keyWord=selected.get("keyword_get") or selected["keyword"],cardType=selected["card_type"])
                    channel = selected
                    break
                except UpstreamError as error:
                    # Only a definitive refusal may try another token. A timeout
                    # may already have purchased a number and must stay uncertain.
                    if error.code not in {"sms_number_limit", "sms_balance_insufficient", "sms_no_numbers", "sms_channel_credentials_invalid", "sms_rate_limit"} or index+1 == len(candidates):
                        raise
            if not re.fullmatch(r"1\d{10}",phone):
                raise UpstreamError("invalid_phone_number")
            with self.store.tx() as (s,state):
                session=s.get(Resource,session_id)
                platform=json.loads(session.data).get("platform") if session is not None else "unknown"
                digest=self.store.phone_key(platform,phone)
                duplicate=s.scalar(select(Resource).where(Resource.kind=="phone_history",Resource.name==digest))
                rejected=self.store.phone_rejected(s,platform,phone) or self.store.phone_rejected(s,"unknown",phone)
                if not duplicate and not rejected:
                    s.add(Resource(id=uid(),kind="phone_history",name=digest,data=json.dumps({"platform":platform,"source":"acquired","session_id":session_id,"first_seen":self.store.clock()}),updated_at=self.store.clock()))
                self.store.emit(s,state,"sms","number_recorded")
            (await asyncio.to_thread(self.update, lease_id,{"status":"reserved","phone_suffix":phone[-4:],"duplicate":bool(duplicate)},{"phone":phone,"channel":{k:v for k,v in channel.items() if k != "tokens"}}))
            if rejected:
                # 平台已明确拒发过这个号：立刻退回，让批次补一个新号，别再发一条注定失败的短信。
                await self.retire(lease_id,"phone_rejected_previously")
                raise DomainError("phone_rejected_previously")
            if duplicate:
                await self.retire(lease_id,"duplicate_number")
                raise DomainError("phone_previously_used")
            return lease_id,phone
        except (DomainError,UpstreamError) as error:
            if error.code not in {"phone_previously_used","phone_rejected_previously"}:
                (await asyncio.to_thread(self.update, lease_id,{"status":"uncertain","error":error.code}))
            raise

    def existing_number(self,channel_id,session_id,phone):
        self.read(channel_id,for_new=True)
        with self.store.tx() as (s,state):
            row=s.scalar(select(Resource).where(Resource.kind=="sms_lease",Resource.name==session_id))
            if row:
                return row.id
            row=Resource(id=uid(),kind="sms_lease",name=session_id,data=json.dumps({"channel_id":channel_id,"session_id":session_id,"status":"reserved","existing_number":True,"created_at":self.store.clock(),"phone_suffix":phone[-4:]}),secret=self.store.cipher.encrypt(json.dumps({"phone":phone}).encode()).decode(),updated_at=self.store.clock())
            s.add(row)
            self.store.emit(s,state,"sms","existing_number_attached")
            return row.id

    def lease(self,lease_id):
        with self.store.read() as (s,_):
            row=s.get(Resource,lease_id)
            if not row or row.kind!="sms_lease":
                raise DomainError("sms_lease_not_found",404)
            return json.loads(row.data),json.loads(self.store.cipher.decrypt(row.secret.encode())) if row.secret else {}

    async def poll(self,lease_id):
        data,secret=(await asyncio.to_thread(self.lease, lease_id))
        if data["status"] not in {"reserved","code_received"}:
            raise DomainError("sms_lease_not_active")
        if data["status"]=="code_received":
            return secret["code"]
        with self.store.tx() as (s, _):
            row=s.get(Resource,lease_id)
            latest=json.loads(row.data)
            if latest.get("next_poll_at",0)>self.store.clock():
                return None
            latest["next_poll_at"]=self.store.clock()+5
            row.data=json.dumps(latest)
        channel=(await asyncio.to_thread(self.read, data["channel_id"]))
        preferred = secret.get("channel") or channel
        ordered = await asyncio.to_thread(self.candidates, data["channel_id"], channel)
        # 号码是项目级共享的，取码不必绑死在买号那个令牌上：先试它，再试其它有钱的。
        candidates = [preferred] + [c for c in ordered if (c.get("token"), c.get("project")) != (preferred.get("token"), preferred.get("project"))]
        if not data.get("existing_number"):
            candidates = candidates[:3]
        if secret.get("channel") and data.get("existing_number"):
            preferred=secret["channel"]
            candidates=[preferred]+[c for c in candidates if (c["token"],c["project"])!=(preferred["token"],preferred["project"])]
        errors=[]
        for index, candidate in enumerate(candidates):
            try:
                if candidate["provider"]=="haozhuma":
                    result=await self.call(candidate,"getMessage",sid=candidate["project"],phone=secret["phone"])
                    text=str(result.get("yzm") or result.get("sms") or "") if result else ""
                else:
                    text=await self.call(candidate,"getMsg",phone=secret["phone"],keyWord=candidate.get("keyword_msg") or candidate["keyword"]) or ""
                code=extract_code(text)
                if code and secret.get("rejected_code")!=code:
                    await asyncio.to_thread(self.update, lease_id,{"status":"code_received","code_received_at":self.store.clock(),"poll_error":None,"poll_error_at":None,"poll_all_failed":False},{"code":code,"channel":{k:v for k,v in candidate.items() if k != "tokens"}})
                    return code
            except UpstreamError as error:
                errors.append(error.code)
                # 该令牌余额不足只是这一个令牌的事，换下一个有钱的继续取，不要把会话判死。
                if error.code in {"sms_balance_insufficient","sms_channel_credentials_invalid"} and index + 1 < len(candidates):
                    continue
                if not data.get("existing_number"):
                    # An earlier successful "still waiting" response is real
                    # provider evidence; a later token failure cannot erase it.
                    await asyncio.to_thread(self.update,lease_id,{"poll_error":error.code,"poll_error_at":self.store.clock(),"poll_all_failed":len(errors)==index+1})
                    raise
        await asyncio.to_thread(self.update,lease_id,{"poll_error":errors[-1] if errors else None,"poll_error_at":self.store.clock() if errors else None,"poll_all_failed":len(errors)==len(candidates)})
        if len(errors)==len(candidates):
            raise UpstreamError(errors[-1])
        return None

    def reject_code(self,lease_id,code=None,*,fingerprint=None):
        """Reject only the submitted code, atomically with the active lease check.

        A delayed rejection must not discard a newer code or reopen a retired
        reservation. Browser callers persist the submitted hash before clicking.
        """
        expected=fingerprint or (hashlib.sha256(code.encode()).hexdigest() if code else None)
        if not expected:
            return False
        with self.store.tx() as (s,state):
            row=s.get(Resource,lease_id)
            if not row or row.kind!="sms_lease":
                raise DomainError("sms_lease_not_found",404)
            data=json.loads(row.data)
            private=json.loads(self.store.cipher.decrypt(row.secret.encode())) if row.secret else {}
            current=private.get("code")
            if data.get("status")!="code_received" or not current or hashlib.sha256(current.encode()).hexdigest()!=expected:
                return False
            data.update(status="reserved",last_code_rejected_at=self.store.clock())
            private["rejected_code"]=current
            row.data=json.dumps(data)
            row.secret=self.store.cipher.encrypt(json.dumps(private).encode()).decode()
            row.version,row.updated_at=row.version+1,self.store.clock()
            self.store.emit(s,state,"sms","reserved")
            return True

    async def retire(self,lease_id,reason):
        data,secret=(await asyncio.to_thread(self.lease, lease_id))
        if data["status"] in {"retired","provider_auto_release"}:
            return
        if not secret.get("phone"):
            raise DomainError("sms_acquisition_requires_review")
        with self.store.tx() as (s,_):
            registration=s.get(Resource,data["session_id"])
            if registration:
                login=json.loads(registration.data)
                material=json.loads(self.store.cipher.decrypt(registration.secret.encode())) if registration.secret else {}
                preflight_rejection = reason in {"duplicate_number", "phone_rejected_previously"} and data["status"] == "reserved" and login["status"] == "sending" and not login.get("result_unresolved") and not any(material.get(k) for k in ("issued_result", "issued_credential", "issued_token"))
                if not preflight_rejection and (login["status"] not in {"succeeded","failed","cancelled"} or login.get("result_unresolved") or any(material.get(k) for k in ("issued_result","issued_credential","issued_token"))):
                    raise DomainError("registration_result_unresolved")
        channel=secret.get("channel") or (await asyncio.to_thread(self.read, data["channel_id"]))
        if data.get("existing_number"):
            (await asyncio.to_thread(self.update, lease_id,{"status":"retired","reason":reason,"release_confirmed":False,"existing_number":True}))
            return
        if channel["provider"]=="eomsg":
            (await asyncio.to_thread(self.update, lease_id,{"status":"provider_auto_release","reason":reason,"release_confirmed":False}))
            return
        blacklisted=data.get("blacklist_confirmed",False)
        released=data.get("release_confirmed",False)
        # A platform refusal/timeout is not evidence that the phone is bad.
        # Successful and cancelled orders also only release their reservation.
        require_blacklist=reason in {"duplicate_number","invalid_phone_number"}
        if require_blacklist and not blacklisted:
            try:
                await self.call(channel,"addBlacklist",sid=channel["project"],phone=secret["phone"])
                blacklisted=True
            except UpstreamError:
                pass
        if not released:
            try:
                await self.call(channel,"cancelRecv",sid=channel["project"],phone=secret["phone"])
                released=True
            except UpstreamError:
                pass
        (await asyncio.to_thread(self.update, lease_id,{"status":"retired" if released and (not require_blacklist or blacklisted) else "retirement_unconfirmed","reason":reason,"blacklist_confirmed":blacklisted,"release_confirmed":released}))


def mount_sms_channels(app,service):
    @app.get("/api/sms/channels")
    def list_channels():
        return service.listing()

    @app.post("/api/sms/channels/credential-login", status_code=201)
    async def create_with_login(body:CredentialChannelInput):
        base = urlsplit(body.base_url)
        if body.provider != "haozhuma" or base.scheme != "https" or not base.hostname or base.username or base.query or base.fragment:
            raise DomainError("invalid_sms_channel", 422)
        if not body.project or body.tokens or body.pairs:
            raise DomainError("sms_login_requires_single_token_channel", 422)
        user, password = body.user.get_secret_value().strip(), body.password.get_secret_value()
        if not user or not password:
            raise DomainError("missing_material", 422)
        result = await service.call({"provider":body.provider,"base_url":body.base_url,"token":""}, "login", user=user, **{"pass":password})
        token = result.get("token") if isinstance(result,dict) else None
        if not isinstance(token,str) or not token.strip():
            raise UpstreamError("sms_login_token_missing")
        clean = ChannelInput(**body.model_dump(exclude={"user","password","token"}), token=SecretStr(token.strip()))
        return {"id":await asyncio.to_thread(service.configure,clean),"status":"token_updated"}

    @app.post("/api/sms/channels",status_code=201)
    def add(body:ChannelInput):
        return {"id":service.configure(body)}

    @app.put("/api/sms/channels/{channel_id}")
    def update(channel_id:str,body:ChannelUpdate):
        return {"id":service.configure(body,channel_id)}

    @app.post("/api/sms/channels/{channel_id}/balance")
    async def balance(channel_id:str):
        return await service.balances(channel_id)

    @app.post("/api/sms/channels/{channel_id}/credential-login")
    async def credential_login(channel_id:str, body:ProviderLogin):
        return await service.credential_login(channel_id, body)

    @app.post("/api/sms/leases/{lease_id}/retire")
    async def retire(lease_id:str):
        data,_=(await asyncio.to_thread(service.lease, lease_id))
        async with app.state.registration_service.execution(data["session_id"]):
            await service.retire(lease_id,"manual_retirement")
        return {"ok":True}
