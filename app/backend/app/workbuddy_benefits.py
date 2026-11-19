"""Native personal WorkBuddy rewards with durable intent and readback.

Protocol reference: 7864 upstream/streak.go, travel.go, blackcat.go (2026-11-18).
No fabricated activity events, model calls, lottery, or automatic agreement.
"""
from datetime import datetime, timedelta, timezone
import math
from uuid import uuid4

from .upstream import UpstreamError

CST = timezone(timedelta(hours=8))
ROOT = "/activity/growth/"


class Benefits:
    def __init__(self, adapter):
        self.a = adapter

    async def read(self, path):
        value = await self.a.request("GET", self.a.base + ROOT + path, self.a.headers())
        if not isinstance(value, dict):
            raise UpstreamError("invalid_benefit_response")
        return value

    async def action(self, key, path, body, confirm, reconcile=False):
        """Never replay an ambiguous POST; reconcile against a specific fact."""
        old = await self.a.ledger("benefit:" + key) or {}
        if old.get("status") == "confirmed":
            return {"key": key, "status": "confirmed", "changed": False}
        if old.get("status") in {"submitting", "uncertain", "submitted"} or reconcile:
            confirmed = await confirm()
            result = {**old, "key": key, "status": "confirmed" if confirmed else "uncertain", "changed": bool(confirmed and old)}
            await self.a.ledger("benefit:" + key, result)
            return result
        intent = {"key": key, "status": "submitting", "client_token": str(uuid4())}
        await self.a.ledger("benefit:" + key, intent)
        try:
            # Only redemption has an upstream client_token contract.
            payload = {**body, "client_token": intent["client_token"]} if path == "redeem" else body
            await self.a.request("POST", self.a.base + ROOT + path, self.a.headers(), payload)
            await self.a.ledger("benefit:" + key, {**intent, "status": "submitted"})
            confirmed = await confirm()
            result = {**intent, "status": "confirmed" if confirmed else "uncertain", "changed": True}
        except (UpstreamError, TimeoutError) as error:
            result = {**intent, "status": "uncertain", "error": getattr(error, "code", "upstream_timeout"), "changed": True}
            await self.a.ledger("benefit:" + key, result)
            # A failed response is not proof the mutation did not happen.
            try:
                if await confirm():
                    result.update(status="confirmed", error=None)
            except (UpstreamError, TimeoutError):
                pass
        await self.a.ledger("benefit:" + key, result)
        return result

    async def streak(self, day, reconcile):
        data = await self.read("streak")
        result = []
        yesterday = (datetime.fromisoformat(day)-timedelta(days=1)).date().isoformat()
        cards = data.get("makeup_cards", {}).get("balance")
        if isinstance(cards, int) and cards > 0:
            heatmap = await self.read("heatmap")
            missed = any(isinstance(c, dict) and str(c.get("date", ""))[:10] == yesterday
                         and type(c.get("score")) in (int, float) and c["score"] == 0
                         for c in heatmap.get("cells", []))
            if missed:
                async def made_up():
                    current = await self.read("heatmap")
                    return any(isinstance(c, dict) and str(c.get("date", ""))[:10] == yesterday
                               and isinstance(c.get("score"), (int, float)) and c["score"] > 0
                               for c in current.get("cells", []))
                result.append(await self.action("makeup:"+yesterday, "makeup-cards/use",
                    {"target_date": yesterday}, made_up, reconcile))
                data = await self.read("streak")
        status = data.get("redemption_status")
        if not isinstance(status, dict):
            raise UpstreamError("invalid_benefit_response")
        # Key includes the streak window's official remaining_days marker when
        # available. Day alone cannot make an already claimed tier eligible.
        for tier in ("7d", "14d", "28d"):
            current = status.get("tier_"+tier+"_status")
            if current == "claimed":
                result.append({"key": "redeem:"+tier, "status": "confirmed", "changed": False})
                continue
            if current not in {"available", "claimable", "unlocked"}:
                result.append({"key": "redeem:"+tier, "status": "locked" if current == "locked" else "unavailable", "changed": False})
                continue
            async def redeemed(tier=tier):
                value = await self.read("streak")
                return value.get("redemption_status", {}).get("tier_"+tier+"_status") == "claimed"
            # A prior ambiguous redemption is reconciled on later days too.
            result.append(await self.action("redeem:"+tier, "redeem", {"tier": tier}, redeemed, reconcile))
        return result

    async def travel(self, day, reconcile):
        buddy = await self.read("buddy/info")
        if "buddy" not in buddy:
            raise UpstreamError("invalid_benefit_response")
        if not buddy["buddy"]:
            return {"key": "travel", "status": "material_required", "reason": "official_buddy_adoption_required", "changed": False}
        state = await self.read("buddy/travel/status")
        if state.get("state") == "traveling":
            return {"key": "travel", "status": "in_progress", "changed": False}
        if state.get("state") == "arrived":
            record = state.get("record_id")
            if type(record) is not int or record <= 0:
                raise UpstreamError("travel_record_required")
            async def claimed():
                value = await self.read("buddy/travel/status")
                return value.get("state") == "idle"
            return await self.action("travel_claim:"+str(record), "buddy/travel/claim", {"record_id": record}, claimed, reconcile)
        if state.get("state") == "idle":
            if state.get("daily_limit_reached") is True:
                return {"key": "travel", "status": "daily_limit", "changed": False}
            if state.get("daily_limit_reached") is not False:
                raise UpstreamError("invalid_benefit_response")
            async def departed():
                value = await self.read("buddy/travel/status")
                return value.get("state") in {"traveling", "arrived"} and value.get("daily_limit_reached") is True
            return await self.action("travel_depart:"+day, "buddy/travel/depart", {"location_id": 4}, departed, reconcile)
        raise UpstreamError("invalid_travel_state")


