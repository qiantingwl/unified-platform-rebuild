"""Bounded async transport. Errors contain categories, never upstream secrets/body."""
import asyncio
import json
import math
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import httpx


@dataclass
class UpstreamError(Exception):
    code: str
    status: int = 502
    retryable: bool = False
    retry_after: float = 0
    # A bounded transport category, never exception text, URL or headers.
    # Appended for compatibility with existing four-position constructors.
    failure_phase: str | None = None

    def __str__(self):
        return self.code


def transport_failure(error, *, response_read=False):
    """Keep public error codes stable while retaining connection evidence.

    A connect failure does not prove the proxy itself is broken: the target or
    tunnel handshake may be unavailable. Read/HTTP errors are not connection
    evidence, and pool timeouts describe local client capacity.
    """
    if isinstance(error, httpx.ProxyError):
        phase = "proxy_connect"
    elif isinstance(error, (httpx.ConnectTimeout, httpx.ConnectError)):
        phase = "connect"
    elif isinstance(error, httpx.PoolTimeout):
        phase = "local_pool"
    elif isinstance(error, (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)):
        phase = "read"
    elif isinstance(error, (httpx.WriteTimeout, httpx.WriteError)):
        phase = "write"
    else:
        phase = "transport_unknown"
    if response_read:
        return UpstreamError("response_read_failed", 502, True, failure_phase=phase)
    if isinstance(error, httpx.TimeoutException):
        return UpstreamError("upstream_timeout", 504, True, failure_phase=phase)
    return UpstreamError("network", 502, True, failure_phase=phase)


def classify(status=200, business=None, retry_after=0, message=None):
    code = str(business or "").strip()
    if code.lower() in {"auditfailed", "audit_blocked", "content_filter", "content_policy_violation"}:
        return UpstreamError("content_filter", 400)
    # Loomy 对「账号已被停用」回的是 HTTP200 + {"code":"100003"}（2026-11-17 实测：
    # 297 个 pending 里抽样 16 个有 15 个是这个码）。凭证没过期、余额列还留着
    # 迁移前的旧数，但取余额和对话都会被拒。以前没有这条判定，它落进 unknown 的
    # upstream_rejected：账户永远停在 pending、每个周期都白撞一次上游。
    # retryable 只为让当前请求换下一个号；账户本身由 store 落成 upstream disabled。
    if code == "100003":
        return UpstreamError("account_disabled", 403, True)
    if code in {"UserIdentityNotVerified", "40331"}:
        return UpstreamError("identity", 403, True)
    if status == 401 or code in {"020002", "100002", "200001", "200003", "-107", "-101", "12153"}:
        return UpstreamError("invalid_credentials", 401, True)
    if status == 402 or code.lower() in {"insufficient_points", "insufficient_quota", "insufficient_balance", "quota_exceeded", "no_quota", "insufficientquota", "insufficientbalance"}:
        return UpstreamError("quota", 402, True)
    # WorkBuddy 6020 is capacity queue.waiting, not an account quota/rate
    # penalty. Keep it retryable inside the existing startup/attempt budget;
    # fail_attempt records it without creating an account/model restriction.
    if code == "6020" and status in {200, 429}:
        return UpstreamError("upstream_queue_wait", 429, True)
    # 7864 distinguishes WorkBuddy's HTTP200 stream-error business codes.
    # 11140 is overloaded: only its explicit rate wording is transient; never
    # classify generated assistant text or a generic 403 by this wording.
    rate_11140 = code == '11140' and status in {200, 403, 429} and isinstance(message, str) and bool(re.search(r'\brate[ -]?limit(?:ing|ed)?\b|\btoo many requests\b', message[:4096], re.IGNORECASE))
    if status == 429 or (code == '6004' and status == 200) or code in {"rate_limit_exceeded", "TooManyRequests", "RateLimitExceeded"} or rate_11140 or (code == "limiter_request_error" and status not in {401, 402, 403}):
        return UpstreamError("rate_limit", 429, True, min(max(retry_after, 1), 3600))
    # The request itself was refused (Marvis 4100406, WPS InvalidArgument / context length):
    # another account would refuse it too, so no retry and no account restriction.
    # WorkBuddy: 11115 context too long; 11101 / 11128 / 11148 deterministic request-body errors.
    if code in {"4100406", "InvalidArgument", "invalid_request_error", "context_length_exceeded", "11115", "11101", "11128", "11148"}:
        return UpstreamError("request_rejected", 400)
    # WorkBuddy 11102 is model permission; HTTP403/11140 is a restricted
    # account response observed on both global samples. Keep the restriction
    # account x model, require a real answer for recovery, never toggle enabled.
    if code in {"ModelPermissionDenied", "PermissionDenied", "model_not_allowed", "11102"} or (code == '11140' and status in {200, 403}):
        return UpstreamError("permission", 403, True)
    if status == 403:
        return UpstreamError("unknown_403", 403, True)
    if status >= 500 or code in {"server_error", "overloaded_error", "api_error", "service_unavailable"}:
        return UpstreamError("upstream_5xx", 502, True)
    return UpstreamError("upstream_rejected", status if status >= 400 else 502)


