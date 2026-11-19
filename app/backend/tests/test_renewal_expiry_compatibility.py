"""Expiry shapes from 3067 maintenance_policy and existing 3065 credentials."""
import base64
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Account, Job
from app.operations import DEFAULT_SETTINGS, Supervisor, credential_expiry
from app.store import Store
from app.native_lifecycle import record_native_login


def jwt(exp):
    claims=base64.urlsafe_b64encode(json.dumps({"exp":exp}).encode()).decode().rstrip("=")
    return "synthetic."+claims+".synthetic"


@pytest.mark.parametrize("credential,expected",[
    ({"expires_at":1794910000},1794910000),
    ({"expiresAt":"1794910000000"},1794910000),
    ({"tokenExpiresAt":1794910000000},1794910000),
    ({"auth":{"expiresAt":1794910000}},1794910000),
    ({"expires_at":"2026-11-17T12:00:00+08:00"},1794888000),
    ({"expires_at":"2026-11-17T04:00:00"},1794888000),
    ({"access_token":jwt(1794910000)},1794910000),
    ({"accessToken":"Bearer "+jwt(1794910000)},1794910000),
    ({"auth":{"token":jwt(1794910000)}},1794910000),
    ({"expires_at":"broken","expiresAt":1794910000},1794910000),
    ({"expires_at":float("nan"),"tokenExpiresAt":1794910000},1794910000),
    ({"expires_at":True,"expiresAt":1794910000},1794910000),
    ({"expires_at":float("inf"),"access_token":"invalid"},None),
    ({"expires_at":0,"access_token":"not-a-jwt","auth":"bad"},None),
    ({"expires_at":-1},None),
])
def test_expiry_shapes_are_normalized_without_network(credential,expected):
    assert credential_expiry(credential)==expected


def test_unverified_and_future_credentials_not_renewed_and_due_work_deduplicated(tmp_path):
    now=1794900000.
    store=Store(f"sqlite:///{tmp_path/'expiry.db'}",Fernet.generate_key(),clock=lambda:now)
    store.real_capabilities["小浣熊"]={"renew","balance"}
    try:
        ids={}
        for label,credential in {
            "alias":{"access_token":"test-alias-token","expiresAt":(now+60)*1000},
            "jwt":{"access_token":jwt(now+60)},
            "future":{"access_token":"test-future-token","tokenExpiresAt":now+3600},
            "unverified":{"access_token":"test-imported-token","expiresAt":now+60},
        }.items():
            aid=store.create_account("小浣熊",label,label)
            store.put_credential(aid,json.dumps(credential),0)
            with store.tx() as (s,_):
                account=s.get(Account,aid)
                account.credential_status="valid"
                if label!='unverified':
                    assert record_native_login(store,s,account,'official',credential)['state']=='independent'
            ids[label]=aid
        supervisor=Supervisor(store,SimpleNamespace(capacity=64),SimpleNamespace(),tmp_path)
        settings={**DEFAULT_SETTINGS,"auto_renew":True}
        supervisor.schedule(settings)
        supervisor.schedule(settings)
        with store.read() as (s,_):
            jobs=list(s.scalars(select(Job).where(Job.kind=="renew")))
            assert len(jobs)==2 and {job.account_id for job in jobs}=={ids["alias"],ids["jwt"]}
    finally:
        store.engine.dispose()
