import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.models import Resource
from app.proxy_pool import PoolInput, RouteInput, save_pool, save_route
from app.proxy_target_health import KIND
from app.proxy_target_scheduler import BATCH_KIND, ProxyTargetScheduler
from app.registration import RegistrationService
from app.adapters import Registry
from app.store import DomainError, Store


@pytest.fixture
def env(tmp_path):
    now=[1800000000.]
    store=Store(f"sqlite:///{tmp_path/'target.db'}",Fernet.generate_key(),clock=lambda:now[0])
    nodes=[store.resource_put("proxy",f"node-{i}",{"status":"untested","target_check_required":True},secret=f"http://node-{i}:8080") for i in range(4)]
    yield store,nodes,now
    store.engine.dispose()


def receipt(store,node,platform="Loomy",**overrides):
    with store.tx() as (s,_):
        version=s.get(Resource,node).version
    data={"proxy_id":node,"platform":platform,"status":"completed","eligible":True,"node_version":version,"checked_at":store.clock(),"code":"target_http_ok"}|overrides
    store.resource_put(KIND,f"{node}:{platform}",data)


def test_public_target_required_for_binding_fixed_pool_and_registration(env):
    store,nodes,now=env
    aid=store.create_account("Loomy","isolated","isolated")
    store.bind_proxy(aid,nodes[0])
    with pytest.raises(DomainError) as error:store.account_proxy(aid)
    assert error.value.code=="proxy_target_unverified"
    receipt(store,nodes[0],platform="WPS 灵犀")
    with pytest.raises(DomainError):store.account_proxy(aid)
    receipt(store,nodes[0])
    assert store.account_proxy(aid)=="http://node-0:8080"
    store.bind_proxy(aid,None)
    save_route(store,"Loomy",RouteInput(mode="fixed",proxy_id=nodes[1]))
    with pytest.raises(DomainError):store.account_proxy(aid)
    pool=save_pool(store,PoolInput(name="p",proxy_ids=nodes))["id"]
    save_route(store,"Loomy",RouteInput(mode="pool",pool_id=pool))
    assert store.account_proxy(aid)=="http://node-0:8080"
    service=RegistrationService(store,Registry(enabled=False))
    with pytest.raises(DomainError):service.create("Loomy","r","sms",{"phone":"13800000000","proxy_id":nodes[2]})
    assert not store.resources("registration")
    sid=service.create("Loomy","r","sms",{"phone":"13800000000"})
    assert service.details(sid)[1]["proxy_id"]==nodes[0]
    now[0]+=86401
    with pytest.raises(DomainError):store.account_proxy(aid)


def test_old_nodes_are_not_forced_to_recheck_and_changed_version_invalidates_public(env):
    store,nodes,now=env
    old=store.resource_put("proxy","old",{"status":"untested"},secret="http://old:8080")
    aid=store.create_account("Loomy","a","a")
    store.bind_proxy(aid,old)
    assert store.account_proxy(aid)=="http://old:8080"
    store.bind_proxy(aid,nodes[0]);receipt(store,nodes[0])
    with store.tx() as (s,_):s.get(Resource,nodes[0]).version+=1
    with pytest.raises(DomainError):store.account_proxy(aid)


def attach_subscription(store,nodes,**config):
    sid=store.resource_put("proxy_subscription","s",config)
    with store.tx() as (s,_):
        for identity in nodes:
            node=s.get(Resource,identity);d=json.loads(node.data);d["subscription_ids"]=[sid];node.data=json.dumps(d)
    return sid


class Checker:
    enabled=True
    def __init__(self,store):self.store,self.calls=store,[]
    async def check(self,identity,platform):
        self.calls.append((identity,platform))
        receipt(self.store,identity,platform)
        return {"code":"target_http_ok"}


def test_auto_discovery_needs_explicit_subscription_opt_in_and_targets(env):
    store,nodes,now=env
    checker=Checker(store);scheduler=ProxyTargetScheduler(store,checker)
    sid=attach_subscription(store,nodes,target_platforms=["Loomy"])
    assert scheduler.discover(force=True)==0
    with store.tx() as (s,_):
        row=s.get(Resource,sid);row.data=json.dumps({"auto_target_check":True})
    assert scheduler.discover(force=True)==0
    with store.tx() as (s,_):
        row=s.get(Resource,sid);row.data=json.dumps({"auto_target_check":True,"target_platforms":["Loomy"]})
    assert scheduler.discover(force=True)==1 and scheduler.discover(force=True)==0
    assert asyncio.run(scheduler.tick())==2
    assert asyncio.run(scheduler.tick())==2
    assert len(checker.calls)==4 and {p for _,p in checker.calls}=={"Loomy"}
    assert scheduler.discover(force=True)==0
    data=store.resources(BATCH_KIND)[0]["data"]
    assert data["cursor"]==4 and data["status"]=="completed"