def limiter_failure(node, status=200, retry_after=0, depth=6, explicit=False):
    """Recognize the Raccoon limiter contract only in error envelopes.

    Do not search generated choices/content or match generic limit/timeout
    prose. The body retry delay only applies after this exact limiter marker.
    """
    if not isinstance(node, dict) or depth <= 0 or status in {401, 402, 403}:
        return None
    code, kind = node.get("code"), node.get("type")
    failed = explicit or status >= 400 or kind == "error" or (code is not None and str(code).lower() not in {"0", "200", "000000", "success"})
    message = next((node[k][:4096] for k in ("message", "msg", "detail") if isinstance(node.get(k), str)), "")
    exact = code == "limiter_request_error" or kind == "limiter_request_error"
    if exact or failed and re.match(r"^\s*limiter_request_error(?:\s*:|\s*$)", message, re.IGNORECASE):
        retry = retry_after if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) and retry_after > 0 else 0
        if not retry:
            match = re.search(r"\bretry[\s-]*after\s*:?\s*(\d+(?:\.\d+)?)\s*(ms|s)?\b", message, re.IGNORECASE)
            if match:
                retry = float(match[1]) / (1000 if (match[2] or "").lower() == "ms" else 1)
                if not math.isfinite(retry):
                    retry = 0
        return classify(429, "limiter_request_error", retry)
    nested = limiter_failure(node.get("error"), status, retry_after, depth - 1, True)
    if nested or any(key in node for key in ("choices", "delta", "_done")):
        return nested
    response = node.get("response")
    for child in (node.get("data"), response.get("data") if isinstance(response, dict) else None):
        nested = limiter_failure(child, status, retry_after, depth - 1, failed)
        if nested:
            return nested
    return None


def structured_business(node, depth=6):
    """Inspect error envelopes only, never assistant content or tool arguments.

    WorkBuddy can wrap its code in data/error/response.data. HTTP status codes
    in an outer envelope must not hide a more specific nested business code.
    """
    if depth <= 0 or not isinstance(node, dict):
        return None
    code = node.get("code")
    text = str(code).strip() if code is not None and not isinstance(code, (dict, list, bool)) else ""
    success = text.lower() in {"", "0", "200", "000000", "success"}
    generic_status = text.isdigit() and 100 <= int(text) <= 599
    if not success and not generic_status:
        return code
    # Some error envelopes retain choices:[] or an empty delta. These must
    # not hide an explicit code/error beside them. We never descend into
    # choices/delta/content themselves, so generated text is not classified.
    explicit_error = structured_business(node.get("error"), depth - 1)
    if explicit_error is not None:
        return explicit_error
    if any(key in node for key in ("choices", "delta", "_done")):
        return None if success else code
    response = node.get("response")
    for child in (node.get("data"), response.get("data") if isinstance(response, dict) else None):
        nested = structured_business(child, depth - 1)
        if nested is not None:
            return nested
    return None if success else code


