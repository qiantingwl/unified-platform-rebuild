import asyncio
import copy
import json

import httpx
import pytest

from app.adapters import WorkBuddy, WorkBuddyInternational
from app.upstream import Transport, UpstreamError, frame_failure, structured_business


@pytest.mark.parametrize("adapter_type", [WorkBuddy, WorkBuddyInternational])
def test_workbuddy_wire_shapes_preserve_text_history_and_cache(adapter_type):
    body = {"model": "model", "prompt_cache_key": "client-cache", "max_completion_tokens": 256,
            "messages": [{"role": "developer", "content": "unchanged instruction"},
                         {"role": "user", "content": [{"type": "image_url", "image_url": "https://example.invalid/image"}]},
                         {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "find", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": "c", "content": "unchanged result"}],
            "tools": [{"type": "function", "function": {"name": "find", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "find"}}}
    before = copy.deepcopy(body)
    async def run():
        adapter = adapter_type(None, {"auth": {"accessToken": "synthetic", "domain": "example.invalid"}, "account": {"uid": "synthetic"}}, "a")
        _, headers, outgoing = await adapter.prepare(body, "session")
        assert outgoing["messages"][0] == {"role": "system", "content": "unchanged instruction"}
        assert outgoing["messages"][1]["content"][0]["image_url"] == {"url": "https://example.invalid/image"}
        assert outgoing["messages"][2:] == before["messages"][2:]
        assert len(outgoing["messages"]) == len(before["messages"])
        assert outgoing["tool_choice"] == "find" and outgoing["tools"] == before["tools"]
        assert outgoing["max_tokens"] == 256 and "max_completion_tokens" not in outgoing
        assert outgoing["prompt_cache_key"] == "client-cache" and headers["X-Conversation-ID"] == "session"
        assert outgoing["stream_options"] == {"include_usage": True}
    asyncio.run(run())
    assert body == before


def test_workbuddy_does_not_inject_system_or_override_explicit_options():
    body = {"messages": [{"role": "user", "content": "original"}], "max_tokens": 50,
            "max_completion_tokens": 500, "stream_options": {"include_usage": False}}
    result = WorkBuddy.outbound(body)
    assert result["messages"] == body["messages"]
    assert result["max_tokens"] == 50 and result["stream_options"] == {"include_usage": False}


@pytest.mark.parametrize("choice", ["none", {"type": "none"}])
def test_no_tools_contract_preserves_historical_tool_results(choice):
    messages = [{"role": "tool", "tool_call_id": "c", "content": "past result"}]
    result = WorkBuddy.outbound({"messages": messages, "tools": [{"type": "function"}], "tool_choice": choice})
    assert "tools" not in result and "tool_choice" not in result
    assert result["messages"] == messages


@pytest.mark.parametrize("choice", [{"type": "unknown"}, {"type": "function"}, [], None])
def test_unrepresentable_tool_choice_is_explicit_not_silently_auto(choice):
    with pytest.raises(UpstreamError) as failure:
        WorkBuddy.outbound({"messages": [], "tool_choice": choice})
    assert failure.value.code == "unsupported_protocol_field" and failure.value.status == 422


@pytest.mark.parametrize("envelope", [{"code": 11128}, {"data": {"code": "11128"}}, {"code": 400, "error": {"data": {"code": 11128}}}, {"response": {"data": {"code": 11128}}}])
def test_top_level_and_nested_business_errors_do_not_become_empty_success(envelope):
    assert frame_failure(envelope).code == "request_rejected"


def test_generated_business_code_is_not_an_error():
    assert frame_failure({"choices": [{"delta": {"content": '{"code":11128}'}}]}) is None
    assert structured_business({"choices": [], "code": 11128}) == 11128
    assert structured_business({"choices": [], "data": {"code": 11128}}) is None
    assert frame_failure({"code": 0, "data": {"text": "answer"}}) is None
    node = {"code": 11128}
    for _ in range(8):
        node = {"data": node}
    assert structured_business(node) is None


@pytest.mark.parametrize("envelope", [
    {"choices": [], "code": 11128},
    {"choices": [], "error": {"code": 11128}},
    {"delta": {}, "error": {"data": {"code": 11128}}},
])
def test_explicit_error_beside_empty_choices_or_delta_is_not_hidden(envelope):
    failure = frame_failure(envelope)
    assert failure.code == "request_rejected" and failure.status == 400


def test_explicit_403_error_beside_choices_preserves_business_code():
    failure = frame_failure({"choices": [], "status": 403, "error": {"code": 11140}})
    assert failure.code == "permission" and failure.status == 403


@pytest.mark.parametrize("wire", ["http", "json", "sse"])
def test_error_contract_is_consistent_across_http_and_stream_envelopes(wire):
    async def run():
        async def handler(request):
            value = {"error": {"status": 403, "data": {"code": 11140}}}
            if wire == "sse":
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(value) + "\n\n")
            return httpx.Response(403 if wire == "http" else 200, json=value)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WorkBuddyInternational(Transport(client), {"auth": {"accessToken": "synthetic", "domain": "example.invalid"}, "account": {"uid": "synthetic"}}, "a")
            with pytest.raises(UpstreamError) as failure:
                _ = [item async for item in adapter.chat({"model": "m", "messages": [{"role": "user", "content": "synthetic"}]}, "s")]
            assert failure.value.code == "permission" and failure.value.status == 403
    asyncio.run(run())


@pytest.mark.parametrize("platform,first_role", [("WorkBuddy 国内", "system"), ("WorkBuddy 国际", "system"), ("Loomy", "user")])
@pytest.mark.parametrize("reply,success", [("OK", True), (" ", False)])
def test_recovery_probe_has_its_own_contract_and_requires_real_answer(monkeypatch, platform, first_role, reply, success):
    from app.worker import MaintenanceWorker
    monkeypatch.setattr("app.proxy_pool.record_proxy_result", lambda *args, **kwargs: None)
    captured, results = [], []
    class Store:
        def credential(self, *args): return {}
        def account_proxy(self, *args): return None
        def finish(self, *args, **kwargs): results.append((args, kwargs))
    class Adapter:
        def __init__(self): self.t = self
        async def close(self): pass
        async def chat(self, body, session):
            captured.append(copy.deepcopy(body))
            yield {"choices": [{"delta": {"content": reply}}]}
            yield {"_done": True}
    class Registry:
        def make(self, *args): return Adapter()
    job = {"kind": "verify", "platform": platform, "model": "m", "account_id": "a", "version": 1, "id": "j", "owner": "w", "attempt": 1}
    asyncio.run(MaintenanceWorker(Store(), registry=Registry()).execute_real(job))
    assert captured[0]["messages"][0]["role"] == first_role
    assert captured[0]["messages"][-1] == {"role": "user", "content": "Reply with OK."}
    assert results[0][0][-1] is success
    if success:
        assert results[0][1]["effective_answer"] is True
    else:
        assert results[0][1]["error"] == "no_effective_output"