def test_batches_resume_by_cursor_and_global_lease_prevents_two_workers(env):
    store,nodes,now=env
    checker=Checker(store);one=ProxyTargetScheduler(store,checker);two=ProxyTargetScheduler(store,checker)
    one.queue(nodes,["Loomy"])
    claim=one._claim()
    assert claim and two._claim() is None
    now[0]+=121
    recovered=two._claim()
    assert recovered["id"]==claim["id"] and recovered["cursor"]==0
    assert one._finish_chunk(claim,[{"code":"late"}]*2) is False
    assert two._finish_chunk(recovered,[{"code":"target_http_ok"}]*2)
    one.queue([nodes[0]],["WPS 灵犀"])
    # Other batch may be fairly picked first; original cursor stays persisted.
    asyncio.run(two.tick());asyncio.run(two.tick())
    data=next(r["data"] for r in store.resources(BATCH_KIND) if r["id"]==claim["id"])
    assert data["cursor"]==4 and data["status"]=="completed"
    assert "late" not in data["counts"]


def test_changed_queued_node_skipped_and_subscription_disable_cancels(env):
    store,nodes,now=env
    checker=Checker(store);scheduler=ProxyTargetScheduler(store,checker)
    scheduler.queue([nodes[0]],["Loomy"])
    with store.tx() as (s,_):s.get(Resource,nodes[0]).version+=1
    assert asyncio.run(scheduler.tick())==1 and checker.calls==[]
    assert store.resources(BATCH_KIND)[0]["data"]["counts"]=={"proxy_changed":1}
    sid=attach_subscription(store,nodes,auto_target_check=True,target_platforms=["Loomy"])
    assert scheduler.discover(force=True)==1
    with store.tx() as (s,_):
        row=s.get(Resource,sid);row.data=json.dumps({"auto_target_check":False,"target_platforms":["Loomy"]})
    assert asyncio.run(scheduler.tick())==0 and checker.calls==[]
    assert any(r["data"]["status"]=="cancelled" for r in store.resources(BATCH_KIND))


def test_rejected_target_does_not_get_eligible_and_future_timestamp_is_rejected(env):
    store,nodes,now=env
    aid=store.create_account("Loomy","a","a");store.bind_proxy(aid,nodes[0])
    receipt(store,nodes[0],eligible=False,code="target_forbidden")
    with pytest.raises(DomainError):store.account_proxy(aid)
    with store.tx() as (s,_):
        for row in s.query(Resource).filter(Resource.kind==KIND):row.data=json.dumps(json.loads(row.data)|{"eligible":True,"checked_at":now[0]+1})
    with pytest.raises(DomainError):store.account_proxy(aid)


def test_routes_queue_only_and_redact_execution_details(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.proxy_target_scheduler import register_routes
    store,nodes,now=env
    checker=Checker(store);app=FastAPI();register_routes(app,store,checker)
    with TestClient(app) as client:
        response=client.post("/api/proxy-target-checks",json={"proxy_ids":nodes[:2],"platforms":["Loomy"]})
        assert response.status_code==202 and response.json()["total"]==2
        assert checker.calls==[]
        batches=client.get("/api/proxy-target-batches").json()["batches"]
        assert len(batches)==1 and "items" not in batches[0]["data"]
        assert "lease_token" not in batches[0]["data"] and "owner" not in batches[0]["data"]
        assert client.get("/api/proxy-target-checks?platform=Loomy").json()["checks"]==[]


def test_discovery_throttle_is_persistent_across_scheduler_restart(env):
    store,nodes,now=env
    scheduler=ProxyTargetScheduler(store,Checker(store))
    assert scheduler.discover()==0
    attach_subscription(store,nodes,auto_target_check=True,target_platforms=["Loomy"])
    restarted=ProxyTargetScheduler(store,Checker(store))
    assert restarted.discover()==0
    now[0]+=60
    assert restarted.discover()==1


def test_shared_nodes_are_queued_once_and_unseen_nodes_precede_old_failures(env):
    store,nodes,now=env
    first=attach_subscription(store,nodes,auto_target_check=True,target_platforms=["Loomy"])
    second=store.resource_put("proxy_subscription","second",{"auto_target_check":True,"target_platforms":["Loomy"]})
    with store.tx() as (s,_):
        for identity in nodes:
            node=s.get(Resource,identity);node.data=json.dumps(json.loads(node.data)|{"subscription_ids":[first,second]})
    receipt(store,nodes[0],eligible=False,checked_at=now[0]-1800)
    scheduler=ProxyTargetScheduler(store,Checker(store))
    assert scheduler.discover()==1
    batches=store.resources(BATCH_KIND)
    assert len(batches)==1 and batches[0]["data"]["total"]==4
    items=batches[0]["data"]["items"]
    assert items[-1]["proxy_id"]==nodes[0]
    assert len({(i["proxy_id"],i["platform"]) for i in items})==4