def proxy_startup_failure(node):
    """Exact legacy 3065 envelope, observed on account-station route 195.

    Never infer a timeout from generated text or a generic proxy_error.
    Gateway retries retain the original request startup deadline.
    """
    if not isinstance(node, dict):
        return None
    detail = node.get("error") if isinstance(node.get("error"), dict) else node
    if detail.get("code") == "upstream_startup_timeout" and detail.get("type") == "proxy_error":
        return UpstreamError("startup_timeout", 504, True)
    return None


def structured_message(node, business, depth=6):
    """Read wording only beside the matching business code in error envelopes."""
    if not isinstance(node, dict) or depth <= 0:
        return None
    if business is not None and str(node.get('code')) == str(business):
        return next((node[k][:4096] for k in ('message', 'msg') if isinstance(node.get(k), str)), None)
    nested = structured_message(node.get('error'), business, depth-1)
    if nested is not None or any(k in node for k in ('choices', 'delta', '_done')):
        return nested
    response = node.get('response')
    for child in (node.get('data'), response.get('data') if isinstance(response, dict) else None):
        nested = structured_message(child, business, depth-1)
        if nested is not None:
            return nested
    return None


def frame_failure(frame):
    """Read structured error metadata, never classify arbitrary request/body text."""
    if not isinstance(frame, dict):
        raise UpstreamError("invalid_stream_frame")
    proxy_timeout = proxy_startup_failure(frame)
    if proxy_timeout:
        return proxy_timeout
    limiter = limiter_failure(frame)
    if limiter:
        return limiter
    error = frame.get("error")
    business = structured_business(frame)
    if error is None and frame.get("type") != "error" and business is None:
        return None
    detail = error if isinstance(error, dict) else frame
    status = detail.get("status") or detail.get("status_code") or frame.get("status") or 200
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 200
    business = business if business is not None else detail.get("type") or frame.get("code")
    message = structured_message(frame, business)
    return classify(status, business, message=message)


def amount(value):
    if isinstance(value, bool):
        raise UpstreamError("invalid_balance")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise UpstreamError("invalid_balance") from None
    if not math.isfinite(result) or result < 0:
        raise UpstreamError("invalid_balance")
    return result


def payload(data):
    if not isinstance(data, dict):
        raise UpstreamError("invalid_response")
    proxy_timeout = proxy_startup_failure(data)
    if proxy_timeout:
        raise proxy_timeout
    limiter = limiter_failure(data)
    if limiter:
        raise limiter
    code = data.get("code")
    if code is not None and str(code) not in {"0", "200", "000000", "success"}:
        raise classify(business=code, message=data.get('message', data.get('msg')))
    if data.get("error") or data.get("result") is not None and str(data["result"]).lower() not in {"ok", "success", "0"}:
        error = data.get("error")
        business = error.get("code") if isinstance(error, dict) else data.get("result")
        raise classify(business=business, message=structured_message(data, business))
    return data.get("data", data)


