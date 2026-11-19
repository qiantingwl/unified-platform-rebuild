import asyncio
import json
import logging
import time
from contextlib import aclosing

import anyio

from .store import DomainError
from .upstream import Transport, UpstreamError, frame_failure


def normalized_delta(delta, legacy_id="call_legacy"):
    if not isinstance(delta, dict):
        raise UpstreamError("invalid_stream_frame")
    delta = dict(delta)
    if "reasoning_content" not in delta and isinstance(delta.get("reasoning"), str):
        delta["reasoning_content"] = delta.pop("reasoning")
    if "function_call" in delta and "tool_calls" not in delta:
        call = delta.pop("function_call")
        if not isinstance(call, dict):
            raise UpstreamError("invalid_stream_frame")
        delta["tool_calls"] = [{"index": 0, "id": legacy_id, "type": "function", "function": call}]
    for key in ("content", "reasoning_content"):
        if delta.get(key) is not None and not isinstance(delta[key], str):
            raise UpstreamError("invalid_stream_frame")
    calls = delta.get("tool_calls")
    if calls == []:
        # Empty lists are not tool events; Qoder otherwise closes its text
        # block for each WorkBuddy token (Magpie workbuddy_blocks regression).
        delta.pop('tool_calls')
        calls = None
    if calls is not None:
        if not isinstance(calls, list):
            raise UpstreamError("invalid_stream_frame")
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("function", {}), dict):
                raise UpstreamError("invalid_stream_frame")
            if call.get("id") is not None and not isinstance(call["id"], str):
                raise UpstreamError("invalid_stream_frame")
            if not isinstance(call.get("index", 0), int) or call.get("index", 0) < 0:
                raise UpstreamError("invalid_stream_frame")
            if any(not isinstance(v, str) for v in call.get("function", {}).values()):
                raise UpstreamError("invalid_stream_frame")
    return delta


def merge_usage(target, incoming):
    for key, value in incoming.items():
        if isinstance(value, dict):
            prior = target.get(key)
            target[key] = {**(prior if isinstance(prior, dict) else {}), **value}
        elif value is not None:
            target[key] = value
    return target

ERROR_CODES = {"request_rejected", "content_filter", "cancelled", "internal_error", "no_effective_output", "startup_timeout", "stream_idle", "truncated_stream", "network", "upstream_timeout", "upstream_5xx", "rate_limit", "unknown_403", "identity", "permission", "quota", "invalid_credentials", "invalid_stream_frame", "frame_too_large", "response_too_large", "response_read_failed", "invalid_response", "upstream_rejected", "no_available_account", "request_lease_lost", "credential_version_conflict", "invalid_credential_format", "missing_material", "network_execution_disabled", "prompt_too_large", "unexpected_redirect", "model_not_found", "unsupported_image_input", "unsupported_protocol_field", "missing_proxy"}
ERROR_CODES |= {"invalid_api_key", "api_key_expired", "api_key_ip_denied", "api_key_concurrency", "api_key_rate_limit", "api_key_daily_limit", "request_not_retryable", "proxy_retired"}
ERROR_CODES.add("upstream_queue_wait")
ERROR_CODES.add("account_concurrency_busy")
ERROR_CODES |= {"proxy_unavailable", "proxy_pool_unavailable", "proxy_pool_missing", "proxy_route_invalid", "proxy_target_unverified"}
# 上游明确停用的账号：换号后仍然全部失败时如实上报，不混成 internal_error。
ERROR_CODES.add("account_disabled")
ERROR_CODES |= {'request_route_changed', 'invalid_model_route', 'response_history_unavailable', 'response_context_too_large'}


