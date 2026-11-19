import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from conftest import seed_sandbox
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Account, Job, Resource, RequestLog
from app.operations import DEFAULT_SETTINGS, SettingsInput, Supervisor, analyze, settings, write_settings
from app.store import Store, DomainError
from app.subscriptions import parse_subscription, save_nodes


@pytest.fixture
def store(tmp_path):
    now=[1794801600.]
    s=Store(f"sqlite:///{tmp_path/'db'}",Fernet.generate_key(),clock=lambda:now[0])
    s.now=now
    yield s
    s.engine.dispose()


def test_settings_cas_and_no_business_limit_mutation(store):
    seed_sandbox(store)
    before=store.snapshot()["accounts"]
    body=SettingsInput(expected_version=0,**{**DEFAULT_SETTINGS,"maintenance_capacity":128})
    write_settings(store,body)
    assert settings(store)["values"]["maintenance_capacity"]==128
    with pytest.raises(DomainError) as error:
        write_settings(store,body)
    assert error.value.code=="resource_version_conflict"
    assert [a["concurrency"] for a in before]==[a["concurrency"] for a in store.snapshot()["accounts"]]


def test_checkin_followup_precedes_remaining_batch(store):
    seed_sandbox(store)
    store.schedule_today()
    j=store.claim("a")
    store.finish(j["id"],j["owner"],j["attempt"],True)
    next_job=store.claim("b")
    assert next_job["kind"]=="balance" and next_job["account_id"]==j["account_id"]


def test_scheduler_idempotency_and_no_balance_before_checkin(store,tmp_path):
    seed_sandbox(store)
    supervisor=Supervisor(store,SimpleNamespace(tasks=[],capacity=64),SimpleNamespace(),tmp_path)
    config={**DEFAULT_SETTINGS,"daily_checkin":True,"refresh_balances":True}
    supervisor.schedule(config)
    supervisor.schedule(config)
    jobs=store.snapshot()["jobs"]
    assert len(jobs)==21 and all(j["kind"]=="checkin" for j in jobs)
    sample=supervisor.sample()
    assert sample["process_memory_bytes"]>0 and sample["disk_total_bytes"]>sample["disk_free_bytes"]


def test_full_window_includes_records_outside_last_200(store):
    seed_sandbox(store)
    aid=store.snapshot()["accounts"][0]["id"]
    now=store.clock()
    with store.tx() as (s,_):
        for i in range(250):
            s.add(RequestLog(id=str(i),platform="Loomy",model="m",account_id=aid,started_at=now-500+i,ended_at=now-400+i,status="failed" if i==0 else "succeeded",error_code="network" if i==0 else None,first_content_at=now-495+i,input_tokens=100,cached_tokens=50))
        s.add(RequestLog(id="old-start",platform="Loomy",model="m",account_id=aid,started_at=now-5000,ended_at=now-30,status="failed",error_code="network"))
        s.add(RequestLog(id="inflight",platform="Loomy",model="m",account_id=aid,started_at=now-20,status="inflight"))
    start=analyze(store,now-3600,now,"start")["groups"][0]
    end=analyze(store,now-3600,now,"end")["groups"][0]
    assert start["total"]==251 and start["statuses"]["inflight"]==1 and start["errors"]["network"]==1
    assert end["total"]==251 and end["errors"]["network"]==2 and "inflight" not in end["statuses"]
    assert start["cache"]["ratio"]==.5 and start["duration"]["p95"]==100
    assert len(store.snapshot()["requests"])==200


@pytest.mark.parametrize("body",[
    "http://user:synthetic@localhost:8888#node\nsocks5://localhost:1080#socks",
    base64.b64encode(b"http://localhost:8888#node\nsocks5://localhost:1080#socks").decode(),
    'proxies:\n  - {name: node, type: http, server: localhost, port: 8888}\n  - {name: socks, type: socks5, server: localhost, port: 1080}',
    '{"outbounds":[{"tag":"node","type":"http","server":"localhost","server_port":8888},{"tag":"socks","type":"socks","server":"localhost","server_port":1080}]}'
])
def test_subscription_formats(body):
    nodes,rejected=parse_subscription(body)
    assert len(nodes)==2 and not rejected
    assert nodes[0]["name"]=="node"


def test_subscription_reconciliation_keeps_binding_and_encrypts(store):
    subscription=store.resource_put("proxy_subscription","local",{})
    nodes,rejected=parse_subscription("http://user:synthetic-password@localhost:8888#node")
    save_nodes(store,subscription,nodes,rejected)
    node=store.resources("proxy")[0]
    aid=store.create_account("Loomy","id","label")
    store.bind_proxy(aid,node["id"])
    assert "synthetic-password" not in json.dumps(store.resources("proxy"))
    nodes[0]["url"]="http://user:rotated-password@localhost:8888"
    save_nodes(store,subscription,nodes,[])
    assert store.resources("proxy")[0]["id"]==node["id"]
    with pytest.raises(DomainError) as unverified:
        store.account_proxy(aid)
    assert unverified.value.code == "proxy_target_unverified"
    refreshed = store.resources("proxy")[0]
    store.resource_put("proxy_target_check", f"{node['id']}:Loomy",
                       {"eligible":True,"status":"completed","node_version":refreshed['version'],
                        "platform":"Loomy","checked_at":store.clock()})
    assert "rotated-password" in store.account_proxy(aid)
    save_nodes(store,subscription,[],[])
    with pytest.raises(DomainError) as error:
        store.account_proxy(aid)
    assert error.value.code=="proxy_retired"


def test_unknown_proxy_protocol_is_explicit_and_yaml_alias_rejected():
    nodes,rejected=parse_subscription("unknown+test://synthetic@example.com:443#node")
    assert not nodes and rejected==[{"index":0,"reason":"proxy_protocol_unsupported"}]
    with pytest.raises(DomainError):
        parse_subscription("proxies: &a [*a]")
