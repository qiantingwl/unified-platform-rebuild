import asyncio
import json
from contextlib import aclosing

import httpx
import pytest
from cryptography.fernet import Fernet

from app.adapters import WPS
from app.gateway import Collector, Gateway
from app.protocols import StreamTranslator, converted, normalize
from app.store import DomainError, Store
from app.upstream import Transport, UpstreamError, classify
from test_gateway import FakeRegistry, delta, execute, real_accounts


def test_sse_socket_read_does_not_override_gateway_startup_budget():
    async def run():
        async def upstream(request):
            timeout = request.extensions["timeout"]
            assert timeout == {"connect": 10, "read": None, "write": 30, "pool": 30}
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text='data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            frames = [frame async for frame in Transport(client).sse("https://synthetic.invalid", {}, b"{}")]
            assert frames[-1] == {"_done": True}
    asyncio.run(run())


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path/'audit.db'}", Fernet.generate_key())
    yield s
    s.engine.dispose()


def test_placeholder_switch_only_changes_placeholder_not_real_first_output(store):
    result = execute(store, FakeRegistry([[delta("OK"), {"_done": True}]]), placeholder_enabled=False)
    assert result[0] == {"delta": {"content": "OK"}}
    record = store.snapshot()["requests"][0]
    assert record["placeholder_at"] is None and record["first_content_at"] is not None


def test_structured_stream_error_changes_account_before_content(store):
    registry = FakeRegistry([[{"error": {"code": "insufficient_points", "type": "invalid_request_error"}}], [delta("OK"), {"_done": True}]])
    result = execute(store, registry)
    assert result[-1] == {"finish": "stop"} and len(registry.calls) == 2
    accounts=store.snapshot()["accounts"]
    failed=next(a for a in accounts if a['id']==registry.calls[0])
    assert all(a['balance']==200 and a['enabled'] for a in accounts)
    assert any(b['reason']=='quota' and b['model']=='model' and b['verified_at'] is None for b in failed['model_blocks'])


