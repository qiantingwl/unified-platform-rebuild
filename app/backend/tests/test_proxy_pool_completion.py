import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.proxy_pool import PoolInput, RouteInput, record_proxy_result, resolve_account_proxy, save_pool, save_route
from app.store import DomainError, Store
from app.models import Account, Lease, RequestAttempt, Resource
from app.gateway import Gateway
from test_gateway import FakeRegistry, delta, real_accounts


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path/'proxy.db'}", Fernet.generate_key())
    yield s
    s.engine.dispose()


def setup_pool(store):
    account = store.create_account("Loomy", "pool-user", "pool-user")
    nodes = [store.resource_put("proxy", f"node-{n}", {"scheme": "http", "status": "untested"}, secret=f"http://user:synthetic-secret@proxy{n}:8080") for n in range(3)]
    pool = save_pool(store, PoolInput(name="pool", proxy_ids=nodes))["id"]
    save_route(store, "Loomy", RouteInput(mode="pool", pool_id=pool))
    return account, nodes, pool


def test_pool_affinity_survives_new_store_instance(store):
    account, nodes, pool = setup_pool(store)
    first = resolve_account_proxy(store, account)
    assert resolve_account_proxy(store, account) == first
    second = Store(store.engine.url.render_as_string(hide_password=False), Fernet.generate_key())
    # Use the existing cipher, just as a restarted app loads its configured key.
    second.cipher = store.cipher
    try:
        assert resolve_account_proxy(second, account) == first
    finally:
        second.engine.dispose()
    assert len(store.resources("proxy_affinity")) == 1


def test_explicit_binding_wins_and_never_changes_when_pool_changes(store):
    account, nodes, pool = setup_pool(store)
    store.bind_proxy(account, nodes[0])
    original = resolve_account_proxy(store, account)
    save_route(store, "Loomy", RouteInput(mode="direct"))
    assert resolve_account_proxy(store, account) == original
    record_proxy_result(store, account, original, success=False, code="network")
    assert resolve_account_proxy(store, account) == original


def test_transport_failure_rotates_pool_and_remains_sticky(store):
    account, nodes, pool = setup_pool(store)
    first = resolve_account_proxy(store, account)
    record_proxy_result(store, account, first, success=False, code="network")
    replacement = resolve_account_proxy(store, account)
    assert replacement != first
    assert resolve_account_proxy(store, account) == replacement
    record_proxy_result(store, account, first, success=True)
    assert resolve_account_proxy(store, account) == replacement


@pytest.mark.parametrize("code", ["unknown_403", "permission", "invalid_credentials", "quota", "rate_limit", "upstream_5xx", "startup_timeout"])
def test_account_or_upstream_error_does_not_poison_proxy(store, code):
    account, nodes, pool = setup_pool(store)
    endpoint = resolve_account_proxy(store, account)
    record_proxy_result(store, account, endpoint, success=False, code=code)
    assert resolve_account_proxy(store, account) == endpoint
    assert store.resources("proxy_health") == []


def test_exhausted_pool_returns_error_not_direct_and_recovers_after_deadline(store):
    account, nodes, pool = setup_pool(store)
    now = store.clock()
    store.clock = lambda: now
    for _ in range(3):
        record_proxy_result(store, account, resolve_account_proxy(store, account), success=False, code="network")
    with pytest.raises(DomainError) as caught:
        resolve_account_proxy(store, account)
    assert caught.value.code == "proxy_pool_unavailable"
    store.clock = lambda: now + 31
    assert resolve_account_proxy(store, account).startswith("http://")


def test_retired_subscription_binding_does_not_fall_back(store):
    account, nodes, pool = setup_pool(store)
    store.bind_proxy(account, nodes[0])
    with store.tx() as (s, _):
        node = s.get(Resource, nodes[0])
        node.data = json.dumps({"status": "retired"})
    with pytest.raises(DomainError) as caught:
        resolve_account_proxy(store, account)
    assert caught.value.code == "proxy_retired"


