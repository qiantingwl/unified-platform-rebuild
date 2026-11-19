"""Once-only reward receipts, adapted from Agent2Providers onboarding contracts.

Balances are always queried separately; nominal task points never become balances.
"""
import time

from .upstream import UpstreamError, amount
from .vendor.mirai.loomy import endpoints as loomy

AMBIGUOUS={"network","upstream_timeout","response_read_failed","upstream_5xx",
           "invalid_response","response_too_large","unconfirmed_onboarding"}
RACCOON_TASKS=(
    ("desktop_login_grant","首次电脑端登录奖励","desktop",{"X-Client-Name":"raccoon-work","X-Client-Platform":"desktop-linux","X-Client-Version":"v1.1.5"}),
    ("mobile_login_grant","首次手机端登录奖励","mobile",{"X-Client-Platform":"app-android","X-Client-Version":"v1.0.3"}),
)


async def load(adapter):
    if not callable(getattr(adapter,"ledger",None)):
        raise UpstreamError("onboarding_ledger_required",422)
    saved=await adapter.ledger("onboarding")
    return saved if isinstance(saved,dict) else {}


async def save(adapter,tasks,claimed=0):
    result={"tasks":tasks,"settled":bool(tasks) and all(t.get("done") is True for t in tasks),
            "claimed":claimed,"failed":sum(t.get("done") is not True for t in tasks),
            "last_checked_at":time.time()}
    await adapter.ledger("onboarding",result)
    return result


async def loomy_claim(adapter):
    saved=await load(adapter)
    if saved.get("settled") is True:
        return {**saved,"claimed":0}
    raw=await adapter.request("GET",adapter.base+loomy.ONBOARDING_TASKS_PATH,adapter.headers())
    progress=raw.get("tasks") if isinstance(raw,dict) else None
    if not isinstance(progress,dict):
        raise UpstreamError("unconfirmed_onboarding")
    tasks=[{"key":key,"title":loomy.ONBOARDING_TASK_LABELS[key],"done":progress.get(key) is True,
            "status":"settled" if progress.get(key) is True else "pending" if progress.get(key) is False else "unverified"}
           for key in loomy.ONBOARDING_TASKS]
    await save(adapter,tasks)
    claimed=0
    for task in tasks:
        # Missing fields are not proof that the task is available to claim.
        if task["status"]!="pending":
            continue
        task["status"]="claiming"
        await save(adapter,tasks,claimed)
        try:
            reply=await adapter.request("POST",adapter.base+loomy.ONBOARDING_COMPLETE_PATH,adapter.headers(),{"key":task["key"]})
            if not isinstance(reply,dict) or type(reply.get("alreadyCompleted")) is not bool:
                raise UpstreamError("unconfirmed_onboarding")
            task.update(done=True,status="settled")
            if not reply["alreadyCompleted"]:
                claimed+=1
        except (UpstreamError,TimeoutError) as error:
            code=getattr(error,"code","upstream_timeout")
            task.update(status="uncertain" if code in AMBIGUOUS else "failed",error=code)
            await save(adapter,tasks,claimed)
            if code=="invalid_credentials":
                raise
        await save(adapter,tasks,claimed)
    return await save(adapter,tasks,claimed)


async def loomy_receipt(adapter):
    """Reconcile an ambiguous grant with GET only, never replay a reward write."""
    await load(adapter)
    raw=await adapter.request("GET",adapter.base+loomy.ONBOARDING_TASKS_PATH,adapter.headers())
    progress=raw.get("tasks") if isinstance(raw,dict) else None
    if not isinstance(progress,dict):
        raise UpstreamError("unconfirmed_onboarding")
    tasks=[{"key":key,"title":loomy.ONBOARDING_TASK_LABELS[key],"done":progress.get(key) is True,
            "status":"settled" if progress.get(key) is True else "pending" if progress.get(key) is False else "uncertain"}
           for key in loomy.ONBOARDING_TASKS]
    return await save(adapter,tasks)


async def raccoon_claim(adapter):
    saved=await load(adapter)
    previous={t.get("key"):t for t in saved.get("tasks",[]) if isinstance(t,dict)}
    tasks=[{**previous.get(key,{}),"key":key,"title":title,
            "done":previous.get(key,{}).get("done") is True} for key,title,_,_ in RACCOON_TASKS]
    claimed=0
    for task,(_,_,client,client_headers) in zip(tasks,RACCOON_TASKS):
        if task["done"]:
            continue
        if task.get("status") in {"claiming","uncertain"}:
            # No read-only status endpoint exists. A lost response is not a
            # license to replay the write automatically after a restart.
            task["status"]="uncertain"
            continue
        task.update(status="claiming")
        task.pop("error",None)
        await save(adapter,tasks,claimed)  # durable intent BEFORE the POST
        try:
            data=await adapter.request("POST",adapter.base+f"/api/web/{client}/v1/login/points/grant",
                                       {**adapter.headers(),**client_headers})
            if not isinstance(data,dict) or type(data.get("granted")) is not bool:
                raise UpstreamError("unconfirmed_onboarding")
            points=None
            if data["granted"]:
                popup=data.get("popup") if isinstance(data.get("popup"),dict) else {}
                try:
                    points=amount(popup.get("points"))
                except UpstreamError:
                    pass  # no nominal fallback masquerading as actual credited points
                claimed+=1
            task.update(done=True,status="settled",granted=data["granted"],points=points if data["granted"] else 0)
        except (UpstreamError,TimeoutError) as error:
            code=getattr(error,"code","upstream_timeout")
            task.update(status="uncertain" if code in AMBIGUOUS else "failed",error=code)
            await save(adapter,tasks,claimed)
            if code=="invalid_credentials":
                raise
        await save(adapter,tasks,claimed)
    return await save(adapter,tasks,claimed)