def test_retry_does_not_publish_failed_attempt_usage_or_cache(store):
    registry = FakeRegistry([
        [{"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 90}}},
         {"_done": True, "error": {"type": "server_error"}}],
        [{"usage": {"prompt_tokens": 50}}, delta("OK"),
         {"usage": {"completion_tokens": 0}}, {"_done": True}],
    ])
    result = execute(store, registry)
    assert len(set(registry.calls)) == 2
    assert result[0]["placeholder"] and result[1] == {"delta": {"content": "OK"}}
    assert [event["usage"] for event in result if "usage" in event] == [
        {"prompt_tokens": 50}, {"prompt_tokens": 50, "completion_tokens": 0},
    ]
    assert result[-1] == {"finish": "stop"}
    record = store.snapshot()["requests"][0]
    assert record["input_tokens"] == 50 and record["cached_tokens"] is None


def test_structured_stream_error_after_content_does_not_retry(store):
    registry = FakeRegistry([[delta("partial"), {"error": {"type": "server_error"}}]])
    result = execute(store, registry)
    assert result[-1]["error"]["code"] == "upstream_5xx" and len(registry.calls) == 1


@pytest.mark.parametrize("call", [
    {"index": 0, "id": "call_only"},
    {"index": 0, "id": "call_empty", "function": {"name": "", "arguments": "{}"}},
    {"index": 0, "function": {"name": "lookup", "arguments": "{}"}},
])
def test_done_after_incomplete_tool_metadata_is_not_a_success(store, call):
    registry = FakeRegistry([[{"choices": [{"delta": {"tool_calls": [call]}}]}, {"_done": True}]])
    result = execute(store, registry)
    assert result[-1]["error"]["code"] == "invalid_stream_frame"
    assert len(registry.calls) == 1  # tool output already started: never replay
    assert store.snapshot()["requests"][0]["status"] == "failed"


@pytest.mark.parametrize("kind", ["reasoning", "tool"])
def test_complete_reasoning_or_fragmented_tool_is_not_empty_output(store, kind):
    frames = ([{"choices": [{"delta": {"reasoning_content": "reasoned result"}}]}] if kind == "reasoning" else [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call", "function": {"name": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "lookup", "arguments": "{}"}}]}}]},
    ])
    result = execute(store, FakeRegistry([frames + [{"_done": True}]]))
    assert result[-1] == {"finish": "stop"}
    assert store.snapshot()["requests"][0]["status"] == "succeeded"


def test_reasoning_and_legacy_calls_survive_all_output_protocols(store):
    frames = [{"choices": [{"delta": {"reasoning": "thought"}}]}, {"choices": [{"delta": {"function_call": {"name": "lookup", "arguments": "{"}}}]}, {"choices": [{"delta": {"function_call": {"arguments": '"a":1}'}}}]}, {"choices": [{"delta": {}, "finish_reason": "function_call"}]}, {"_done": True}]
    result = execute(store, FakeRegistry([frames]))
    collector = Collector("r", "m")
    for item in result:
        collector.add(item)
    out = collector.result()
    assert out["choices"][0]["message"]["reasoning_content"] == "thought"
    assert json.loads(out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"a": 1}
    assert converted(out, "messages")["stop_reason"] == "tool_use"
    assert converted(out, "responses")["output"][-1]["type"] == "function_call"


def test_partial_usage_frames_merge_details_without_inventing_missing_counts():
    collector = Collector("r", "m")
    collector.add({"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}})
    collector.add({"usage": {"prompt_tokens_details": {"audio_tokens": 0}, "completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 7}, "cache_creation_input_tokens": 5}})
    collector.add({"delta": {"content": "OK"}})
    collector.add({"finish": "stop"})
    response = converted(collector.result(), "responses")
    assert response["usage"]["input_tokens_details"]["cached_tokens"] == 80
    assert response["usage"]["output_tokens_details"]["reasoning_tokens"] == 7
    message = converted(collector.result(), "messages")
    assert message["usage"] == {"input_tokens": 15, "output_tokens": 10, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 5}
    empty = Collector("r2", "m")
    empty.add({"delta": {"content": "OK"}}); empty.add({"finish": "stop"})
    assert converted(empty.result(), "responses")["usage"] is None
    assert converted(empty.result(), "messages")["usage"] == {}


@pytest.mark.parametrize("payload", [None, [], {"model": "m", "messages": {}}, {"model": "m", "messages": [None]}, {"model": "m", "messages": [{"role": "user", "content": [42]}]}])
def test_invalid_input_is_explicit_422_not_server_error(payload):
    with pytest.raises(DomainError) as failure:
        normalize(payload, "chat")
    assert failure.value.status == 422


def test_responses_parallel_tool_calls_stay_in_same_assistant_turn():
    body = normalize({"model": "m", "reasoning": {"effort": "high"}, "input": [{"role": "user", "content": "q"}, {"type": "function_call", "call_id": "a", "name": "f", "arguments": "{}"}, {"type": "function_call", "call_id": "b", "name": "g", "arguments": "{}"}, {"type": "function_call_output", "call_id": "a", "output": "x"}, {"type": "function_call_output", "call_id": "b", "output": "y"}]}, "responses")
    assert len(body["messages"]) == 4
    assert len(body["messages"][1]["tool_calls"]) == 2
    assert body["reasoning_effort"] == "high"


def test_explicit_cache_hint_survives_long_multi_turn_and_tool_history():
    system = [{"type": "text", "text": "stable system "*2000, "cache_control": {"type": "ephemeral"}}]
    tools = [{"name": "find", "input_schema": {"type": "object", "properties": {}}, "cache_control": {"type": "ephemeral"}}]
    raw = {"model": "m", "system": system, "tools": tools, "messages": [{"role": "user", "content": "first"}]}
    first = normalize(raw, "messages")
    for turn in range(100):
        raw["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"c{turn}", "name": "find", "input": {}}]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"c{turn}", "content": "result"*100}]}]
    last = normalize(raw, "messages")
    assert first["prompt_cache_key"] == last["prompt_cache_key"]
    assert last["messages"][0]["content"] == system
    assert len(last["messages"]) == 202  # no silent history trimming
    assert last["tools"][0]["cache_control"] == {"type": "ephemeral"}
    raw["system"][0]["text"] = "different"
    assert normalize(raw, "messages")["prompt_cache_key"] != first["prompt_cache_key"]
    assert "prompt_cache_key" not in normalize({"model": "m", "messages": [{"role": "user", "content": "q"}]}, "messages")


@pytest.mark.parametrize("code", ["insufficient_points", " Insufficient_Points ", "insufficient_balance", "no_quota"])
def test_raccoon_quota_string_codes_from_merged_pr45(code):
    error = classify(400, code)
    assert error.code == "quota" and error.retryable
    assert classify(400, "invalid_parameter").code != "quota"
    assert classify(403).code == "unknown_403"


def test_http200_json_error_is_classified_immediately_and_redacted():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": {"code": "rate_limit_exceeded", "message": "private body"}}))) as client:
            with pytest.raises(UpstreamError) as failure:
                _ = [frame async for frame in Transport(client).sse("https://synthetic.invalid", {}, b"{}")]
            assert failure.value.code == "rate_limit" and "private" not in str(failure.value)
    asyncio.run(run())


def test_wps_http_rejection_invalidates_cached_run_but_size_rejection_does_not():
    async def run():
        cache = {("a", 1, "m", "s"): {"created": __import__('time').time(), "session_id": "sid", "run_id": "rid"}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": {"code": "PermissionDenied"}}))) as client:
            adapter = WPS(Transport(client), {"cookie": "wps_sid=synthetic"}, "a", 1)
            adapter.cache = cache
            body = {"model": "m", "messages": [{"role": "user", "content": "q"}]}
            with pytest.raises(UpstreamError, match="permission"):
                _ = [frame async for frame in adapter.chat(body, "s")]
            assert not cache
    asyncio.run(run())


def test_wps_passes_official_reasoning_and_completion_limit_without_trimming():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"session_id": "sid", "run_id": "rid"}}))) as client:
            adapter = WPS(Transport(client), {"cookie": "wps_sid=synthetic"}, "a", 1); adapter.cache = {}
            body = {"model": "m", "messages": [{"role": "user", "content": "q"}], "reasoning_effort": "high", "max_completion_tokens": 200}
            _, _, outgoing = await adapter.prepare(body, "s")
            assert outgoing["reasoning_effort"] == outgoing["llm_arguments"]["reasoning_effort"] == "high"
            assert outgoing["base_llm_arguments"]["max_tokens"] == 200
            assert body["messages"] == [{"role": "user", "content": "q"}]
    asyncio.run(run())


def test_explicit_stream_usage_option_and_invalid_custom_tools():
    raw = {"model": "m", "messages": [{"role": "user", "content": "q"}], "stream_options": {"include_usage": True}}
    assert normalize(raw, "chat")["stream_options"] == {"include_usage": True}
    raw["tools"] = [{"type": "custom", "name": "exec", "format": {"type": "grammar"}}]
    with pytest.raises(DomainError) as failure:
        normalize(raw, "chat")
    assert failure.value.status == 422  # no malformed request reaches DeepSeek


def test_wps_tool_schema_uses_official_reference_expansion():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"session_id": "sid", "run_id": "rid"}}))) as client:
            adapter = WPS(Transport(client), {"cookie": "wps_sid=synthetic"}, "a", 1); adapter.cache = {}
            schema = {"$defs": {"X": {"type": "object", "properties": {"x": {"type": "string"}}}}, "$ref": "#/$defs/X"}
            body = {"model": "m", "messages": [{"role": "user", "content": "q"}], "tools": [{"type": "function", "function": {"name": "f", "parameters": schema}}]}
            _, _, outgoing = await adapter.prepare(body, "s")
            parameters = outgoing["tools"][0]["function"]["parameters"]
            assert parameters["type"] == "object" and parameters["properties"]["x"]["type"] == "string"
            assert "$ref" in schema  # no mutation of original client's history
    asyncio.run(run())


