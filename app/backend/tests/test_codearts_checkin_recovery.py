"""CodeArts daily receipts need dated intent plus upstream confirmation."""
import asyncio
import copy

import pytest

from app.adapters import CodeArts
from app.upstream import UpstreamError

DAY = "2026-11-17"


def row(status="AVAILABLE", claimable=True, cid="daily"):
    return {"campaignId": cid, "type": "USER_LOGIN", "benefitUnit": "CREDIT",
            "status": status, "claimable": claimable}


def adapter_with_ledger(items, saved=None):
    adapter = CodeArts(None, {}, "synthetic")
    saved = {} if saved is None else saved
    calls = []

    async def ledger(key, value=None):
        if value is not None:
            saved[key] = copy.deepcopy(value)
        return copy.deepcopy(saved.get(key))

    async def read(path):
        calls.append(("GET", path))
        return {"items": copy.deepcopy(items)}

    async def post(path, data):
        calls.append(("POST", path))
        return {"campaignId": data["campaignId"]}

    adapter.read, adapter.post, adapter.ledger = read, post, ledger
    return adapter, saved, calls


@pytest.mark.parametrize("status", ["CLAIMED", "CONFIRMED", "CONSUMED"])
def test_undated_old_activity_is_not_confirmed_as_today(status):
    async def run():
        adapter, saved, calls = adapter_with_ledger([row(status, False)])
        with pytest.raises(UpstreamError, match="unconfirmed_checkin"):
            await adapter.checkin(DAY)
        assert not any(method == "POST" for method, _ in calls)
        assert "checkin:" + DAY not in saved
    asyncio.run(run())


def test_crash_after_confirm_recovers_by_readback_without_reclaim():
    async def run():
        items = [row()]
        adapter, saved, calls = adapter_with_ledger(items)

        async def post(path, data):
            # The intent must survive before any external mutation.
            assert saved["checkin_plan:" + DAY]["claim_ids"] == ["daily"]
            calls.append(("POST", path))
            if path.endswith("confirm"):
                items[:] = [row("CONFIRMED", False)]
                raise asyncio.CancelledError()
            return {"campaignId": "daily"}

        adapter.post = post
        with pytest.raises(asyncio.CancelledError):
            await adapter.checkin(DAY)
        assert "checkin:" + DAY not in saved
        restarted, _, readback_calls = adapter_with_ledger(items, saved)
        assert await restarted.checkin_receipt(DAY) == {"day": DAY}
        assert saved["checkin:" + DAY] == {"day": DAY}
        assert readback_calls == [("GET", "/v1/ops/delivery?channel=IDE")]
        # An ordinary scheduler retry uses the saved receipt without a new claim.
        assert await restarted.checkin(DAY) == {"day": DAY}
        assert len(readback_calls) == 1
    asyncio.run(run())


def test_unconfirmed_readback_does_not_mutate_or_credit():
    async def run():
        saved = {"checkin_plan:" + DAY: {"day": DAY, "campaign_ids": ["daily"], "claim_ids": ["daily"]}}
        adapter, saved, calls = adapter_with_ledger([row("CLAIMED", False)], saved)
        with pytest.raises(UpstreamError, match="unconfirmed_checkin"):
            await adapter.checkin_receipt(DAY)
        assert "checkin:" + DAY not in saved
        assert all(method == "GET" for method, _ in calls)
    asyncio.run(run())


def test_one_confirmed_campaign_cannot_cover_missing_campaign():
    async def run():
        saved = {"checkin_plan:" + DAY: {"day": DAY, "campaign_ids": ["daily", "other"], "claim_ids": ["daily", "other"]}}
        adapter, saved, _ = adapter_with_ledger([row("CONFIRMED", False)], saved)
        with pytest.raises(UpstreamError, match="unconfirmed_checkin"):
            await adapter.checkin_receipt(DAY)
        assert "checkin:" + DAY not in saved
    asyncio.run(run())


def test_previous_day_plan_does_not_prove_today():
    async def run():
        saved = {"checkin_plan:2026-11-16": {"day": "2026-11-16", "campaign_ids": ["daily"], "claim_ids": ["daily"]}}
        adapter, saved, calls = adapter_with_ledger([row("CONFIRMED", False)], saved)
        with pytest.raises(UpstreamError, match="unconfirmed_checkin"):
            await adapter.checkin_receipt(DAY)
        assert not calls and "checkin:" + DAY not in saved
    asyncio.run(run())


def test_missing_claim_response_reuses_durable_idempotency_key():
    async def run():
        items = [row()]
        adapter, saved, _ = adapter_with_ledger(items)
        keys = []

        async def post(path, data):
            if path.endswith("claim"):
                keys.append(data["idempotentKey"])
                if len(keys) == 1:
                    raise UpstreamError("network")
            else:
                items[:] = [row("CONFIRMED", False)]
            return {"campaignId": "daily"}

        adapter.post = post
        with pytest.raises(UpstreamError, match="network"):
            await adapter.checkin(DAY)
        assert await adapter.checkin(DAY) == {"day": DAY}
        assert len(keys) == 2 and keys[0] == keys[1]
    asyncio.run(run())
