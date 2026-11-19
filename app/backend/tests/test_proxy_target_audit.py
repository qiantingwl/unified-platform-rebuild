"""Independent state-change and large-source audit; all transport is synthetic."""
import asyncio
import json

import pytest

from app.models import Resource
from app.proxy_target_health import ProxyTargetHealth
from app.proxy_target_scheduler import BATCH_KIND, ProxyTargetScheduler
from app.store import DomainError
from test_proxy_target_scheduler import env, Checker, attach_subscription, receipt


def modify(store, identity, **values):
    # Deliberately do not bump version: lifecycle state must fence receipts too.
    with store.tx() as (session, _):
        row = session.get(Resource, identity)
        row.data = json.dumps(json.loads(row.data) | values)


@pytest.mark.parametrize("status", ["disabled", "retired", "pending_kernel"])
def test_same_version_unavailable_node_never_probed(env, status):
    store, nodes, _ = env
    checker = Checker(store)
    scheduler = ProxyTargetScheduler(store, checker)
    scheduler.queue(nodes[:1], ["Loomy"])
    modify(store, nodes[0], status=status)
    assert asyncio.run(scheduler.tick()) == 1
    assert checker.calls == []
    with pytest.raises(DomainError):
        ProxyTargetHealth(store)._begin(nodes[0], "Loomy")


@pytest.mark.parametrize("status", ["disabled", "retired", "pending_kernel"])
def test_same_version_lifecycle_change_invalidates_inflight_success(env, status):
    store, nodes, _ = env
    checker = ProxyTargetHealth(store)
    snapshot = checker._begin(nodes[0], "Loomy")
    modify(store, nodes[0], status=status)
    result = checker._finish(snapshot, checker._base(snapshot) | {
        "status": "completed", "code": "target_http_ok", "eligible": True})
    assert result["status"] == "stale"
    assert result["eligible"] is False


def test_removed_source_membership_does_not_probe_shared_node(env):
    store, nodes, _ = env
    source = attach_subscription(store, nodes[:1], auto_target_check=True, target_platforms=["Loomy"])
    other = store.resource_put("proxy_subscription", "other", {"auto_target_check": False})
    checker = Checker(store)
    scheduler = ProxyTargetScheduler(store, checker)
    assert scheduler.discover(force=True) == 1
    modify(store, nodes[0], subscription_ids=[other])
    assert asyncio.run(scheduler.tick()) == 1
    assert checker.calls == []
    assert store.resources(BATCH_KIND)[0]["data"]["source_subscription"] == source


def test_5000_shared_nodes_prioritize_untouched_tail_and_deduplicate(env):
    store, _, now = env
    first = store.resource_put("proxy_subscription", "first", {"auto_target_check": True, "target_platforms": ["Loomy"]})
    second = store.resource_put("proxy_subscription", "second", {"auto_target_check": True, "target_platforms": ["Loomy"]})
    # Bulk fixture avoids thousands of independent commits; not production code.
    encrypted = store.cipher.encrypt(b"http://synthetic.invalid:8080").decode()
    identities = [f"audit-{i:05d}" for i in range(5000)]
    with store.tx() as (session, _):
        session.add_all([Resource(id=i, name=i, kind="proxy", secret=encrypted,
            data=json.dumps({"status": "untested", "target_check_required": True, "subscription_ids": [first, second]}),
            updated_at=now[0]) for i in identities])
    for identity in identities[:4]:
        receipt(store, identity, eligible=False, checked_at=now[0]-1800)
    scheduler = ProxyTargetScheduler(store, Checker(store))
    assert scheduler.discover(force=True) == 2
    batches = store.resources(BATCH_KIND)
    pairs = [(i["proxy_id"], i["platform"]) for b in batches for i in b["data"]["items"]]
    assert len(pairs) == 512 == len(set(pairs))
    assert not set(identities[:4]).intersection(i for i, _ in pairs)
    assert all(b["data"]["total"] == 256 for b in batches)
