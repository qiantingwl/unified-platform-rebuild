"""Transport categories come from exception types, never upstream prose."""
import asyncio
from dataclasses import asdict

import httpx
import pytest

from app.upstream import Transport, UpstreamError


ERRORS = [
    (httpx.ProxyError, "network", 502, "proxy_connect"),
    (httpx.ConnectError, "network", 502, "connect"),
    (httpx.ConnectTimeout, "upstream_timeout", 504, "connect"),
    (httpx.ReadTimeout, "upstream_timeout", 504, "read"),
    (httpx.ReadError, "network", 502, "read"),
    (httpx.RemoteProtocolError, "network", 502, "read"),
    (httpx.WriteTimeout, "upstream_timeout", 504, "write"),
    (httpx.WriteError, "network", 502, "write"),
    (httpx.PoolTimeout, "upstream_timeout", 504, "local_pool"),
]


async def invoke(transport, method):
    if method == "json":
        return await transport.json("GET", "https://synthetic.invalid/api", {"Authorization": "synthetic-secret"})
    if method == "oauth":
        return await transport.oauth_envelope("https://synthetic.invalid/oauth", {})
    return [frame async for frame in transport.sse("https://synthetic.invalid/chat", {}, b"synthetic-user-content")]


@pytest.mark.parametrize("method", ["json", "oauth", "sse"])
@pytest.mark.parametrize("exception,code,status,phase", ERRORS)
def test_typed_transport_errors_preserve_codes_without_leaking_material(method, exception, code, status, phase):
    async def run():
        async def handle(request):
            raise exception("synthetic-secret synthetic-user-content", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(UpstreamError) as caught:
                await invoke(Transport(client=client), method)
        error = caught.value
        assert (error.code, error.status, error.retryable, error.failure_phase) == (code, status, True, phase)
        public = str(error) + repr(error) + str(asdict(error))
        assert "synthetic-secret" not in public
        assert "synthetic-user-content" not in public
        assert "synthetic.invalid" not in public
    asyncio.run(run())


@pytest.mark.parametrize("method", ["json", "oauth", "sse"])
def test_http_502_is_not_proxy_connect_failure(method):
    async def run():
        async def handle(request):
            # Prose must not be used to invent a proxy connection failure.
            return httpx.Response(502, json={"message":"proxy connect timeout synthetic-secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(UpstreamError) as caught:
                await invoke(Transport(client=client), method)
        assert (caught.value.code, caught.value.failure_phase) == ("upstream_5xx", "http_status")
    asyncio.run(run())


@pytest.mark.parametrize("method", ["json", "oauth", "sse"])
def test_error_response_read_failure_remains_distinct_from_http_502(method):
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":'
            raise httpx.ReadError("synthetic-secret")
    async def run():
        async def handle(request):
            return httpx.Response(502, stream=BrokenBody())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(UpstreamError) as caught:
                await invoke(Transport(client=client), method)
        assert (caught.value.code, caught.value.failure_phase) == ("response_read_failed", "read")
    asyncio.run(run())


@pytest.mark.parametrize("method", ["json", "oauth"])
def test_whole_operation_timeout_not_misclassified_as_connection_failure(method):
    async def run():
        async def handle(request):
            raise TimeoutError("synthetic-secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(UpstreamError) as caught:
                await invoke(Transport(client=client), method)
        assert (caught.value.code, caught.value.failure_phase) == ("upstream_timeout", "operation_timeout")
    asyncio.run(run())


def test_original_positional_error_constructor_remains_compatible():
    error = UpstreamError("rate_limit", 429, True, 60)
    assert error.retry_after == 60 and error.failure_phase is None
