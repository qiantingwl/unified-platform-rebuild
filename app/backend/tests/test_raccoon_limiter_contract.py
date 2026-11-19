import asyncio

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.gateway import Gateway
from app.models import ModelState
from app.store import Store
from app.upstream import Transport, UpstreamError, classify, frame_failure, payload
from test_gateway import FakeRegistry, delta, real_accounts


@pytest.mark.parametrize("body", [
    {"code": "limiter_request_error", "message": "retry after:9s"},
    {"code": 500, "message": "limiter_request_error: request exceed max time, retry after:9s"},
    {"error": {"type": "limiter_request_error", "message": "retry after:9s"}},
    {"code": 200, "data": {"code": 500, "message": "limiter_request_error: retry after:9s"}},
])
def test_json_and_stream_envelopes_classify_exact_limiter(body):
    with pytest.raises(UpstreamError) as error:
        payload(body)
    assert (error.value.code, error.value.retry_after, error.value.retryable) == ("rate_limit", 9, True)
    failure = frame_failure(body)
    assert (failure.code, failure.retry_after) == ("rate_limit", 9)


@pytest.mark.parametrize("http_status", [400, 500])
def test_http_error_envelope_honors_header_then_body_retry(http_status):
    async def run():
        for header, expected in [({}, 9), ({"Retry-After": "12"}, 12)]:
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(http_status, headers=header, json={"code": 500, "message": "limiter_request_error: retry after:9s"})))
            try:
                with pytest.raises(UpstreamError) as error:
                    await Transport(client).json("POST", "https://example.test/chat")
                assert (error.value.code, error.value.retry_after) == ("rate_limit", expected)
            finally:
                await client.aclose()
    asyncio.run(run())


def test_normal_generated_content_and_generic_retry_text_are_not_limiter_errors():
    assert frame_failure({"choices": [{"delta": {"content": "limiter_request_error: retry after:9s"}}]}) is None
    assert frame_failure({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '{"code":"limiter_request_error"}'}}]}}]}) is None
    assert frame_failure({"code": 0, "message": "limiter_request_error: retry after:9s", "data": {}}) is None
    assert frame_failure({"code": 400, "message": "max_tokens exceed max time; retry after:9s"}).code == "upstream_rejected"
    assert classify(401, "limiter_request_error").code == "invalid_credentials"
    assert classify(402, "limiter_request_error").code == "quota"
    assert classify(403, "limiter_request_error").code == "unknown_403"


@pytest.mark.parametrize("text,delay", [("retry-after: 500ms", 1), ("retry after:99999s", 3600), ("no delay supplied", 1)])
def test_delay_bounds_are_existing_classification_policy(text, delay):
    assert frame_failure({"error": {"type": "limiter_request_error", "message": text}}).retry_after == delay


def test_limiter_rotates_account_without_waiting_for_retry_after(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'limiter.db'}", Fernet.generate_key())
    try:
        token = real_accounts(store, 2)
        current = store.acquire(token, "model")
        registry = FakeRegistry([[frame_failure({"error": {"type": "limiter_request_error", "message": "retry after:9s"}})], [delta("OK"), {"_done": True}]])
        async def run():
            async with asyncio.timeout(2):
                return [item async for item in Gateway(store, registry).frames(token, {"model": "model", "messages": [{"role": "user", "content": "synthetic"}]}, current)]
        frames = asyncio.run(run())
        assert len(registry.calls) == 2 and registry.calls[0] != registry.calls[1]
        assert any(item.get("delta", {}).get("content") == "OK" for item in frames)
        with store.read() as (session, _):
            block = session.scalar(select(ModelState).where(ModelState.account_id == current["account_id"]))
            assert block.reason == "rate_limit" and block.review_at > store.clock()
    finally:
        store.engine.dispose()
