"""Credential-free HTTPS checks through explicitly selected proxy nodes.

This records connectivity evidence, never account/model availability. It does
not disable a node, change account bindings, or scan nodes on its own.
"""
import asyncio
import hashlib
import json
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import anyio
import httpx
from sqlalchemy import select

from .models import Resource
from .store import DomainError, uid


TARGET_URLS = {
    "Loomy": "https://loomyad.xunfei.cn",
    "小浣熊": "https://xiaohuanxiong.com",
    "WPS 灵犀": "https://lingxi.wps.cn",
    "WorkBuddy 国内": "https://copilot.tencent.com",
    "WorkBuddy 国际": "https://www.workbuddy.ai",
    "CodeArts": "https://snap-access.cn-north-4.myhuaweicloud.com",
    "Marvis": "https://yybadaccess.3g.qq.com",
}
# These fixed origins expose business APIs without a public root page.
# An HTTPS 404 there proves target connectivity only.
API_ONLY_ROOTS = {
    "Loomy": "https://loomyad.xunfei.cn",
    "WorkBuddy 国内": "https://copilot.tencent.com",
}
KIND = "proxy_target_check"
MAX_BATCH_CHECKS = 256
TARGET_GROUPS = {platform: (url,) for platform, url in TARGET_URLS.items()}
TARGET_GROUPS["WPS 灵犀"] = (TARGET_URLS["WPS 灵犀"], "https://lingxi.kdocs.cn")


@dataclass(frozen=True, repr=False)
class Snapshot:
    proxy_id: str
    node_version: int
    endpoint: str
    digest: str
    platform: str
    check_id: str
    started_at: float


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _http_code(status):
    if status == 401:
        return "target_auth_required"
    if status == 403:
        return "target_forbidden"
    if status == 429:
        return "target_rate_limited"
    if status >= 500:
        return "target_server_error"
    if status >= 400:
        return "target_http_error"
    if status >= 300:
        return "target_redirect"
    return "target_http_ok"


def _error_code(error):
    seen = set()
    chain = []
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    # SSLWantRead/Write are normal nonblocking handshake states. They often
    # remain in a timeout's context and are not evidence of a TLS failure.
    # A real certificate rejection is explicit; otherwise preserve timeout
    # taxonomy before considering fatal SSL exceptions deeper in the chain.
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain):
        return "target_tls_error"
    if any(isinstance(item, httpx.ConnectTimeout) for item in chain):
        return "proxy_connect_timeout"
    if any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in chain):
        return "target_timeout"
    if any(isinstance(item, ssl.SSLError) and not isinstance(item, (ssl.SSLWantReadError, ssl.SSLWantWriteError)) for item in chain):
        return "target_tls_error"
    if any(isinstance(item, socket.gaierror) for item in chain):
        return "proxy_dns_error"
    if isinstance(error, httpx.ProxyError):
        return "proxy_connect_rejected"
    if isinstance(error, httpx.ConnectError):
        return "proxy_connect_error"
    if isinstance(error, httpx.RemoteProtocolError):
        return "target_protocol_error"
    return "network_error"


