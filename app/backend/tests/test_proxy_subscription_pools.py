import json

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.adapters import Registry
from app.models import Resource
from app.proxy_pool import PoolInput,RouteInput,_pool_nodes,resolve_explicit_proxy,save_pool,save_route
from app.proxy_target_health import KIND
from app.registration import RegistrationService
from app.store import DomainError,Store


@pytest.fixture
def env(tmp_path):
    now=[1800000000.]
    store=Store(f"sqlite:///{tmp_path/'dynamic.db'}",Fernet.generate_key(),clock=lambda:now[0])
    sources=[store.resource_put("proxy_subscription",f"source-{i}",{}) for i in range(2)]
    aid=store.create_account("Loomy","a","a")
    yield store,sources,aid,now
    store.engine.dispose()


def node(store,label,sources,**extra):
    return store.resource_put("proxy",label,{"status":"untested","subscription_ids":sources}|extra,secret=f"http://{label}:8080")


def passed(store,identity,platform="Loomy"):
    with store.read() as (s,_):version=s.get(Resource,identity).version
    return store.resource_put(KIND,f"{identity}:{platform}",{"proxy_id":identity,"platform":platform,"status":"completed","eligible":True,"node_version":version,"checked_at":store.clock()})


def test_new_subscription_nodes_join_existing_pool_after_target_check(env):
    store,sources,aid,now=env
    pool=save_pool(store,PoolInput(name="dynamic",subscription_ids=[sources[0]]))["id"]
    assert not store.resources("proxy_route") # Saving never chooses a default route.
    save_route(store,"Loomy",RouteInput(mode="pool",pool_id=pool))
    with pytest.raises(DomainError):store.account_proxy(aid)
    identity=node(store,"new-node",[sources[0]])
    with pytest.raises(DomainError):store.account_proxy(aid)
    passed(store,identity)
    assert store.account_proxy(aid)=="http://new-node:8080"
    original=next(p for p in store.resources("proxy_pool") if p["id"]==pool)
    assert original["data"]["proxy_ids"]==[]
    now[0]+=86401
    with pytest.raises(DomainError):store.account_proxy(aid)


def test_removed_source_membership_and_retired_nodes_leave_pool(env):
    store,sources,aid,now=env
    identity=node(store,"n",[sources[0]]);passed(store,identity)
    pool=save_pool(store,PoolInput(name="p",subscription_ids=[sources[0]]))["id"]
    save_route(store,"Loomy",RouteInput(mode="pool",pool_id=pool))
    assert store.account_proxy(aid)=="http://n:8080"
    with store.tx() as (s,_):
        row=s.get(Resource,identity);row.data=json.dumps(json.loads(row.data)|{"subscription_ids":[sources[1]]})
    with pytest.raises(DomainError):store.account_proxy(aid)
    with store.tx() as (s,_):
        row=s.get(Resource,identity);row.data=json.dumps(json.loads(row.data)|{"subscription_ids":[sources[0]],"status":"retired"})
    with pytest.raises(DomainError):store.account_proxy(aid)


def test_shared_sources_and_explicit_members_deduplicate_and_static_pools_still_work(env):
    store,sources,aid,now=env
    shared=node(store,"shared",sources);passed(store,shared)
    manual=node(store,"manual",[])
    pool=save_pool(store,PoolInput(name="mixed",proxy_ids=[shared,manual],subscription_ids=sources))["id"]
    with store.read() as (s,_):
        assert {r.id for r in _pool_nodes(s,s.get(Resource,pool))}=={shared,manual}
    with store.tx() as (s,_):
        row=s.get(Resource,shared);row.data=json.dumps(json.loads(row.data)|{"subscription_ids":[sources[1]]})
    with store.read() as (s,_):assert len(_pool_nodes(s,s.get(Resource,pool)))==2
    static=save_pool(store,PoolInput(name="static",proxy_ids=[manual]))["id"]
    save_route(store,"Loomy",RouteInput(mode="pool",pool_id=static))
    assert store.account_proxy(aid)=="http://manual:8080"


def test_dynamic_pool_shared_by_maintenance_registration_and_forwarding(env):
    store,sources,aid,now=env
    identity=node(store,"n",[sources[0]],target_check_required=True);passed(store,identity)
    pool=save_pool(store,PoolInput(name="p",subscription_ids=[sources[0]]))["id"]
    route={"mode":"pool","pool_id":pool}
    save_route(store,"Loomy",RouteInput(**route))
    assert resolve_explicit_proxy(store,aid,route,affinity_scope="maintenance:balance")=="http://n:8080"
    service=RegistrationService(store,Registry(enabled=False))
    sid=service.create("Loomy","r","sms",{"phone":"13800000000"})
    data,material=service.details(sid)
    assert service.proxy(data,material)==store.account_proxy(aid)=="http://n:8080"


def test_missing_or_disabled_subscription_does_not_supply_nodes(env):
    store,sources,aid,now=env
    with pytest.raises(ValidationError):PoolInput(name="empty")
    with pytest.raises(DomainError):save_pool(store,PoolInput(name="bad",subscription_ids=["missing"]))
    identity=node(store,"n",[sources[0]]);passed(store,identity)
    pool=save_pool(store,PoolInput(name="p",subscription_ids=[sources[0]]))["id"]
    save_route(store,"Loomy",RouteInput(mode="pool",pool_id=pool))
    with store.tx() as (s,_):s.get(Resource,sources[0]).data=json.dumps({"enabled":False})
    with pytest.raises(DomainError):store.account_proxy(aid)
