import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import Resource
from app.proxy_pool import RouteInput,save_route
from app.registration import RegistrationService
from app.registration_preflight import PreflightInput,mount_registration_preflight,registration_preflight
from app.sms_channels import ChannelInput
from app.store import Store


@pytest.fixture
def env(tmp_path):
    now=[1794900000.]
    store=Store(f"sqlite:///{tmp_path/'preflight.db'}",Fernet.generate_key(),clock=lambda:now[0])
    def network(**kwargs):
        raise AssertionError("preflight must not contact an upstream")
    service=RegistrationService(store,SimpleNamespace(enabled=True,transport_factory=network))
    yield store,service,now
    store.engine.dispose()


def checks(result):
    return {c["code"]:c for c in result["checks"]}


def test_complete_config_is_not_registration_success_and_never_writes(env):
    store,service,_=env
    channel=service.sms.configure(ChannelInput(name="synthetic",provider="eomsg",base_url="https://sms.example.test",token="secret-marker",keyword="synthetic"))
    proxy=store.resource_put("proxy","synthetic",{"status":"untested"},secret="http://private-user:private-password@proxy.example.test:8080")
    store.service_heartbeat("tasks",{"browser_executor":{"status":"ok","sessions":0,"max_sessions":8,"browser_connected":False}})
    before=store.revision()
    result=registration_preflight(service,PreflightInput(platform="WPS 灵犀",mode="browser",sms_channel_id=channel,proxy_id=proxy))
    assert result["configuration_ok"] and not result["registration_verified"] and result["read_only"]
    assert all(checks(result)[name]["status"]=="pass" for name in ("registration_mode","sms_channel","proxy_configuration","browser_executor"))
    assert store.revision()==before and store.resources("registration")==[] and store.resources("sms_lease")==[]
    public=json.dumps(result,ensure_ascii=False)
    assert all(word not in public for word in ("secret-marker","private-user","private-password","proxy.example.test"))


@pytest.mark.parametrize("change",["disabled","missing_project","unreadable"])
def test_channel_preflight_reports_actual_local_configuration(env,change):
    store,service,_=env
    channel=service.sms.configure(ChannelInput(name="synthetic",provider="haozhuma",base_url="https://sms.example.test",token="secret-marker",project="project"))
    with store.tx() as (s,_):
        row=s.get(Resource,channel)
        if change=="disabled":
            row.data=json.dumps({"enabled":False})
        elif change=="unreadable":
            row.secret="broken"
        else:
            secret=json.loads(store.cipher.decrypt(row.secret.encode()))
            secret["project"]=""
            row.secret=store.cipher.encrypt(json.dumps(secret).encode()).decode()
    result=registration_preflight(service,PreflightInput(platform="Loomy",mode="register",sms_channel_id=channel))
    assert checks(result)["sms_channel"]["status"]=="fail" and not result["configuration_ok"]


def test_direct_is_visible_and_missing_channel_is_not_a_new_global_block(env):
    _,service,_=env
    result=registration_preflight(service,PreflightInput(platform="Loomy",mode="sms"))
    assert checks(result)["proxy_configuration"]["status"]=="warning"
    assert checks(result)["sms_channel"]["status"]=="warning"
    assert result["configuration_ok"] and not result["registration_verified"]


def test_preflight_respects_account_binding_over_explicit_proxy(env):
    store,service,_=env
    aid=store.create_account("Loomy","synthetic","synthetic")
    bad=store.resource_put("proxy","bound",{"status":"untested"},secret="http://synthetic:8080")
    good=store.resource_put("proxy","explicit",{"status":"untested"},secret="http://synthetic-good:8080")
    store.bind_proxy(aid,bad)
    with store.tx() as (s,_):
        s.get(Resource,bad).data=json.dumps({"status":"disabled"})
    result=registration_preflight(service,PreflightInput(platform="Loomy",mode="sms",account_id=aid,proxy_id=good))
    assert checks(result)["proxy_configuration"]["status"]=="fail"


def test_stale_browser_heartbeat_does_not_claim_online(env):
    store,service,now=env
    store.service_heartbeat("tasks",{"browser_executor":{"status":"ok"}})
    now[0]+=46
    result=registration_preflight(service,PreflightInput(platform="WPS 灵犀",mode="browser"))
    assert checks(result)["browser_executor"]["status"]=="unverified"


def test_browser_capacity_is_a_waiting_hint_and_raw_error_is_private(env):
    store,service,_=env
    store.service_heartbeat("tasks",{"browser_executor":{"status":"ok","sessions":8,"max_sessions":8}})
    request=PreflightInput(platform="WPS 灵犀",mode="browser")
    assert checks(registration_preflight(service,request))["browser_executor"]["status"]=="warning"
    store.service_heartbeat("tasks",{"browser_executor":{"status":"ok","last_error":"private-error-marker"}})
    assert "private-error-marker" not in json.dumps(registration_preflight(service,request))


def test_endpoint_rejects_unsupported_mode_and_never_echoes_inputs(env):
    _,service,_=env
    app=FastAPI()
    mount_registration_preflight(app,service)
    with TestClient(app) as client:
        response=client.post("/api/registration/preflight",json={"platform":"secret-as-platform","mode":"unsupported"})
        assert response.status_code==200 and not response.json()["configuration_ok"]
        assert "secret-as-platform" not in response.text
