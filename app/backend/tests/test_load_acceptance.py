"""Isolated ASGI + SQL acceptance; never contacts any real platform.

Run with UNIFIED_TEST_DATABASE_URL for production PostgreSQL semantics. The
controlled adapter is intentionally the only mock: API auth, normalization,
selection, leases, retries, ledger, streaming and cancellation are real code.
"""
import asyncio
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import time
import threading

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import func, select

from app.adapters import Registry
from app.main import create_app
from app.models import Account, Credential, Lease, RequestAttempt, RequestLog
from app.store import Store, uid
from app.upstream import UpstreamError


PLATFORMS = ["Loomy", "小浣熊", "WPS 灵犀"]
PROTOCOLS = ["chat", "responses", "messages"]
PATHS = ["/v1/chat/completions", "/v1/responses", "/v1/messages"]


def payload(index):
    # 80 KiB of unmodified history per client, including an earlier assistant
    # answer. No user request text from live systems is used in this fixture.
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"fixture-{i}:" + "x" * 1024} for i in range(80)]
    history.append({"role": "user", "content": f"client:{index}"})
    protocol = PROTOCOLS[index % 3]
    body = {"model": "test-model", "stream": True, "max_tokens": 64}
    body["input" if protocol == "responses" else "messages"] = history
    return body


class ControlledRegistry(Registry):
    def __init__(self, count, before_cancel, after_cancel):
        super().__init__(enabled=True)
        self.count = count
        self.before_cancel, self.after_cancel = before_cancel, after_cancel
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        self.after_output = {i: asyncio.Event() for i in after_cancel}
        self.hold = asyncio.Event()
        self.seen, self.calls, self.active_accounts = set(), Counter(), Counter()
        self.platforms = {}
        self.active = self.peak = self.account_peak = self.closed = 0
        self.history_hashes = {}

    def make(self, platform, credential, identity, version, proxy):
        assert credential.get("session", "").startswith("isolated-load-")
        assert version == 1 and proxy is None
        registry = self

        class Adapter:
            def __init__(self):
                self.t = self

            async def close(self):
                registry.closed += 1

            async def chat(self, body, session):
                messages = body["messages"]
                index = int(messages[-1]["content"].split(":")[1])
                assert platform == PLATFORMS[index % 3]
                assert len(messages) == 81
                assert messages[0]["content"] == "fixture-0:" + "x" * 1024
                assert messages[-2]["content"] == "fixture-79:" + "x" * 1024
                digest = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
                assert registry.history_hashes.setdefault(index, digest) == digest
                registry.calls[index] += 1
                attempt = registry.calls[index]
                registry.platforms.setdefault(index, set()).add(platform)
                registry.seen.add(index)
                registry.active += 1
                registry.active_accounts[identity] += 1
                registry.peak = max(registry.peak, registry.active)
                registry.account_peak = max(registry.account_peak, registry.active_accounts[identity])
                if len(registry.seen) == registry.count:
                    registry.ready.set()
                try:
                    await registry.release.wait()
                    if index % 5 == 0 and attempt == 1:
                        raise UpstreamError("upstream_5xx", 502, True)
                    yield {"choices": [{"delta": {"content": f"load-answer-{index}"}}]}
                    if index in registry.after_cancel:
                        registry.after_output[index].set()
                        await registry.hold.wait()
                    await asyncio.sleep(0.005)
                    yield {"usage": {"prompt_tokens": 1000, "completion_tokens": 8, "total_tokens": 1008,
                                     "prompt_tokens_details": {"cached_tokens": 800}}}
                    yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                    yield {"_done": True}
                finally:
                    registry.active -= 1
                    registry.active_accounts[identity] -= 1

        return Adapter()