def effective(delta):
    return bool((isinstance(delta.get("content"), str) and delta["content"].strip()) or
                (isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"].strip()) or
                (isinstance(delta.get("reasoning"), str) and delta["reasoning"].strip()) or
                (isinstance(delta.get("function_call"), dict) and bool(delta["function_call"].get("name") or delta["function_call"].get("arguments"))) or
                any(t.get("id") or t.get("function", {}).get("name") or t.get("function", {}).get("arguments") for t in delta.get("tool_calls", [])))


class Gateway:
    def __init__(self, store, registry, startup=120, idle=120, max_attempts=3, usage_grace=1, placeholder_enabled=True):
        self.store, self.registry = store, registry
        self.startup, self.idle, self.max_attempts = startup, idle, max_attempts
        self.usage_grace = usage_grace
        self.placeholder_enabled = placeholder_enabled
        self.heartbeat_interval = 10
        self._slots_changed = asyncio.Event()

    def slots_changed(self):
        previous, self._slots_changed = self._slots_changed, asyncio.Event()
        previous.set()

    async def acquire(self, token, model, excluded=(), request_id=None, conversation=None,
                      client_ip=None, previous=None, is_disconnected=None, client_source=None):
        """Optional bounded admission wait; no reservation until atomic acquire.

        A release wakes local waiters. A half-second async timer rechecks slots
        released by other processes; it neither spins nor monopolizes a slot.
        """
        deadline = previous.get('_startup_deadline') if previous else None
        deadline = deadline if deadline is not None else time.monotonic() + self.startup
        remaining_wait = previous.get('_queue_wait_remaining') if previous else None
        queue_deadline = None
        while True:
            if is_disconnected and await is_disconnected():
                raise asyncio.CancelledError()
            if time.monotonic() >= deadline:
                raise UpstreamError('startup_timeout', 504)
            wake = self._slots_changed
            pending = asyncio.create_task(asyncio.to_thread(
                self.store.acquire, token, model, excluded, request_id, conversation,
                client_ip=client_ip, distinguish_busy=True, client_source=client_source))
            try:
                current = await asyncio.shield(pending)
            except asyncio.CancelledError:
                # Cancelling to_thread cannot roll back a late DB commit. Join
                # it under shield and release only this abandoned request.
                with anyio.CancelScope(shield=True):
                    try:
                        abandoned = await pending
                    except Exception:
                        pass
                    else:
                        await asyncio.to_thread(self.store.end_request, abandoned['id'], 'cancelled', 'cancelled')
                        self.slots_changed()
                raise
            except DomainError as failure:
                if failure.code != 'account_concurrency_busy':
                    raise
                if remaining_wait is None:
                    remaining_wait = failure.queue_wait_seconds
                if queue_deadline is None:
                    queue_deadline = time.monotonic() + min(10, max(0, remaining_wait))
                left = min(queue_deadline, deadline) - time.monotonic()
                if left <= 0:
                    if time.monotonic() >= deadline:
                        raise UpstreamError('startup_timeout', 504) from None
                    raise
                try:
                    await asyncio.wait_for(wake.wait(), timeout=min(0.5, left))
                except TimeoutError:
                    pass
                # Do not take a new slot after the queue's deadline.
                if time.monotonic() >= min(queue_deadline, deadline):
                    if time.monotonic() >= deadline:
                        raise UpstreamError('startup_timeout', 504) from None
                    raise failure
                continue
            if time.monotonic() >= deadline or (queue_deadline is not None and time.monotonic() >= queue_deadline):
                code = 'startup_timeout' if time.monotonic() >= deadline else 'account_concurrency_busy'
                await asyncio.to_thread(self.store.end_request, current['id'], 'failed', code)
                self.slots_changed()
                raise UpstreamError(code, 504 if code == 'startup_timeout' else 503)
            current['_startup_deadline'] = deadline
            current['_queue_wait_remaining'] = (max(0, queue_deadline - time.monotonic()) if queue_deadline is not None
                                                else remaining_wait if remaining_wait is not None else current.get('queue_wait_seconds', 0))
            return current

    async def observe_proxy(self, current, proxy, *, success, code=None, failure_phase=None):
        if not proxy:
            return
        from .proxy_pool import record_proxy_result
        try:
            await asyncio.to_thread(record_proxy_result, self.store, current["account_id"], proxy,
                                   success=success, code=code, model=current.get("upstream_model", current.get("model")),
                                   model_verified=success, failure_phase=failure_phase)
        except Exception:
            # An observation write must not discard a response already generated.
            logging.getLogger(__name__).warning("proxy observation could not be persisted")

    async def mark_upstream_progress(self, current, stage):
        pending = asyncio.create_task(asyncio.to_thread(self.store.mark_upstream_progress, current, stage))
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Join the transaction before cancellation releases this attempt's lease.
            with anyio.CancelScope(shield=True):
                await asyncio.gather(pending, return_exceptions=True)
            raise
        except DomainError as failure:
            if failure.code == 'stale_request_receipt':
                # Do not run retry/fail_attempt bookkeeping using a stale receipt.
                raise DomainError('request_lease_lost', 409) from None
            raise

    async def frames(self, token, body, current, conversation=None):
        """One request ledger and startup deadline, including session setup/retries."""
        deadline = current.get('_startup_deadline', time.monotonic() + self.startup)
        status, error = "cancelled", "cancelled"
        started, excluded = False, set()
        last_effective = time.monotonic()
        request_id = current["id"]

        async def heartbeat():
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                pending = asyncio.create_task(asyncio.to_thread(self.store.heartbeat_request, request_id))
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    # Cancelling an await does not stop a DB transaction in its
                    # worker thread. Join it before fail_attempt/end_request can
                    # release the lease, including under ASGI level cancellation.
                    with anyio.CancelScope(shield=True):
                        await asyncio.gather(pending, return_exceptions=True)
                    raise

        pulse = asyncio.create_task(heartbeat())

        async def next_frame(stream, remaining):
            # A lease failure must interrupt a silent model, not wait for its
            # next token or the full startup/idle timeout before being noticed.
            pending = asyncio.create_task(anext(stream))
            try:
                done, _ = await asyncio.wait((pending, pulse), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if pulse in done:
                    pulse.result()
                if pending in done:
                    return pending.result()
                raise TimeoutError()
            finally:
                if not pending.done():
                    pending.cancel()
                # Finish cancelling anext before the enclosing aclosing closes
                # the async generator (otherwise it is still running).
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(pending, return_exceptions=True)
        try:
            if self.placeholder_enabled:
                await asyncio.to_thread(self.store.mark_output, request_id, True)
                yield {"delta": {"role": "assistant", "content": " "}, "placeholder": True}
            if current["sandbox"]:
                await asyncio.to_thread(self.store.mark_output, request_id)
                yield {"delta": {"content": "本地沙盒响应。该请求未访问任何上游平台。"}}
                status, error = "succeeded", None
                yield {"finish": "stop"}
                return
            for number in range(self.max_attempts):
                excluded.add(current["account_id"])
                adapter, finished, done = None, None, False
                retry_failure = None
                proxy = None
                finish_deadline = None
                attempt_usage = {}
                tool_metadata = {}
                first_frame = False
                try:
                    credential = await asyncio.to_thread(self.store.credential, current["account_id"], current["version"])
                    try:
                        proxy = await asyncio.to_thread(self.store.account_proxy, current["account_id"])
                    except DomainError as failure:
                        # Keep this account's configured egress unchanged. A
                        # different account may have a healthy route, but never
                        # reinterpret proxy resolution failure as direct access.
                        if failure.code in {"missing_proxy", "proxy_retired", "proxy_unavailable", "proxy_pool_unavailable", "proxy_pool_missing", "proxy_target_unverified"}:
                            raise UpstreamError(failure.code, 503, True) from None
                        raise
                    adapter = self.registry.make(current["platform"], credential, current["account_id"], current["version"], proxy)
                    attempt = dict(current)
                    if isinstance(adapter.t, Transport):
                        async def response_arrived(receipt=attempt):
                            await self.mark_upstream_progress(receipt, 'upstream_response_at')
                        adapter.t.on_upstream_response = response_arrived
                    if 'thinking' in body and not getattr(adapter, 'supports_thinking_control', False):
                        raise UpstreamError('unsupported_protocol_field', 422)
                    upstream_body = {**body, "model": current.get("upstream_model", body["model"])}
                    async with aclosing(adapter.chat(upstream_body, current["session"])) as stream:
                        while True:
                            if pulse.done():
                                pulse.result()
                            remaining = self.idle - (time.monotonic() - last_effective) if started else deadline - time.monotonic()
                            if finish_deadline is not None:
                                remaining = min(remaining, finish_deadline - time.monotonic())
                            if remaining <= 0:
                                if finished:
                                    break
                                raise UpstreamError("stream_idle" if started else "startup_timeout", 504)
                            try:
                                frame = await next_frame(stream, remaining)
                            except StopAsyncIteration:
                                break
                            except TimeoutError:
                                if finished:
                                    break
                                raise UpstreamError("stream_idle" if started else "startup_timeout", 504) from None
                            except UpstreamError as failure:
                                if finished and failure.code in {"network", "truncated_stream", "upstream_timeout"}:
                                    break
                                raise
                            if not first_frame:
                                await self.mark_upstream_progress(attempt, 'first_frame_at')
                                first_frame = True
                            failure = frame_failure(frame)
                            if failure:
                                raise failure
                            if frame.get("_done"):
                                done = True
                                break
                            usage_updated = isinstance(frame.get("usage"), dict) and bool(frame["usage"])
                            if usage_updated:
                                merge_usage(attempt_usage, frame["usage"])
                            previously_started = started
                            choices = frame.get("choices") or []
                            if not isinstance(choices, list) or any(not isinstance(c, dict) for c in choices):
                                raise UpstreamError("invalid_stream_frame")
                            for choice in choices:
                                if choice.get("index", 0) != 0:
                                    raise UpstreamError("invalid_stream_frame")
                                delta = normalized_delta(choice.get("delta") or {}, "call_"+request_id)
                                for call in delta.get("tool_calls", []):
                                    metadata = tool_metadata.setdefault(call.get("index", 0), {"id": False, "name": False})
                                    metadata["id"] |= bool((call.get("id") or "").strip())
                                    metadata["name"] |= bool(call.get("function", {}).get("name", "").strip())
                                if effective(delta):
                                    last_effective = time.monotonic()
                                    if not started:
                                        started = True
                                        await asyncio.to_thread(self.store.mark_output, request_id)
                                # Don't forward pre-content role/blank frames repeatedly.
                                if started and delta:
                                    yield {"delta": delta}
                                if choice.get("finish_reason"):
                                    finished = choice["finish_reason"]
                                    if finish_deadline is None:
                                        finish_deadline = time.monotonic() + self.usage_grace
                            # Before output, usage belongs to a speculative attempt.
                            # Never publish it if this request switches accounts.
                            if started and attempt_usage and (usage_updated or not previously_started):
                                await asyncio.to_thread(self.store.record_usage, request_id, attempt_usage)
                                yield {"usage": dict(attempt_usage)}
                    if not started and finished == "content_filter":
                        # A moderation verdict is a result: another account would get the same.
                        status, error = "failed", "content_filter"
                        yield {"finish": "content_filter"}
                        return
                    if not started:
                        raise UpstreamError("no_effective_output", 502, True)
                    if not (done or finished):
                        raise UpstreamError("truncated_stream", 502)
                    if finished not in {"length", "content_filter"} and any(not (tool["id"] and tool["name"]) for tool in tool_metadata.values()):
                        # A tool id alone starts output (so cannot be replayed),
                        # but it is not an executable completed tool call.
                        raise UpstreamError("invalid_stream_frame", 502)
                    status, error = "succeeded", None
                    await self.observe_proxy(current, proxy, success=True)
                    yield {"finish": finished or "stop"}
                    return
                except UpstreamError as failure:
                    # fail_attempt releases the current slot. Do not let an
                    # old heartbeat mistake the legitimate retry admission gap
                    # for losing an active lease.
                    pulse.cancel()
                    await asyncio.gather(pulse, return_exceptions=True)
                    await self.observe_proxy(current, proxy, success=False, code=failure.code, failure_phase=failure.failure_phase)
                    await asyncio.to_thread(self.store.fail_attempt, current, failure)
                    self.slots_changed()
                    if not started and proxy and failure.failure_phase in {'connect', 'proxy_connect'} and number + 1 < self.max_attempts:
                        # Preserve explicit fixed bindings, but don't retry this
                        # same failed connection through another identically bound
                        # account in the same request. No persisted account penalty.
                        from .proxy_pool import accounts_on_fixed_endpoint
                        try:
                            excluded.update(await asyncio.to_thread(accounts_on_fixed_endpoint, self.store, current['platform'], proxy))
                        except Exception:
                            logging.getLogger(__name__).warning('request egress exclusion could not be read')
                    if not started and failure.retryable and time.monotonic() >= deadline:
                        # Accounting and account switching consume the same startup
                        # budget; report its exhaustion even if the last hop failed.
                        raise UpstreamError("startup_timeout", 504) from None
                    if started or not failure.retryable or number + 1 >= self.max_attempts or time.monotonic() >= deadline:
                        raise
                    retry_failure = failure
                finally:
                    if adapter:
                        # Cleanup failures must not replace a known upstream result.
                        with anyio.CancelScope(shield=True):
                            try:
                                await adapter.t.close()
                            except Exception:
                                pass
                try:
                    current = await self.acquire(token, body["model"], excluded, request_id, conversation,
                                                 client_ip=current.get("client_ip"), previous=current)
                except DomainError as admission:
                    # No next candidate does not erase the upstream failure
                    # that caused this retry. Initial admission is outside this
                    # path; key/auth/policy failures retain their own meaning.
                    if admission.code in {"no_available_account", "account_concurrency_busy"} and retry_failure is not None:
                        if time.monotonic() >= deadline:
                            raise UpstreamError("startup_timeout", 504) from None
                        raise retry_failure from None
                    raise
                pulse = asyncio.create_task(heartbeat())
            raise UpstreamError("no_available_account", 503)
        except (UpstreamError, DomainError) as failure:
            status, error = "failed", failure.code if failure.code in ERROR_CODES else "internal_error"
            yield {"error": {"code": error, "type": "upstream_error", "status": failure.status}}
        except asyncio.CancelledError:
            raise
        except Exception:
            status, error = "failed", "internal_error"
            yield {"error": {"code": error, "type": "upstream_error"}}
        finally:
            # Starlette's disconnect cancel scope cancels every unshielded
            # await. Shield the heartbeat join too, or it prevents the request
            # receipt/slot release below from ever running.
            with anyio.CancelScope(shield=True):
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
                await asyncio.to_thread(self.store.end_request, request_id, status, error)
                self.slots_changed()


def chunk(request_id, model, item):
    result = {"id": request_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    if "error" in item:
        return {"error": item["error"]}
    if "usage" in item:
        return {**result, "choices": [], "usage": item["usage"]}
    return {**result, "choices": [{"index": 0, "delta": item.get("delta", {}), "finish_reason": item.get("finish")}]}


class Collector:
    def __init__(self, request_id, model):
        self.id, self.model = request_id, model
        self.content, self.reasoning, self.tools = [], [], {}
        self.usage, self.finish = {}, None
        self.size = 0

    def add(self, item):
        if "error" in item:
            raise UpstreamError(item["error"]["code"], item["error"].get("status", 502))
        if item.get("placeholder"):
            return
        self.size += len(json.dumps(item))
        if self.size > 16 * 1024 * 1024:
            raise UpstreamError("response_too_large")
        d = normalized_delta(item.get("delta", {}), "call_"+self.id)
        if d.get("content"):
            self.content.append(d["content"])
        if d.get("reasoning_content"):
            self.reasoning.append(d["reasoning_content"])
        for t in d.get("tool_calls", []):
            target = self.tools.setdefault(t.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if t.get("id"):
                target["id"] = t["id"]
            for key in ("name", "arguments"):
                if t.get("function", {}).get(key):
                    target["function"][key] += t["function"][key]
        if "usage" in item:
            merge_usage(self.usage, item["usage"])
        if "finish" in item:
            self.finish = item["finish"]

    def result(self):
        message = {"role": "assistant", "content": "".join(self.content)}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.tools:
            message["tool_calls"] = [self.tools[k] for k in sorted(self.tools)]
        return {"id": self.id, "object": "chat.completion", "created": int(time.time()), "model": self.model, "choices": [{"index": 0, "message": message, "finish_reason": self.finish}], **({"usage": self.usage} if self.usage else {})}