def require_personal_domestic(adapter, platform):
    account = adapter.c.get("account", {})
    account = account if isinstance(account, dict) else {}
    if platform != "WorkBuddy 国内" or account.get("enterpriseId") or account.get("enterprise_id"):
        raise UpstreamError("benefits_personal_domestic_only", 422)
    if not callable(getattr(adapter, "ledger", None)):
        raise UpstreamError("benefits_ledger_required", 422)


async def inspect(adapter, platform, *, job=None):
    """Read official state using GET only; never call an action or claim ledger."""
    require_personal_domestic(adapter, platform)
    runner = Benefits(adapter)
    tasks = []
    today = datetime.now(CST).date().isoformat()
    yesterday = (datetime.fromisoformat(today)-timedelta(days=1)).date().isoformat()
    try:
        streak = await runner.read("streak")
        tiers = streak.get("redemption_status")
        tiers = tiers if isinstance(tiers, dict) else {}
        for tier in ("7d", "14d", "28d"):
            official = tiers.get("tier_"+tier+"_status")
            known = official in {"claimed", "available", "claimable", "unlocked", "locked"} if isinstance(official,str) else False
            tasks.append({"key":"redeem:"+tier,"status":official if known else "unknown",
                "eligible":official in {"available","claimable","unlocked"} if known else None})
        makeup = streak.get("makeup_cards")
        cards = makeup.get("balance") if isinstance(makeup,dict) else None
        cards = cards if type(cards) is int and cards>=0 else None
        eligible = None
        if cards == 0:
            eligible = False
        elif cards is not None:
            heatmap = await runner.read("heatmap")
            cells = heatmap.get("cells")
            matches = [cell for cell in cells if isinstance(cell,dict) and str(cell.get("date",""))[:10]==yesterday] if isinstance(cells,list) else []
            score = matches[0].get("score") if len(matches)==1 else None
            if type(score) in (int,float) and math.isfinite(score):
                eligible = score == 0
        tasks.append({"key":"makeup:"+yesterday,"status":"available" if eligible else "unavailable" if eligible is False else "unknown",
            "eligible":eligible,"cards":cards})
    except (UpstreamError,TimeoutError) as error:
        tasks.append({"key":"streak","status":"failed","eligible":None,"error":getattr(error,"code","upstream_timeout")})
    try:
        buddy = await runner.read("buddy/info")
        if "buddy" not in buddy:
            tasks.append({"key":"travel","status":"unknown","eligible":None})
        elif not buddy["buddy"]:
            tasks.append({"key":"travel","status":"material_required","eligible":False,"reason":"official_buddy_adoption_required"})
        else:
            travel = await runner.read("buddy/travel/status")
            state = travel.get("state")
            known = state in {"idle","traveling","arrived"} if isinstance(state,str) else False
            limit = travel.get("daily_limit_reached")
            record = travel.get("record_id")
            eligible = (True if type(record) is int and record>0 else None) if state=="arrived" else False if state=="traveling" else not limit if state=="idle" and type(limit) is bool else None
            tasks.append({"key":"travel","status":state if known else "unknown","eligible":eligible})
    except (UpstreamError,TimeoutError) as error:
        tasks.append({"key":"travel","status":"failed","eligible":None,"error":getattr(error,"code","upstream_timeout")})
    failed = any(task["status"]=="failed" for task in tasks)
    unknown = any(task["status"]=="unknown" or task.get("eligible") is None for task in tasks)
    result = {"read_only":True,"status":"failed" if failed else "unknown" if unknown else "verified","day":today,
        "tasks":tasks,"error_code":"benefits_incomplete" if failed or unknown else None}
    if job:
        result.update(job_id=job["id"],credential_version=job["version"],attempt=job["attempt"])
    await adapter.ledger("benefits_status",result)
    return result


async def execute(adapter, platform, *, day=None, reconcile=False, job=None):
    require_personal_domestic(adapter, platform)
    today = datetime.now(CST).date().isoformat()
    if day and day != today:
        raise UpstreamError("benefits_business_day_changed", 409)
    day = day or today
    runner = Benefits(adapter)
    tasks = []
    for operation in (runner.streak, runner.travel):
        try:
            value = await operation(day, reconcile)
            tasks.extend(value if isinstance(value, list) else [value])
        except (UpstreamError, TimeoutError) as error:
            tasks.append({"key": operation.__name__, "status": "failed", "error": getattr(error, "code", "upstream_timeout")})
    unresolved = any(t["status"] in {"failed", "uncertain", "submitting", "submitted", "material_required", "unavailable"} for t in tasks)
    error = ("benefits_uncertain" if any(t["status"] in {"uncertain", "submitting", "submitted"} for t in tasks)
             else "benefits_material_required" if any(t["status"] == "material_required" for t in tasks)
             else "benefits_incomplete" if unresolved else None)
    result = {"tasks": tasks, "settled": not unresolved, "claimed": sum(t.get("changed", False) and t["status"] == "confirmed" for t in tasks),
              "needs_balance": any(t.get("changed") or t["status"] == "uncertain" for t in tasks), "day": day, "error_code": error}
    if job:
        result.update(job_id=job["id"], credential_version=job["version"], attempt=job["attempt"])
    await adapter.ledger("benefits", result)
    return result
