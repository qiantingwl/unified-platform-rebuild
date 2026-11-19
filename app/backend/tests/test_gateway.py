import asyncio
import json
from contextlib import aclosing

import httpx
import pytest
from conftest import seed_sandbox
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters import Registry, Loomy, Raccoon, WorkBuddy, Marvis, WPS
from app.gateway import Gateway, Collector
from app.main import create_app
from app.models import Account, Lease, RequestAttempt, ModelState, State
from app.protocols import normalize, converted
from app.store import Store
from app.upstream import Transport, UpstreamError


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'db'}", Fernet.generate_key())
    yield s
    s.engine.dispose()


def real_accounts(store, n=3):
    store.real_capabilities = {"Loomy": {"chat", "balance", "checkin"}}
    for i in range(n):
        aid = store.create_account("Loomy", str(i), f"account {i}")
        store.put_credential(aid, json.dumps({"session": f"synthetic-{i}"}), 0)
        with store.tx() as (s, _):
            a = s.get(Account, aid)
            a.credential_status, a.balance, a.balance_at, a.balance_version = "valid", 200, store.clock(), 1
    return store.create_key("test", "Loomy", "model")["token"]


class FakeRegistry:
    enabled = True
    def __init__(self, flows):
        self.flows, self.calls, self.closed = flows, [], 0
    def make(self, platform, credential, identity, version, proxy):
        self.calls.append(identity)
        flow = self.flows[len(self.calls) - 1]
        registry = self
        class Adapter:
            t = None
            def __init__(self):
                self.t = self
            async def close(self):
                registry.closed += 1
            async def chat(self, body, session):
                for item in flow:
                    if isinstance(item, float):
                        await asyncio.sleep(item)
                    elif isinstance(item, Exception):
                        raise item
                    else:
                        yield item
        return Adapter()


def delta(text):
    return {"choices": [{"delta": {"content": text}}]}


def execute(store, registry, **options):
    token = real_accounts(store)
    current = store.acquire(token, "model", conversation="conversation")
    async def run():
        return [x async for x in Gateway(store, registry, **options).frames(token, {"model": "model", "messages": [{"role": "user", "content": "private-input"}]}, current, "conversation")]
    result = asyncio.run(run())
    with store.tx() as (s, _):
        assert list(s.scalars(select(Lease))) == []
    return result


def test_retry_before_effective_and_one_request_log(store):
    reg = FakeRegistry([[UpstreamError("invalid_credentials", 401, True)], [delta("answer"), {"_done": True}]])
    result = execute(store, reg)
    assert result[0]["placeholder"] and result[-1]["finish"] == "stop"
    assert len(set(reg.calls)) == 2 and reg.closed == 2
    data = store.snapshot()
    assert len(data["requests"]) == 1 and data["requests"][0]["status"] == "succeeded"
    # One 401 is an observation: the account pauses for a credential review, not a relogin.
    assert sum(a["credential_status"] == "suspect" for a in data["accounts"]) == 1
    assert sum(a["status"] == "credential_review" for a in data["accounts"]) == 1
    assert all(a["enabled"] for a in data["accounts"])
    with store.tx() as (s, _):
        attempts = list(s.scalars(select(RequestAttempt)))
        assert len(attempts) == 2 and all(a.ended_at for a in attempts)


