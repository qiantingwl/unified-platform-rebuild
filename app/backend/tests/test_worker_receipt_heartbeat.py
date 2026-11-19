"""A terminal receipt is not a lost lease; genuine live fencing still cancels."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.store import DomainError
from app.upstream import UpstreamError
from app.worker import MaintenanceWorker


class ReceiptStore:
    def __init__(self):
        self.terminal = False
        self.heartbeats = 0
        self.after_terminal = 0
        self.commits = 0
        self.success = None
        self.operations = {}

    def clock(self):
        return 1794900000.0

    def operation_ledger(self, account_id, key, value=None, fence=None):
        assert fence['owner']=='worker' and fence['attempt']==1 and fence['version']==1
        if value is not None:
            self.operations[key]=value
        return self.operations.get(key)

    def credential(self, *args):
        return {"session": "synthetic"}

    def account_proxy(self, *args):
        return None

    def heartbeat_job(self, job):
        self.heartbeats += 1
        if self.terminal:
            self.after_terminal += 1
            raise DomainError("stale_job_receipt")

    def finish(self, *args, **kwargs):
        self.commits += 1
        self.success = args[3]
        self.terminal = True
        # Simulate COMMIT already durable while the worker-thread result is
        # delayed. The old pulse misclassified this as losing ownership.
        time.sleep(0.06)

    def finish_login(self, *args):
        self.finish(*args[:3], True)

    def renewal_candidate(self, *args):
        return None


@pytest.fixture(autouse=True)
def isolate_task_route_lookup(monkeypatch):
    # ReceiptStore deliberately models only receipts and leases, not the route
    # database. The worker now resolves task-specific routes through this entry
    # point; keep that unrelated dependency at the existing fake's boundary.
    # Real route selection is covered by test_maintenance_proxy.py.
    monkeypatch.setattr("app.worker.resolve_maintenance_proxy",
                        lambda store, account_id, kind: store.account_proxy(account_id))


class Adapter:
    def __init__(self, failure=False):
        self.t = self
        self.failure = failure
        self.closed = False

    async def pause(self):
        await asyncio.sleep(0.035)
        if self.failure:
            raise UpstreamError("network")

    async def balance(self):
        await self.pause()
        return 42

    async def checkin(self, day):
        await self.pause()
        return {"day": day}

    async def onboarding(self):
        await self.pause()
        return {"settled": True}

    async def renew(self):
        await self.pause()
        return {"session": "synthetic-new"}

    async def validate_renewal(self, replacement):
        return True

    async def chat(self, *args):
        await self.pause()
        yield {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("kind", ["balance", "checkin", "renew", "verify", "onboarding", "invitation"])
def test_terminal_commit_and_slow_tail_do_not_report_lost_lease(monkeypatch, kind):
    store, adapter = ReceiptStore(), Adapter()
    worker = MaintenanceWorker(store, registry=SimpleNamespace(make=lambda *args: adapter))
    worker.HEARTBEAT_SECONDS = 0.01
    tail_completed = []

    def slow_proxy_record(*args, **kwargs):
        time.sleep(0.04)
        tail_completed.append(True)

    async def invitation(job):
        await adapter.pause()
        return {}

    worker.invitations = SimpleNamespace(execute_job=invitation)
    monkeypatch.setattr("app.proxy_pool.record_proxy_result", slow_proxy_record)
    job = {"id": "job", "owner": "worker", "attempt": 1, "version": 1,
           "account_id": "account", "kind": kind, "platform": "Loomy", "day": "2026-11-17", "model": "synthetic"}
    asyncio.run(worker.execute(job))
    assert store.commits == 1 and store.success is True
    assert store.heartbeats > 0 and store.after_terminal == 0
    assert worker.last_error is None
    if kind != "invitation":
        assert tail_completed == [True] and adapter.closed


def test_failed_receipt_is_also_terminal_not_lost_lease(monkeypatch):
    store, adapter = ReceiptStore(), Adapter(failure=True)
    worker = MaintenanceWorker(store, registry=SimpleNamespace(make=lambda *args: adapter))
    worker.HEARTBEAT_SECONDS = 0.01
    monkeypatch.setattr("app.proxy_pool.record_proxy_result", lambda *args, **kwargs: None)
    job = {"id": "job", "owner": "worker", "attempt": 1, "version": 1,
           "account_id": "account", "kind": "balance", "platform": "Loomy"}
    asyncio.run(worker.execute(job))
    assert store.commits == 1 and store.success is False
    assert store.heartbeats > 0 and store.after_terminal == 0
    assert worker.last_error is None and adapter.closed