def test_connectivity_and_model_success_are_separate_and_secret_free(store):
    account, nodes, pool = setup_pool(store)
    endpoint = resolve_account_proxy(store, account)
    record_proxy_result(store, account, endpoint, success=True)
    assert store.resources("proxy_model_check") == []
    record_proxy_result(store, account, endpoint, success=True, model="deepseek", model_verified=True)
    checks = store.resources("proxy_model_check")
    assert len(checks) == 1 and checks[0]["data"]["effective_output"] is True
    public = json.dumps(store.resources("proxy_health") + checks + store.resources("proxy_affinity"))
    assert "synthetic-secret" not in public and endpoint not in public


def test_pool_api_configures_routes_and_validates_node_ids(store):
    account, nodes, pool = setup_pool(store)
    app = create_app(store, start_worker=False)
    with TestClient(app, headers={"X-Console-Client": "unified-local"}) as client:
        bad = client.post("/api/proxy-pools", json={"name": "bad", "proxy_ids": ["not-a-proxy"]})
        assert bad.status_code == 422
        result = client.get("/api/proxy-pools")
        assert result.status_code == 200 and len(result.json()["pools"]) == 1
        response = client.put("/api/proxy-routes/Loomy", json={"mode": "direct"})
        assert response.status_code == 200
        assert resolve_account_proxy(store, account) is None


def test_failed_fixed_route_switches_account_without_rebinding_or_direct_retry(store):
    token = real_accounts(store)
    current = store.acquire(token, "model")
    node = store.resource_put("proxy", "unavailable fixed", {"status": "retired"}, secret="http://synthetic:8080")
    store.bind_proxy(current["account_id"], node)
    registry = FakeRegistry([[delta("works"), {"_done": True}]])

    async def run():
        return [item async for item in Gateway(store, registry).frames(token, {"model": "model"}, current)]

    frames = asyncio.run(run())
    assert frames[-1] == {"finish": "stop"}
    assert registry.calls[0] != current["account_id"]
    assert store.resources("proxy_binding")[0]["data"]["proxy_id"] == node
    with store.read() as (s, _):
        attempts = list(s.scalars(select(RequestAttempt)))
        assert len(attempts) == 2
        assert next(a for a in attempts if a.account_id == current["account_id"]).code == "proxy_retired"


def hold_connection(store, account, endpoint):
    record_proxy_result(store, account, endpoint, success=False, code="network", failure_phase="proxy_connect")


def test_known_failed_fixed_proxies_do_not_spend_gateway_attempts(store):
    token = real_accounts(store, n=4)
    with store.tx() as (s, _):
        accounts = list(s.scalars(select(Account).order_by(Account.external_id)))
        for i, account in enumerate(accounts):
            account.priority = 100 - i
        bad = [a.id for a in accounts[:3]]
        good = accounts[3].id
    for i, account in enumerate(bad):
        url = f"http://fixture-down-{i}.invalid:8080"
        node = store.resource_put("proxy", f"down-{i}", {"status": "untested"}, secret=url)
        store.bind_proxy(account, node)
        hold_connection(store, account, url)
    registry = FakeRegistry([[delta("answer"), {"_done": True}]])
    current = store.acquire(token, "model")

    async def run():
        return [frame async for frame in Gateway(store, registry).frames(token, {"model": "model"}, current)]

    frames = asyncio.run(run())
    assert frames[0]["placeholder"] and frames[-1] == {"finish": "stop"}
    assert registry.calls == [good]
    with store.read() as (s, _):
        attempts = list(s.scalars(select(RequestAttempt)))
        assert [a.account_id for a in attempts] == [good]
        assert not list(s.scalars(select(Lease)))
        assert all(a.enabled for a in s.scalars(select(Account)))