@pytest.mark.parametrize("first", [delta("text"), {"choices": [{"delta": {"reasoning_content": "thinking"}}]}, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call", "function": {"name": "weather", "arguments": "{"}}]}}]}])
def test_never_retry_after_meaningful_content(store, first):
    reg = FakeRegistry([[first, UpstreamError("network", 502, True)]])
    result = execute(store, reg)
    assert result[-1]["error"]["code"] == "network"
    assert len(reg.calls) == 1


def test_startup_budget_not_reset_by_blanks_or_retry(store):
    reg = FakeRegistry([[.025, delta(" "), .025, UpstreamError("network", 502, True)], [.06, delta("too late")]])
    result = execute(store, reg, startup=.08)
    assert result[-1]["error"]["code"] == "startup_timeout"
    assert store.snapshot()["requests"][0]["first_content_at"] is None


def test_idle_not_kept_alive_by_empty_chunks(store):
    reg = FakeRegistry([[delta("first"), .03, delta(""), .03, delta(""), .03, delta("more")]])
    result = execute(store, reg, idle=.05)
    assert result[-1]["error"]["code"] == "stream_idle"


def test_empty_done_is_failure_and_eof_is_not_success(store):
    reg = FakeRegistry([[{"_done": True}], [delta(" "), {"_done": True}], [delta(" "), {"_done": True}]])
    assert execute(store, reg)[-1]["error"]["code"] == "no_effective_output"


def test_partial_eof_is_explicit_failure(store):
    result = execute(store, FakeRegistry([[delta("partial")]]))
    assert result[-1]["error"]["code"] == "truncated_stream"


def test_finished_stream_releases_without_waiting_for_socket_close(store):
    reg=FakeRegistry([[delta("answer"), {"choices":[{"delta":{},"finish_reason":"stop"}]}, .1, delta("unexpected")]])
    result=execute(store,reg,usage_grace=.02)
    assert result[-1]["finish"]=="stop" and reg.closed==1


def test_usage_tail_after_finish_is_recorded(store):
    reg=FakeRegistry([[delta("answer"), {"choices":[{"delta":{},"finish_reason":"stop"}]}, {"choices":[],"usage":{"prompt_tokens":100,"prompt_tokens_details":{"cached_tokens":80}}}, {"_done":True}]])
    result=execute(store,reg)
    assert result[-1]["finish"]=="stop"
    assert store.snapshot()["requests"][0]["cached_tokens"]==80


def test_generator_cancellation_releases_slot(store):
    token=real_accounts(store,1)
    current=store.acquire(token,"model")
    reg=FakeRegistry([[10.,delta("too late")]])
    async def run():
        generator=Gateway(store,reg).frames(token,{"model":"model","messages":[]},current)
        await anext(generator)
        pending=asyncio.create_task(anext(generator))
        await asyncio.sleep(.02)
        pending.cancel()
        await asyncio.gather(pending,return_exceptions=True)
        await generator.aclose()
    asyncio.run(run())
    with store.tx() as (s,_):
        assert list(s.scalars(select(Lease)))==[]
    assert store.snapshot()["requests"][0]["status"]=="cancelled"


def test_affinity_and_no_cross_platform_fallback(store):
    token = real_accounts(store, 2)
    a = store.acquire(token, "model", conversation="c1")
    store.mark_output(a["id"])
    store.end_request(a["id"], "succeeded")
    b = store.acquire(token, "model", conversation="c1")
    assert b["account_id"] == a["account_id"] and b["session"] == a["session"]
    c = store.acquire(token, "model", conversation="c2")
    assert c["account_id"] != b["account_id"]


def test_response_protocols_and_unknown_fields(store):
    seed_sandbox(store)
    token = store.create_key("t", "Loomy", "sandbox-echo")["token"]
    client = TestClient(create_app(store, start_worker=False))
    headers = {"Authorization": "Bearer " + token}
    for path, payload in [("responses", {"input": "hello"}), ("messages", {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 64})]:
        body = {"model": "sandbox-echo", **payload}
        response = client.post("/v1/" + path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        stream = client.post("/v1/" + path, headers=headers, json={**body, "stream": True})
        assert ("response.completed" if path == "responses" else "message_stop") in stream.text
    bad = client.post('/v1/responses', headers=headers, json={"model":"sandbox-echo", "input":"hi", "previous_response_id":"unknown"})
    assert bad.status_code == 404
    assert bad.json()['error']['code'] == 'response_not_found'


def test_protocol_image_and_tool_preservation():
    body = normalize({"model":"m", "input":[{"role":"user", "content":[{"type":"input_image", "image_url":"data:image/png;base64,abc"}]}, {"type":"function_call", "call_id":"x", "name":"f", "arguments":"{}"}, {"type":"function_call_output", "call_id":"x", "output":"result"}]}, "responses")
    assert body["messages"][0]["content"][0]["image_url"]["url"].endswith("abc")
    assert body["messages"][1]["tool_calls"][0]["id"] == body["messages"][2]["tool_call_id"]
    c = Collector("id", "m")
    c.add({"delta":{"tool_calls":[{"index":0,"id":"c", "function":{"name":"tool","arguments":"{\"a\":"}}]}})
    c.add({"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}})
    c.add({"finish":"tool_calls"})
    assert converted(c.result(), "messages")["content"][0]["input"] == {"a":1}


def test_proxy_encryption_version_and_network_off(store):
    client = TestClient(create_app(store, start_worker=False))
    headers = {"X-Console-Client":"unified-local"}
    secret = "http://username:private-password@127.0.0.1:8888"
    r = client.post('/api/proxies', headers=headers, json={"name":"local", "url":secret})
    assert r.status_code == 201
    data = client.get('/api/proxies').text
    assert "private-password" not in data and "username" not in data
    r = client.post(f'/api/proxies/{r.json()["id"]}/check', headers=headers, json={"platform":"Loomy"})
    assert r.status_code == 409


def test_source_contracts_zero_and_missing_balance():
    async def run():
        replies = [{"code":0,"data":{"availableBalance":0,"balance":500}}, {"code":0,"data":{}}, {"code":0,"data":{"total_balance":0}}]
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,json=replies.pop(0))))
        t = Transport(client)
        assert await Loomy(t,{"session":"synthetic"},"id").balance() == 0
        with pytest.raises(UpstreamError, match="invalid_balance"):
            await Raccoon(t,{"token":"synthetic"},"id").balance()
        assert await WPS(t,{"cookie":"wps_sid=synthetic"},"id").balance() == 0
        await client.aclose()
    asyncio.run(run())


def test_sse_split_utf8_multiline_and_error_body_read():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = 'data: {"choices":[{"delta":{"content":"中文"}}]}\r\n\r\ndata: [DONE]\n\n'.encode()
            for byte in data:
                yield bytes([byte])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,stream=Stream()))) as client:
            frames = [x async for x in Transport(client).sse("https://mock.test",{},b"{}")]
            assert frames[0]["choices"][0]["delta"]["content"] == "中文"
            assert frames[-1]["_done"]
    asyncio.run(run())


def test_additive_migration_keeps_accounts(store):
    seed_sandbox(store)
    with store.tx() as (s, _):
        s.get(State, 1).schema_version = 1
    # The encryption key stays outside DB; reuse the initialized cipher via key encoding.
    import base64
    key = base64.urlsafe_b64encode(store.cipher._signing_key + store.cipher._encryption_key)
    reopened = Store(store.engine.url.render_as_string(hide_password=False), key)
    assert len(reopened.snapshot()["accounts"]) == 28
    with reopened.read() as (s, _):
        assert s.get(State, 1).schema_version == 2
    reopened.engine.dispose()