def seed(store, count):
    with store.tx() as (s, _):
        for platform in PLATFORMS:
            # Dedicated spare accounts ensure retries are tested without
            # conflating intentional exhaustion with scheduler failures.
            for i in range(math.ceil(count / 3 * 1.3) + 4):
                identity = uid()
                s.add(Account(id=identity, platform=platform, external_id=f"load-{i}", label=f"load-{i}",
                              sandbox=False, enabled=True, credential_version=1, credential_status="valid",
                              balance=10000, balance_at=store.clock(), balance_version=1, concurrency=1,
                              created_at=store.clock(), updated_at=store.clock()))
                s.add(Credential(id=uid(), account_id=identity, version=1,
                                 ciphertext=store.cipher.encrypt(json.dumps({"session": f"isolated-load-{i}"}).encode()).decode(),
                                 created_at=store.clock()))
    return {platform: store.create_key("load-only", platform, "test-model")["token"] for platform in PLATFORMS}


def percentile(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int((len(values) - 1) * q))], 3)


@pytest.mark.parametrize("clients", [50, 100, 200])
def test_mixed_protocol_load_acceptance(tmp_path, clients):
    store = Store(f"sqlite:///{tmp_path/'load.db'}", Fernet.generate_key())
    try:
        tokens = seed(store, clients)
        before_cancel = {i for i in range(clients) if i % 11 == 0}
        after_cancel = {i for i in range(clients) if i % 17 == 0 and i not in before_cancel}

        async def run():
            registry = ControlledRegistry(clients, before_cancel, after_cancel)
            app = create_app(store, registry=registry, start_worker=False, role="api", admin_token="isolated-load-admin-only")
            app.state.gateway.startup = 120
            timings, statuses, errors, started_at = {}, {}, {}, {}
            stage, waiting_index = "initial_admission", None
            batch_started = time.perf_counter()

            async def diagnostics():
                def database_counts():
                    with store.read() as (s, _):
                        return {"requests": [{"status": status, "error": error, "count": count} for status, error, count in s.execute(
                                    select(RequestLog.status, RequestLog.error_code, func.count()).group_by(RequestLog.status, RequestLog.error_code))],
                                "attempts": [{"code": code, "count": count} for code, count in s.execute(
                                    select(RequestAttempt.code, func.count()).group_by(RequestAttempt.code))],
                                "remaining_leases": s.scalar(select(func.count()).select_from(Lease))}
                report = {"backend": store.engine.dialect.name, "clients": clients, "stage": stage, "waiting_client": waiting_index,
                          "elapsed_seconds": round(time.perf_counter() - batch_started, 3), "status_counts": dict(Counter(statuses.values())),
                          "request_errors": errors, "adapter_started_clients": len(registry.seen), "adapter_calls": sum(registry.calls.values()),
                          "adapter_active": registry.active, "adapter_peak": registry.peak,
                          "completed_latencies_seconds": {str(i): round(value, 3) for i, value in timings.items()},
                          "pending_ages_seconds": {str(i): round(time.perf_counter() - value, 3) for i, value in started_at.items() if i not in timings},
                          "after_output_events": {str(i): event.is_set() for i, event in registry.after_output.items()}}
                try:
                    report["database"] = await asyncio.wait_for(asyncio.to_thread(database_counts), 5)
                except Exception as failure:
                    report["database_query_error_class"] = type(failure).__name__
                path = Path(os.getenv("UNIFIED_LOAD_REPORT_DIR", str(Path(__file__).resolve().parents[2] / "artifacts")))
                path.mkdir(parents=True, exist_ok=True)
                (path / f"load-failure-{store.engine.dialect.name}-{clients}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                print("LOAD_FAILURE_DIAGNOSTICS " + json.dumps(report))

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://load.test", timeout=150) as client:
                async def request(index):
                    started = time.perf_counter()
                    started_at[index] = started
                    try:
                        result = await client.post(PATHS[index % 3], json=payload(index),
                                                   headers={"Authorization": "Bearer " + tokens[PLATFORMS[index % 3]]})
                        assert result.status_code == 200, (index, result.status_code, result.text[:200])
                        assert f"load-answer-{index}" in result.text
                        assert '"error"' not in result.text
                        if index % 3 == 0:
                            assert "data: [DONE]" in result.text
                        elif index % 3 == 1:
                            assert "response.completed" in result.text
                        else:
                            assert "message_stop" in result.text
                        statuses[index] = "succeeded"
                    except asyncio.CancelledError:
                        statuses[index] = "cancelled"
                        raise
                    except Exception as failure:
                        statuses[index] = "failed"
                        # This harness only sends synthetic inputs. Do not log
                        # client headers, tokens, DB URLs or full response bodies.
                        errors[index] = {"class": type(failure).__name__, "detail": str(failure)[:500]}
                        raise
                    finally:
                        timings[index] = time.perf_counter() - started

                tasks = [asyncio.create_task(request(i)) for i in range(clients)]
                ready_task = asyncio.create_task(registry.ready.wait())
                try:
                    done, _ = await asyncio.wait([ready_task, *tasks], timeout=90, return_when=asyncio.FIRST_COMPLETED)
                    for finished in done:
                        finished.result()
                    assert ready_task.done(), ("not all clients reached adapters", len(registry.seen), clients)
                    for index in before_cancel:
                        tasks[index].cancel()
                    await asyncio.gather(*(tasks[i] for i in before_cancel), return_exceptions=True)
                    registry.release.set()
                    stage = "waiting_after_output"
                    for index in sorted(after_cancel):
                        waiting_index = index
                        output = asyncio.create_task(registry.after_output[index].wait())
                        try:
                            finished, _ = await asyncio.wait((output, tasks[index]), timeout=90, return_when=asyncio.FIRST_COMPLETED)
                            if tasks[index] in finished and not output.done():
                                tasks[index].result()  # Raise the original HTTP assertion/error immediately.
                                raise AssertionError(f"client {index} finished without its expected output event")
                            if output not in finished:
                                raise TimeoutError(f"client {index} did not produce effective output before test deadline")
                            tasks[index].cancel()
                        finally:
                            output.cancel()
                            await asyncio.gather(output, return_exceptions=True)
                    stage, waiting_index = "final_completion", None
                    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 120)
                    failures = [r for r in results if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)]
                    assert failures == [], [repr(e) for e in failures]
                except Exception:
                    await diagnostics()
                    raise
                finally:
                    ready_task.cancel()
                    await asyncio.gather(ready_task, return_exceptions=True)
                    registry.release.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            assert registry.peak == clients
            assert registry.account_peak == 1 and registry.active == 0
            assert all(len(platforms) == 1 for platforms in registry.platforms.values())
            expected_retries = sum(i % 5 == 0 and i not in before_cancel for i in range(clients))
            assert sum(registry.calls.values()) == clients + expected_retries
            assert registry.closed == sum(registry.calls.values())
            expected_cancel = len(before_cancel | after_cancel)
            assert Counter(statuses.values()) == {"succeeded": clients - expected_cancel, "cancelled": expected_cancel}
            with store.read() as (s, _):
                assert list(s.scalars(select(Lease))) == []
                logs = list(s.scalars(select(RequestLog)))
                attempts = list(s.scalars(select(RequestAttempt)))
                assert len(logs) == clients
                assert Counter(r.status for r in logs) == Counter(statuses.values())
                assert len(attempts) == clients + expected_retries
                assert all(a.ended_at is not None for a in attempts)
                assert all(r.first_content_at is not None for r in logs if r.status == "succeeded")
                assert all(r.ended_at is not None for r in logs)
            report = {"backend": store.engine.dialect.name, "clients": clients, "peak_adapter_concurrency": registry.peak,
                      "peak_single_account_concurrency": registry.account_peak, "successful": clients - expected_cancel,
                      "cancelled_before_output": len(before_cancel), "cancelled_after_output": len(after_cancel),
                      "same_platform_retries": expected_retries, "attempts": len(attempts), "remaining_leases": 0,
                      "protocol_clients": dict(Counter(PROTOCOLS[i % 3] for i in range(clients))),
                      "duration_seconds": {"min": round(min(timings.values()), 3), "p50": percentile(list(timings.values()), .5),
                                           "p95": percentile(list(timings.values()), .95), "max": round(max(timings.values()), 3)},
                      "history_messages_per_request": 81, "history_bytes_per_request_approx": 84000,
                      "external_requests": 0, "transport": "in-process ASGI + controlled adapter",
                      "synthetic_cache_usage_only": True, "observed_at": time.time()}
            path = Path(os.getenv("UNIFIED_LOAD_REPORT_DIR", str(Path(__file__).resolve().parents[2] / "artifacts")))
            path.mkdir(parents=True, exist_ok=True)
            (path / f"load-acceptance-{store.engine.dialect.name}-{clients}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            return report

        report = asyncio.run(run())
        print(json.dumps(report))
    finally:
        store.engine.dispose()


def test_27_mib_history_reaches_adapter_unchanged_and_configured_limit_is_explicit(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path/'large.db'}", Fernet.generate_key())
    try:
        tokens = seed(store, 1)
        calls = []
        piece = "z" * (1024 * 1024)
        expected = hashlib.sha256(piece.encode()).hexdigest()

        class LargeRegistry(Registry):
            def make(self, platform, credential, identity, version, proxy):
                class Adapter:
                    def __init__(self):
                        self.t = self

                    async def close(self):
                        pass

                    async def chat(self, body, session):
                        assert len(body["messages"]) == 27
                        for message in body["messages"]:
                            assert hashlib.sha256(message["content"].encode()).hexdigest() == expected
                        calls.append(True)
                        yield {"choices": [{"delta": {"content": "large-history-ok"}}]}
                        yield {"_done": True}
                return Adapter()

        async def run():
            registry = LargeRegistry(enabled=True)
            app = create_app(store, registry=registry, start_worker=False, role="api", admin_token="isolated-load-admin-only")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://load.test") as client:
                headers = {"Authorization": "Bearer " + tokens["Loomy"]}
                body = {"model": "test-model", "stream": False, "conversation_id": "isolated-large-history",
                        "messages": [{"role": "user", "content": piece} for _ in range(27)]}
                monkeypatch.setenv("UNIFIED_MAX_REQUEST_BYTES", str(32 * 1024 * 1024))
                response = await client.post("/v1/chat/completions", headers=headers, json=body)
                assert response.status_code == 200
                assert response.json()["choices"][0]["message"]["content"] == "large-history-ok"
                monkeypatch.setenv("UNIFIED_MAX_REQUEST_BYTES", str(512 * 1024))
                response = await client.post("/v1/chat/completions", headers=headers,
                                             json={"model": "test-model", "messages": [{"role": "user", "content": piece}]})
                assert response.status_code == 413
                assert response.json()["error"]["code"] == "request_too_large"
            assert calls == [True]
            with store.read() as (s, _):
                assert list(s.scalars(select(Lease))) == []
                assert len(list(s.scalars(select(RequestLog)))) == 1

        asyncio.run(run())
    finally:
        store.engine.dispose()


def test_client_cancel_during_initial_account_acquire_does_not_leave_lease(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path/'acquire-cancel.db'}", Fernet.generate_key())
    acquired, release, returned = threading.Event(), threading.Event(), threading.Event()
    try:
        tokens = seed(store, 1)
        original_acquire = store.acquire

        def delayed_acquire(*args, **kwargs):
            result = original_acquire(*args, **kwargs)
            acquired.set()
            try:
                assert release.wait(10), "test controller did not release acquire"
                return result
            finally:
                returned.set()

        monkeypatch.setattr(store, "acquire", delayed_acquire)

        async def run():
            app = create_app(store, registry=Registry(enabled=True), start_worker=False, role="api", admin_token="isolated-load-admin-only")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://load.test") as client:
                request = asyncio.create_task(client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + tokens["Loomy"]},
                                                         json={"model": "test-model", "stream": True, "messages": [{"role": "user", "content": "isolated cancel"}]}))
                try:
                    assert await asyncio.to_thread(acquired.wait, 10)
                    request.cancel()
                    await asyncio.sleep(.01)
                    release.set()
                    result = await asyncio.gather(request, return_exceptions=True)
                    assert isinstance(result[0], asyncio.CancelledError)
                    assert await asyncio.to_thread(returned.wait, 10)
                finally:
                    release.set()
                    if not request.done():
                        request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
            with store.read() as (s, _):
                assert list(s.scalars(select(Lease))) == []
                logs = list(s.scalars(select(RequestLog)))
                assert len(logs) == 1 and logs[0].status == "cancelled"

        asyncio.run(run())
    finally:
        release.set()
        store.engine.dispose()