class Transport:
    def __init__(self, client=None, proxy=None):
        self.client = client or httpx.AsyncClient(
            proxy=proxy, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        self.owned = client is None
        # Generation SSE only; setup/credential JSON calls are not model response timing.
        self.on_upstream_response = None

    async def close(self):
        if self.owned:
            await self.client.aclose()

    async def _read(self, response, limit=4 * 1024 * 1024):
        chunks, size = [], 0
        try:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise UpstreamError("response_too_large")
                chunks.append(chunk)
        except httpx.HTTPError as error:
            raise transport_failure(error, response_read=True) from None
        return b"".join(chunks)

    async def _check(self, response):
        if response.status_code < 400:
            return
        raw = await self._read(response, 256 * 1024)
        business, data = None, None
        try:
            data = json.loads(raw)
            business = structured_business(data)
        except (ValueError, AttributeError, TypeError):
            pass
        try:
            retry = float(response.headers.get("retry-after", 0))
        except ValueError:
            try:
                retry = (parsedate_to_datetime(response.headers["retry-after"]) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, KeyError, OverflowError):
                retry = 0
        failure = proxy_startup_failure(data) or limiter_failure(data, response.status_code, retry) or classify(response.status_code, business, retry, message=structured_message(data, business))
        failure.failure_phase = "http_status"
        raise failure

    async def json(self, method, url, headers=None, body=None):
        try:
            async with asyncio.timeout(30):
                async with self.client.stream(method, url, headers=headers, content=body) as response:
                    await self._check(response)
                    if 300 <= response.status_code < 400:
                        raise UpstreamError("unexpected_redirect")
                    raw = await self._read(response)
                    try:
                        return json.loads(raw)
                    except ValueError:
                        raise UpstreamError("invalid_response") from None
        except TimeoutError:
            raise UpstreamError("upstream_timeout", 504, True, failure_phase="operation_timeout") from None
        except httpx.HTTPError as error:
            raise transport_failure(error) from None

    async def oauth_envelope(self, url, headers):
        """OAuth may return credentials on a 302; never follow its Location."""
        try:
            async with asyncio.timeout(30):
                async with self.client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                    await self._check(response)
                    if response.status_code not in {200, 302}:
                        raise UpstreamError("unexpected_redirect")
                    raw = await self._read(response, 256 * 1024)
                    try:
                        data = json.loads(raw) if raw.strip() else {}
                    except ValueError:
                        data = {}
                    return data, dict(response.cookies)
        except TimeoutError:
            raise UpstreamError("upstream_timeout", 504, True, failure_phase="operation_timeout") from None
        except httpx.HTTPError as error:
            raise transport_failure(error) from None

    async def sse(self, url, headers, body):
        try:
            # The gateway owns the shared startup and effective-output idle
            # budgets. A separate 30s socket read limit would terminate valid
            # initial reasoning before that configured budget can elapse.
            stream_timeout = httpx.Timeout(None, connect=10, write=30, pool=30)
            async with self.client.stream("POST", url, headers=headers, content=body, timeout=stream_timeout) as response:
                if self.on_upstream_response is not None:
                    await self.on_upstream_response()
                await self._check(response)
                if 300 <= response.status_code < 400:
                    raise UpstreamError("unexpected_redirect")
                if "application/json" in response.headers.get("content-type", "").lower():
                    raw = await self._read(response, 256 * 1024)
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        raise UpstreamError("invalid_stream_frame") from None
                    failure = frame_failure(value)
                    if failure:
                        raise failure
                    # A JSON success is not a completed SSE stream. Do not turn
                    # a proxy/login page or undocumented response into an empty success.
                    raise UpstreamError("invalid_stream_frame")
                # Read raw chunks rather than unbounded aiter_lines: an upstream
                # without newlines must not grow memory until the request ends.
                decoder = __import__("codecs").getincrementaldecoder("utf-8")()
                buffer, lines = "", []
                async for chunk in response.aiter_bytes():
                    buffer += decoder.decode(chunk)
                    if len(buffer) + sum(map(len, lines)) > 2 * 1024 * 1024:
                        raise UpstreamError("frame_too_large")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")
                        if line.startswith("data:"):
                            lines.append(line[5:].lstrip(" "))
                        elif not line and lines:
                            value = "\n".join(lines)
                            lines = []
                            if value == "[DONE]":
                                yield {"_done": True}
                                return
                            try:
                                frame = json.loads(value)
                            except ValueError:
                                raise UpstreamError("invalid_stream_frame") from None
                            if not isinstance(frame, dict):
                                raise UpstreamError("invalid_stream_frame")
                            yield frame
                buffer += decoder.decode(b"", final=True)
                if buffer.strip() or lines:
                    raise UpstreamError("truncated_stream", 502, True)
        except httpx.HTTPError as error:
            raise transport_failure(error) from None
        except UnicodeError:
            raise UpstreamError("invalid_stream_frame") from None
