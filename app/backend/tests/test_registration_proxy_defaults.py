import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.adapters import Registry
from app.models import Resource
from app.proxy_pool import PoolInput, RouteInput, record_proxy_result, save_pool, save_route
from app.registration import RegistrationService
from app.store import DomainError, Store


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'registration-proxy.db'}", Fernet.generate_key())
    service = RegistrationService(store, Registry(enabled=False))
    nodes = [store.resource_put("proxy", f"node-{i}", {"status": "untested"}, secret=f"http://user:synthetic-{i}@proxy-{i}:8080") for i in range(3)]
    yield store, service, nodes
    store.engine.dispose()


def create(service, **material):
    return service.create("Loomy", "isolated", "sms", {"phone": "13800000000", **material})


def test_new_registration_uses_default_fixed_proxy_once_and_explicit_wins(env):
    store, service, nodes = env
    save_route(store, "Loomy", RouteInput(mode="fixed", proxy_id=nodes[0]))
    session = create(service)
    data, material = service.details(session)
    assert material["proxy_id"] == nodes[0]
    first = service.proxy(data, material)
    save_route(store, "Loomy", RouteInput(mode="fixed", proxy_id=nodes[1]))
    with store.tx() as (s, _):
        node = s.get(Resource, nodes[0])
        node.secret = store.cipher.encrypt(b"http://new-node:8080").decode()
    assert service.proxy(data, material) == first  # even credential/node refresh cannot move this login
    explicit = create(service, proxy_id=nodes[2])
    assert service.details(explicit)[1]["proxy_id"] == nodes[2]
    public = json.dumps(store.resources("registration"))
    assert "synthetic-" not in public and "13800000000" not in public


def test_new_registration_pool_chooses_healthy_node_and_pins_it(env):
    store, service, nodes = env
    pool = save_pool(store, PoolInput(name="pool", proxy_ids=nodes))["id"]
    save_route(store, "Loomy", RouteInput(mode="pool", pool_id=pool))
    aid = store.create_account("Loomy", "existing", "existing")
    endpoint = "http://user:synthetic-0@proxy-0:8080"
    record_proxy_result(store, aid, endpoint, success=False, code="network")
    session = create(service)
    data, material = service.details(session)
    assert material["proxy_id"] in nodes[1:]
    selected = service.proxy(data, material)
    save_route(store, "Loomy", RouteInput(mode="direct"))
    assert service.proxy(data, material) == selected


def test_all_failed_pool_prevents_session_and_sms_side_effects(env):
    store, service, nodes = env
    pool = save_pool(store, PoolInput(name="pool", proxy_ids=nodes))["id"]
    save_route(store, "Loomy", RouteInput(mode="pool", pool_id=pool))
    aid = store.create_account("Loomy", "existing", "existing")
    for index in range(3):
        record_proxy_result(store, aid, f"http://user:synthetic-{index}@proxy-{index}:8080", success=False, code="network")
    with pytest.raises(DomainError) as error:
        create(service)
    assert error.value.code == "proxy_pool_unavailable"
    assert store.resources("registration") == []


def test_existing_account_binding_outranks_new_material_and_platform_route(env):
    store, service, nodes = env
    aid = store.create_account("Loomy", "existing", "existing")
    store.bind_proxy(aid, nodes[0])
    save_route(store, "Loomy", RouteInput(mode="fixed", proxy_id=nodes[1]))
    session = service.create("Loomy", "relogin", "sms", {"phone": "13800000000", "proxy_id": nodes[2]}, account_id=aid)
    data, material = service.details(session)
    assert material["proxy_id"] == nodes[0]
    original = service.proxy(data, material)
    store.bind_proxy(aid, nodes[2])
    assert service.proxy(data, material) == original


def test_direct_registration_remains_direct_after_route_change(env):
    store, service, nodes = env
    session = create(service)
    data, material = service.details(session)
    save_route(store, "Loomy", RouteInput(mode="fixed", proxy_id=nodes[0]))
    assert material["_proxy_selected"] and material["proxy_id"] is None
    assert service.proxy(data, material) is None


def test_retired_pinned_node_fails_without_switching_ip(env):
    store, service, nodes = env
    session = create(service, proxy_id=nodes[0])
    data, material = service.details(session)
    with store.tx() as (s, _):
        s.get(Resource, nodes[0]).data = json.dumps({"status": "retired"})
    with pytest.raises(DomainError) as error:
        service.proxy(data, material)
    assert error.value.code == "proxy_unavailable"


def test_pool_selection_works_inside_registration_batch_transaction(env):
    store, service, nodes = env
    pool = save_pool(store, PoolInput(name="pool", proxy_ids=nodes))["id"]
    save_route(store, "Loomy", RouteInput(mode="pool", pool_id=pool))
    with store.tx() as transaction:
        identity = service.create("Loomy", "batch", "sms", {"phone": "13800000000"}, transaction=transaction)
    assert service.details(identity)[1]["proxy_id"] in nodes