@pytest.mark.parametrize("recovered_by", ["retry_deadline", "new_node_version"])
def test_platform_fixed_hold_respects_account_override_and_recovery(store, recovered_by):
    token = real_accounts(store, n=2)
    t = store.clock()
    store.clock = lambda: t
    with store.tx() as (s, _):
        first, second = list(s.scalars(select(Account).order_by(Account.external_id)))
        first.priority = 99
        preferred, override = first.id, second.id
    primary = "http://fixture-primary.invalid:8080"
    node = store.resource_put("proxy", "primary", {"status": "untested"}, secret=primary)
    alternate = store.resource_put("proxy", "alternate", {"status": "untested"}, secret="http://fixture-alt.invalid:8080")
    save_route(store, "Loomy", RouteInput(mode="fixed", proxy_id=node))
    store.bind_proxy(override, alternate)
    before_failure = store.acquire(token, "model", conversation="sticky")
    assert before_failure["account_id"] == preferred
    store.end_request(before_failure["id"], "cancelled")

    # An observation on another platform and a permission response are not
    # connection holds on this platform, even when the node ID is shared.
    another_platform = store.create_account("Marvis", "other", "other")
    hold_connection(store, another_platform, primary)
    record_proxy_result(store, preferred, primary, success=False, code="unknown_403", failure_phase="http_status")
    still_eligible = store.acquire(token, "model", conversation="sticky")
    assert still_eligible["account_id"] == preferred
    store.end_request(still_eligible["id"], "cancelled")

    hold_connection(store, preferred, primary)
    during = store.acquire(token, "model", conversation="sticky")
    assert during["account_id"] == override
    store.end_request(during["id"], "cancelled")
    with pytest.raises(DomainError, match="proxy_unavailable"):
        store.acquire(token, "model", excluded=(override,))

    if recovered_by == "retry_deadline":
        t += 30
    else:
        store.resource_put("proxy", "primary", {"status": "untested"}, secret="http://fixture-new.invalid:8080",
                           resource_id=node, expected_version=1)
    recovered = store.acquire(token, "model", conversation="new-conversation")
    assert recovered["account_id"] == preferred
    store.end_request(recovered["id"], "cancelled")


@pytest.mark.parametrize("phase", ["connect", "proxy_connect"])
def test_fixed_connection_failure_blocks_only_same_endpoint_version_until_retry(store, phase):
    from app.proxy_pool import resolve_explicit_proxy, select_registration_proxy

    aid, nodes, _ = setup_pool(store)
    store.bind_proxy(aid, nodes[0])
    now = store.clock()
    store.clock = lambda: now
    endpoint = resolve_account_proxy(store, aid)
    route = {"mode": "fixed", "proxy_id": nodes[0]}

    record_proxy_result(store, aid, endpoint, success=False, code="unknown_403", failure_phase="http_status")
    record_proxy_result(store, aid, endpoint, success=False, code="upstream_timeout", failure_phase="read")
    assert resolve_account_proxy(store, aid) == endpoint

    record_proxy_result(store, aid, endpoint, success=False, code="upstream_timeout", failure_phase=phase)
    with pytest.raises(DomainError, match="proxy_unavailable"):
        resolve_account_proxy(store, aid)
    with pytest.raises(DomainError, match="proxy_unavailable"):
        resolve_explicit_proxy(store, aid, route, affinity_scope="maintenance:balance")
    with store.read() as (session, _):
        with pytest.raises(DomainError, match="proxy_unavailable"):
            select_registration_proxy(store, session, "Loomy", "session", account_id=aid)
    store.clock = lambda: now + 31
    assert resolve_account_proxy(store, aid) == endpoint

    record_proxy_result(store, aid, endpoint, success=False, code="network", failure_phase=phase)
    replacement = "http://new-fixture-proxy:8080"
    store.resource_put("proxy", "node-0", {"scheme": "http", "status": "untested"}, secret=replacement,
                       resource_id=nodes[0], expected_version=1)
    assert resolve_account_proxy(store, aid) == replacement
