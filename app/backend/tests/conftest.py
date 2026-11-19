import itertools
import os
import tempfile
import threading
from uuid import uuid4

import httpx
import pytest

# UNIFIED_TEST_POSTGRES=1 runs every test against a fresh PostgreSQL database
# (embedded server from the pgserver package) instead of SQLite.
_EXTERNAL_POSTGRES = os.getenv("UNIFIED_TEST_DATABASE_URL")
_POSTGRES = os.getenv("UNIFIED_TEST_POSTGRES") == "1" or bool(_EXTERNAL_POSTGRES)
_counter = itertools.count()


@pytest.fixture(scope="session")
def postgres_server():
    if _EXTERNAL_POSTGRES:
        yield _EXTERNAL_POSTGRES
        return
    if not _POSTGRES:
        yield None
        return
    import pgserver
    server = pgserver.get_server(tempfile.mkdtemp(prefix="unified-pg-"), cleanup_mode="stop")
    yield server


@pytest.fixture(autouse=True)
def database_backend(postgres_server, monkeypatch):
    if postgres_server is None:
        yield
        return
    from urllib.parse import urlsplit
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    import app.store as store_module

    if isinstance(postgres_server, str):
        base_url = make_url(postgres_server).set(database="postgres")
    else:
        # pgserver may expose only a Unix socket (especially --network none).
        # Preserve its host query instead of manufacturing host=None:port=None.
        base_url = make_url(postgres_server.get_uri()).set(drivername='postgresql+psycopg', database='postgres')
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    original = store_module.Store.__init__
    created = []
    stores = []
    urls = {}
    lock = threading.Lock()

    def redirected(self, url, *args, **kwargs):
        if url.startswith("sqlite"):
            with lock:
                if url not in urls:
                    name = "t" + uuid4().hex
                    with admin.connect() as c:
                        c.execute(text(f"CREATE DATABASE {name}"))
                    created.append(name)
                    urls[url] = base_url.set(database=name).render_as_string(hide_password=False)
                url = urls[url]
        original(self, url, *args, **kwargs)
        stores.append(self)

    monkeypatch.setattr(store_module.Store, "__init__", redirected)
    try:
        yield
    finally:
        for store in stores:
            store.engine.dispose()
        with admin.connect() as c:
            for name in created:
                c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch):
    """Contract tests must use MockTransport; never send real model/SMS traffic."""
    def denied(*args, **kwargs):
        raise AssertionError("Real HTTP transport forbidden in isolated tests")
    async def denied_async(*args, **kwargs):
        raise AssertionError("Real HTTP transport forbidden in isolated tests")
    # Service defaults are production (upstream on, Secure cookie); tests run isolated over plain HTTP.
    monkeypatch.setenv("UNIFIED_ENABLE_UPSTREAM", "0")
    monkeypatch.setenv("UNIFIED_ROLE", "all")
    monkeypatch.setenv("UNIFIED_SECURE_COOKIE", "0")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied_async)


def seed_sandbox(store):
    """Synthetic accounts for state-machine tests; the product has no seeding path."""
    from app.models import Account, Credential, ModelState
    from app.store import PLATFORMS, uid
    with store.tx() as (s, state):
        for pi, platform in enumerate(PLATFORMS):
            for i in range(4):
                a = Account(id=uid(), platform=platform, external_id=f"sandbox-{pi}-{i}", label=f"synthetic {platform} {i+1:02}", sandbox=True, enabled=True,
                    credential_version=1, credential_status="invalid" if i == 3 else "valid", balance=0 if i == 2 else 2000,
                    balance_at=store.clock(), balance_version=1, checkin_day=None, created_at=store.clock(), updated_at=store.clock())
                s.add(a)
                s.add(Credential(id=uid(), account_id=a.id, version=1, ciphertext=store.cipher.encrypt(b"synthetic-no-external-credential").decode(), created_at=store.clock()))
                if i == 1:
                    s.add(ModelState(id=uid(), account_id=a.id, model="sandbox-echo", reason="permission", credential_version=1, review_at=store.clock() + 60))
                store.emit(s, state, "account", "created", a.id)
