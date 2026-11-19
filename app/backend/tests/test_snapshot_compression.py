import asyncio
import gzip

import pytest

from app.snapshot_compression import SnapshotGZipMiddleware


PAYLOAD = b'{"accounts":[' + b'{"status":"ready","balance":123},' * 5000 + b'{}]}'


def invoke(path="/api/snapshot", encoding="gzip", payload=PAYLOAD, extra_headers=(), chunks=None, method="GET"):
    sent = []
    original = []

    async def app(scope, receive, send):
        messages = [{"type": "http.response.start", "status": 200,
                     "headers": [(b"content-type", b"application/json"),
                                 (b"content-length", str(len(payload)).encode()), *extra_headers]}]
        bodies = chunks if chunks is not None else [payload]
        messages += [{"type": "http.response.body", "body": part, "more_body": index < len(bodies)-1}
                     for index, part in enumerate(bodies)]
        for message in messages:
            original.append({**message, **({"headers": list(message["headers"])} if "headers" in message else {})})
            await send(message)

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": method, "path": path, "headers": [] if encoding is None else [(b"accept-encoding", encoding.encode())]}
    asyncio.run(SnapshotGZipMiddleware(app)(scope, receive, send))
    return sent, original


@pytest.mark.parametrize("encoding", ["gzip", "br, gzip;q=0.5", "GZip", "*;q=0.5"])
def test_snapshot_negotiates_gzip_and_preserves_json(encoding):
    sent, _ = invoke(encoding=encoding)
    headers = dict(sent[0]["headers"])
    compressed = b"".join(m["body"] for m in sent[1:])
    assert headers[b"content-encoding"] == b"gzip"
    assert b"Accept-Encoding" in headers[b"vary"]
    assert int(headers[b"content-length"]) == len(compressed)
    assert len(compressed) < len(PAYLOAD) // 10
    assert gzip.decompress(compressed) == PAYLOAD


@pytest.mark.parametrize("encoding", [None, "identity", "br", "gzip;q=0", "*;q=1, gzip;q=0", "gzip;q=bad", "gzip;q=NaN"])
def test_unnegotiated_snapshot_is_unchanged(encoding):
    sent, original = invoke(encoding=encoding)
    assert sent == original


@pytest.mark.parametrize("path", ["/api/events", "/v1/chat/completions", "/v1/messages", "/v1/responses", "/api/health"])
def test_other_streams_keep_every_message_and_placeholder_unchanged(path):
    frames = [b'data: {"delta":{"content":" "}}\n\n', b'data: {"delta":{"content":"answer"}}\n\n', b'data: [DONE]\n\n']
    sent, original = invoke(path=path, chunks=frames)
    assert sent == original


def test_small_and_already_encoded_snapshots_are_unchanged():
    sent, original = invoke(payload=b'{"ok":true}')
    assert sent == original
    sent, original = invoke(extra_headers=[(b"content-encoding", b"br")])
    assert sent == original


def test_streamed_snapshot_chunks_form_one_valid_gzip_response():
    sent, _ = invoke(chunks=[PAYLOAD[:3000], PAYLOAD[3000:]])
    assert b"content-length" not in dict(sent[0]["headers"])
    assert gzip.decompress(b"".join(m["body"] for m in sent[1:])) == PAYLOAD


def test_main_mount_compresses_snapshot_without_bypassing_login(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from app.adapters import Registry
    from app.main import create_app
    from app.store import Store

    monkeypatch.setenv("UNIFIED_SECURE_COOKIE", "0")
    store = Store(f"sqlite:///{tmp_path/'snapshot.db'}", Fernet.generate_key())
    app = create_app(store=store, registry=Registry(enabled=False), admin_token="synthetic-admin-token", role="api")
    client = TestClient(app)
    try:
        assert client.get("/api/snapshot").status_code == 401
        assert client.post("/api/session", headers={"X-Console-Client": "unified-local"},
                           json={"token": "synthetic-admin-token"}).status_code == 200
        response = client.get("/api/snapshot", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        assert int(response.headers["content-length"]) < len(response.content)
        assert response.json()["accounts"] == []
        response = client.get("/api/snapshot", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in response.headers
        assert response.json()["accounts"] == []
    finally:
        client.close()
        store.engine.dispose()


@pytest.mark.parametrize("path", ["/assets/app.js", "/assets/style.css", "/api/proxies", "/api/jobs", "/api/catalogs"])
def test_allowlisted_console_reads_compress(path):
    sent, _ = invoke(path=path)
    assert gzip.decompress(b"".join(m["body"] for m in sent[1:])) == PAYLOAD


@pytest.mark.parametrize("path,method", [("/api/session","POST"),("/api/keys","POST"),("/api/proxies","POST"),("/api/unknown","GET")])
def test_secret_creation_and_nonallowlisted_responses_bypass(path,method):
    sent, original = invoke(path=path,method=method)
    assert sent == original