@pytest.mark.parametrize("frame,code", [
    ({"error": {"code": "UserIdentityNotVerified"}}, "identity"),
    ({"code": "AuditFailed"}, "content_filter"),
    ({"result": "UserUnauthorized"}, "invalid_credentials"),
    ({"error": {"code": "UserUnauthorized"}}, "invalid_credentials"),
])
def test_wps_nested_errors_and_moderation_are_not_empty_responses(frame, code):
    async def run():
        cache = {("a", 1, "m", "s"): {"created": __import__('time').time(), "session_id": "sid", "run_id": "rid"}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: "+json.dumps(frame)+"\n\n"))) as client:
            adapter = WPS(Transport(client), {"cookie": "wps_sid=synthetic"}, "a", 1); adapter.cache = cache
            with pytest.raises(UpstreamError) as failure:
                _ = [f async for f in adapter.chat({"model": "m", "messages": [{"role": "user", "content": "q"}]}, "s")]
            assert failure.value.code == code
            assert bool(cache) == (code == "content_filter")
    asyncio.run(run())


def test_nonstream_request_error_keeps_http_status(store):
    result = execute(store, FakeRegistry([[UpstreamError("request_rejected", 400)]]))
    collector = Collector("r", "m")
    with pytest.raises(UpstreamError) as failure:
        for item in result:
            collector.add(item)
    assert failure.value.status == 400


def test_alias_resolution_and_client_ip_survive_retry(store):
    token = real_accounts(store, 2)
    # Stub only the policy layer to exercise the gateway's actual retry arguments.
    original_acquire = store.acquire
    calls = []
    def acquire(token, model, excluded=(), request_id=None, conversation=None, client_ip=None, distinguish_busy=False, client_source=None):
        calls.append(client_ip)
        result = original_acquire(token, model, excluded, request_id, conversation, client_ip=client_ip, distinguish_busy=distinguish_busy, client_source=client_source)
        result["upstream_model"] = "official-model"
        return result
    store.acquire = acquire
    current = acquire(token, "model", client_ip="192.0.2.1")
    seen = []
    class Registry(FakeRegistry):
        def make(self, *args):
            adapter = super().make(*args)
            original = adapter.chat
            async def chat(body, session):
                seen.append(body["model"])
                async with aclosing(original(body, session)) as stream:
                    async for item in stream:
                        yield item
            adapter.chat = chat
            return adapter
    registry = Registry([[UpstreamError("network", 502, True)], [delta("OK"), {"_done": True}]])
    async def run():
        return [x async for x in Gateway(store, registry).frames(token, {"model": "model", "messages": []}, current)]
    assert asyncio.run(run())[-1] == {"finish": "stop"}
    assert seen == ["official-model", "official-model"] and calls == ["192.0.2.1", "192.0.2.1"]
