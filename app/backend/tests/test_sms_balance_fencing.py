"""Wallet results must belong to the exact current channel token ordering."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.sms_channels import SmsChannels, ChannelInput, ChannelUpdate
from app.store import Store, DomainError


@pytest.fixture
def env(tmp_path):
    now=[1794801600.0]
    store=Store(f"sqlite:///{tmp_path / 'wallet.db'}",Fernet.generate_key(),clock=lambda:now[0])
    sms=SmsChannels(store,SimpleNamespace(enabled=True))
    body=dict(name="wallet fixture",provider="eomsg",base_url="https://sms.example.test/api",tokens=["token-a","token-b"],keyword="fixture")
    cid=sms.configure(ChannelInput(**body))
    yield store,sms,cid,body,now
    store.engine.dispose()


def test_changed_tokens_never_inherit_previous_token_balances(env):
    store,sms,cid,body,now=env
    async def wallet(channel,*args,**kwargs):
        return "10" if channel["token"]=="token-a" else "0"
    sms.call=wallet
    asyncio.run(sms.balances(cid))
    assert sms.token_balances(cid,sms.read(cid))=={"token-a":10,"token-b":0}
    sms.configure(ChannelUpdate(**{**body,"tokens":["token-b","token-a"]},expected_version=1),cid)
    assert sms.token_balances(cid,sms.read(cid))=={}
    listed=sms.listing()["balances"][0]["data"]
    assert listed["status"]=="stale" and listed["balance"] is None
    assert listed["tokens"]==[] and listed["verified_tokens"]==0
    assert "token-a" not in json.dumps(sms.listing())


def test_edit_during_query_cannot_commit_old_wallet(env):
    store,sms,cid,body,now=env
    calls=[]
    async def wallet(channel,*args,**kwargs):
        if not calls:
            sms.configure(ChannelUpdate(**{**body,"tokens":["token-new"]},expected_version=1),cid)
        calls.append(channel["token"])
        return "12"
    sms.call=wallet
    with pytest.raises(DomainError,match="resource_version_conflict"):
        asyncio.run(sms.balances(cid))
    assert store.resources("sms_balance")==[]


def test_older_slow_query_cannot_replace_newer_wallet(env):
    store,sms,cid,body,now=env
    async def run():
        old_started=asyncio.Event()
        release_old=asyncio.Event()
        async def wallet(channel,*args,**kwargs):
            if asyncio.current_task().get_name()=="older":
                old_started.set()
                await release_old.wait()
                return "100"
            return "7"
        sms.call=wallet
        older=asyncio.create_task(sms.balances(cid),name="older")
        await old_started.wait()
        now[0]+=1
        newer=await sms.balances(cid)
        release_old.set()
        late=await older
        assert late==newer
    asyncio.run(run())
    assert len(store.resources("sms_balance"))==1
    assert store.resources("sms_balance")[0]["data"]["balance"]==14