def complete_login(service, sid, identity="isolated-result"):
    observed = []

    class Transport:
        async def close(self):
            pass

    def transport_factory(**kwargs):
        observed.append(kwargs["proxy"])
        return Transport()

    class Login:
        def __init__(self, transport):
            pass

        async def start(self, material, mode):
            return {"identity": identity, "credential": {"session": "synthetic-issued-session"}}

    service.registry = SimpleNamespace(enabled=True, transport_factory=transport_factory)
    service.factory = Login
    result = asyncio.run(service.run(sid))
    assert result["status"] == "succeeded"
    return result["account_id"], observed


def test_pool_registration_finishes_as_affinity_then_can_fail_over(env):
    store, service, nodes = env
    pool = save_pool(store, PoolInput(name="pool", proxy_ids=nodes))["id"]
    save_route(store, "Loomy", RouteInput(mode="pool", pool_id=pool))
    sid = create(service)
    data, material = service.details(sid)
    endpoint = service.proxy(data, material)
    aid, observed = complete_login(service, sid)
    assert observed == [endpoint]
    assert store.resources("proxy_binding") == []
    affinity = next(row for row in store.resources("proxy_affinity") if row["name"] == aid)
    assert affinity["data"] == {"pool_id": pool, "proxy_id": material["proxy_id"], "platform": "Loomy"}
    assert store.account_proxy(aid) == endpoint
    record_proxy_result(store, aid, endpoint, success=False, code="network")
    assert store.account_proxy(aid) != endpoint


@pytest.mark.parametrize("explicit", [False, True])
def test_fixed_or_explicit_registration_still_finishes_as_binding(env, explicit):
    store, service, nodes = env
    pool = save_pool(store, PoolInput(name="pool", proxy_ids=nodes))["id"]
    save_route(store, "Loomy", RouteInput(mode="pool", pool_id=pool) if explicit else RouteInput(mode="fixed", proxy_id=nodes[0]))
    # Untrusted caller hints cannot convert an explicit/fixed route to pool mode.
    sid = create(service, **({"proxy_id": nodes[1]} if explicit else {}), _proxy_origin="pool", _proxy_pool_id=pool)
    data, material = service.details(sid)
    endpoint = service.proxy(data, material)
    aid, observed = complete_login(service, sid)
    assert observed == [endpoint]
    binding = next(row for row in store.resources("proxy_binding") if row["name"] == aid)
    assert binding["data"]["proxy_id"] == (nodes[1] if explicit else nodes[0])
    assert store.resources("proxy_affinity") == []
    record_proxy_result(store, aid, endpoint, success=False, code="network")
    assert store.account_proxy(aid) == endpoint


def test_route_change_mid_registration_keeps_login_egress_but_not_permanent_binding(env):
    store, service, nodes = env
    pool = save_pool(store, PoolInput(name="pool", proxy_ids=nodes[:2]))["id"]
    save_route(store, "Loomy", RouteInput(mode="pool", pool_id=pool))
    sid = create(service)
    data, material = service.details(sid)
    endpoint = service.proxy(data, material)
    save_route(store, "Loomy", RouteInput(mode="fixed", proxy_id=nodes[2]))
    assert service.proxy(data, material) == endpoint
    aid, observed = complete_login(service, sid)
    assert observed == [endpoint]
    assert store.resources("proxy_binding") == []
    assert next(row for row in store.resources("proxy_affinity") if row["name"] == aid)["data"]["pool_id"] == pool
    assert store.account_proxy(aid) == "http://user:synthetic-2@proxy-2:8080"


def test_legacy_session_without_proxy_origin_preserves_previous_binding_rule(env):
    store, service, nodes = env
    sid = create(service, proxy_id=nodes[0])
    with store.tx() as (s, _):
        row = s.get(Resource, sid)
        material = json.loads(store.cipher.decrypt(row.secret.encode()))
        material.pop("_proxy_origin", None)
        material.pop("_proxy_pool_id", None)
        row.secret = store.cipher.encrypt(json.dumps(material).encode()).decode()
    aid, _ = complete_login(service, sid)
    assert next(row for row in store.resources("proxy_binding") if row["name"] == aid)["data"]["proxy_id"] == nodes[0]


def test_relogin_does_not_rewrite_existing_fixed_binding(env):
    store, service, nodes = env
    aid = store.create_account("Loomy", "existing", "existing")
    store.bind_proxy(aid, nodes[0])
    pool = save_pool(store, PoolInput(name="pool", proxy_ids=nodes))["id"]
    save_route(store, "Loomy", RouteInput(mode="pool", pool_id=pool))
    sid = service.create("Loomy", "relogin", "sms", {"phone":"13800000000", "proxy_id":nodes[1], "_proxy_origin":"pool", "_proxy_pool_id":pool}, account_id=aid)
    _, observed = complete_login(service, sid, identity="existing")
    assert observed == ["http://user:synthetic-0@proxy-0:8080"]
    assert next(row for row in store.resources("proxy_binding") if row["name"] == aid)["data"]["proxy_id"] == nodes[0]
    assert store.resources("proxy_affinity") == []