class ProxyTargetHealth:
    def __init__(self, store, *, enabled=True, concurrency=2, timeout=15, client_factory=httpx.AsyncClient):
        if not 1 <= concurrency <= 8 or not 0 < timeout <= 30:
            raise ValueError("invalid_probe_limits")
        self.store, self.enabled = store, enabled
        self.timeout, self.client_factory = timeout, client_factory
        self.semaphore = asyncio.Semaphore(concurrency)

    def _begin(self, proxy_id, platform):
        with self.store.tx() as (s, state):
            node = s.get(Resource, proxy_id)
            if node is None or node.kind != "proxy" or not node.secret:
                raise DomainError("proxy_not_found", 404)
            if json.loads(node.data).get("status") in {"disabled","retired","pending_kernel"}:
                raise DomainError("proxy_unavailable",503)
            endpoint = self.store.cipher.decrypt(node.secret.encode()).decode()
            try:
                parsed = urlsplit(endpoint)
                if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or not parsed.port or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                    raise ValueError()
            except ValueError:
                raise DomainError("invalid_proxy_url", 422) from None
            name = f"{proxy_id}:{platform}"
            row = s.scalar(select(Resource).where(Resource.kind == KIND, Resource.name == name))
            if row is None:
                row = Resource(id=uid(), kind=KIND, name=name, data="{}", updated_at=self.store.clock())
                s.add(row)
            else:
                row.version += 1
            snapshot = Snapshot(proxy_id, node.version, endpoint, _digest(endpoint), platform, uid(), self.store.clock())
            previous=json.loads(row.data)
            if previous.get("status")=="completed" and previous.get("node_version")==node.version:
                # A refresh does not withdraw still-fresh prior evidence while
                # the bounded request is in flight. Consumers enforce its TTL.
                data=previous | {"inflight_check_id":snapshot.check_id,"inflight_started_at":snapshot.started_at}
            else:
                data=self._base(snapshot) | {"status": "running", "code": "checking", "inflight_check_id":snapshot.check_id,"inflight_started_at":snapshot.started_at}
            row.data = json.dumps(data)
            row.updated_at = self.store.clock()
            self.store.emit(s, state, KIND, "started")
            return snapshot

    def _base(self, snapshot):
        return {"proxy_id": snapshot.proxy_id, "node_version": snapshot.node_version,
                "platform": snapshot.platform, "target_url": TARGET_URLS[snapshot.platform],
                "target_urls": list(TARGET_GROUPS[snapshot.platform]), "verification_scope": "https_target_origins_only",
                "method": "HEAD", "check_id": snapshot.check_id, "started_at": snapshot.started_at,
                "checked_at": None, "tcp_reachable": None, "target_http_reachable": False,
                "http_status": None, "model_verified": False, "account_verified": False,
                "business_verified": False, "eligible": False, "latency_ms": None, "targets": []}

    def _finish(self, snapshot, result):
        with self.store.tx() as (s, state):
            name = f"{snapshot.proxy_id}:{snapshot.platform}"
            row = s.scalar(select(Resource).where(Resource.kind == KIND, Resource.name == name))
            if row is None or json.loads(row.data).get("inflight_check_id") != snapshot.check_id:
                return self._base(snapshot) | {"status": "stale", "code": "check_superseded", "stored": False}
            node = s.get(Resource, snapshot.proxy_id)
            current = self.store.cipher.decrypt(node.secret.encode()).decode() if node is not None and node.kind == "proxy" and node.secret else None
            if node is None or node.version != snapshot.node_version or current is None or _digest(current) != snapshot.digest or json.loads(node.data).get("status") in {"disabled","retired","pending_kernel"}:
                result = self._base(snapshot) | {"status": "stale", "code": "proxy_changed", "checked_at": self.store.clock()}
            else:
                result = {**result, "checked_at": self.store.clock()}
            row.data = json.dumps(result)
            row.version += 1
            row.updated_at = self.store.clock()
            self.store.emit(s, state, KIND, result["code"])
            return result | {"stored": True}

    async def _probe(self, snapshot):
        result = self._base(snapshot) | {"status": "completed", "code": "network_error"}
        started = time.monotonic()

        active_target = None

        async def trace(event, info):
            # HTTPX/httpcore trace values may contain addresses; never store or
            # print info. Only evidence that the proxy TCP connection completed.
            if event == "connection.connect_tcp.complete":
                active_target["tcp_reachable"] = True
            elif event == "connection.connect_tcp.failed" and active_target["tcp_reachable"] is None:
                active_target["tcp_reachable"] = False

        client = None
        try:
            client = self.client_factory(proxy=snapshot.endpoint, verify=True, trust_env=False,
                follow_redirects=False, timeout=httpx.Timeout(min(self.timeout, 10), connect=min(self.timeout, 5)),
                headers={"User-Agent": "Unified-Platform-Target-Check/1", "Accept": "*/*"})
            async with asyncio.timeout(self.timeout):
                for target_url in TARGET_GROUPS[snapshot.platform]:
                    active_target = {"url": target_url, "method": "HEAD", "tcp_reachable": None,
                                     "target_http_reachable": False, "http_status": None,
                                     "code": "network_error", "eligible": False, "redirect_same_origin": None,
                                     "body_bytes_read": 0}
                    result["targets"].append(active_target)
                    try:
                        for method in ("HEAD", "GET"):
                            active_target["method"] = method
                            # Header-only streaming: never consume or persist
                            # the target body. GET fallback is strictly once,
                            # carries a byte range, and reads zero body bytes.
                            headers = {"Range": "bytes=0-8191"} if method == "GET" else None
                            async with client.stream(method, target_url, headers=headers, extensions={"trace": trace}) as response:
                                status = response.status_code
                                active_target.update(tcp_reachable=True, target_http_reachable=True,
                                                     http_status=status, code=_http_code(status))
                                if method == "HEAD" and status == 405:
                                    continue
                                active_target["eligible"] = 200 <= status < 400
                                if status == 404 and target_url == API_ONLY_ROOTS.get(snapshot.platform):
                                    active_target.update(eligible=True, code="target_root_not_found")
                                if 300 <= status < 400:
                                    location = response.headers.get("location")
                                    try:
                                        redirect = urlsplit(urljoin(target_url, location or ""))
                                        origin = urlsplit(target_url)
                                        same = bool(location) and redirect.scheme == "https" and redirect.hostname == origin.hostname and (redirect.port or 443) == (origin.port or 443) and not redirect.username
                                    except ValueError:
                                        same = False
                                    active_target["redirect_same_origin"] = same
                                    active_target["eligible"] = same
                                    if not same:
                                        active_target["code"] = "target_external_redirect" if location else "target_redirect_missing_location"
                                break
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        active_target["code"] = _error_code(error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            result["code"] = _error_code(error)
            if active_target is not None:
                active_target["code"] = result["code"]
        finally:
            if client is not None:
                # Closing must be bounded and must not turn a completed probe
                # into an unrecorded result when its caller is cancelled.
                with anyio.CancelScope(shield=True):
                    with anyio.move_on_after(3):
                        try:
                            await client.aclose()
                        except Exception:
                            pass
            result["latency_ms"] = round((time.monotonic() - started) * 1000)
        if result["targets"]:
            targets = result["targets"]
            result["tcp_reachable"] = True if all(x["tcp_reachable"] is True for x in targets) else False if any(x["tcp_reachable"] is False for x in targets) else None
            result["target_http_reachable"] = len(targets) == len(TARGET_GROUPS[snapshot.platform]) and all(x["target_http_reachable"] for x in targets)
            result["eligible"] = len(targets) == len(TARGET_GROUPS[snapshot.platform]) and all(x["eligible"] for x in targets)
            decisive = next((x for x in targets if not x["eligible"]), targets[0])
            result["http_status"], result["code"], result["method"] = decisive["http_status"], decisive["code"], decisive["method"]
        return result

    async def check(self, proxy_id, platform):
        if not self.enabled:
            raise DomainError("network_execution_disabled", 409)
        if platform not in TARGET_URLS:
            raise DomainError("unsupported_platform", 422)
        async with self.semaphore:
            pending = asyncio.create_task(asyncio.to_thread(self._begin, proxy_id, platform))
            snapshot = None
            try:
                snapshot = await asyncio.shield(pending)
                result = await self._probe(snapshot)
                with anyio.CancelScope(shield=True):
                    return await asyncio.to_thread(self._finish, snapshot, result)
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    if snapshot is None:
                        try:
                            snapshot = await pending
                        except Exception:
                            raise asyncio.CancelledError() from None
                    result = self._base(snapshot) | {"status": "cancelled", "code": "check_cancelled"}
                    await asyncio.to_thread(self._finish, snapshot, result)
                raise

    async def check_many(self, proxy_ids, platforms):
        ids, targets = list(dict.fromkeys(proxy_ids)), list(dict.fromkeys(platforms))
        if not ids or not targets or len(ids) * len(targets) > MAX_BATCH_CHECKS:
            raise DomainError("invalid_proxy_check_batch", 422)
        if any(target not in TARGET_URLS for target in targets):
            raise DomainError("unsupported_platform", 422)

        async def one(proxy_id, platform):
            try:
                return await self.check(proxy_id, platform)
            except DomainError as error:
                return {"proxy_id": proxy_id, "platform": platform, "code": error.code,
                        "status": "rejected", "stored": False, "model_verified": False}

        return await asyncio.gather(*(one(proxy_id, platform) for proxy_id in ids for platform in targets))
