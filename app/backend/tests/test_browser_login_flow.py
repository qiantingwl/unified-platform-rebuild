"""Ordinary browser form automation: no challenge solving and no replayed SMS."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from app.browser_login import BrowserHub, ExecutorClient
from app.models import Resource
from app.registration import RegistrationService
from app.sms_channels import ChannelInput
from app.store import Store, uid
from browser_executor.main import FlowInput, advance_login
from test_browser_executor import FakeExecutor


class Element:
    def __init__(self):self.clicks=0;self.value=None
    async def is_visible(self):return True
    async def is_enabled(self):return True
    async def fill(self,value):self.value=value
    async def click(self,**kwargs):self.clicks+=1
    async def is_checked(self):return True


class Locator:
    def __init__(self,items):self.items=items
    async def count(self):return len(self.items)
    def nth(self,i):return self.items[i]
    def filter(self,**kwargs):return self


class Page:
    url="https://xiaohuanxiong.com/authorize"
    def __init__(self,captcha=False):
        self.frames=[self];self.captcha=captcha
        self.phone,self.code,self.send,self.login=Element(),Element(),Element(),Element()
    def locator(self,selector):
        if "geetest" in selector:return Locator([Element()] if self.captcha else [])
        if "login-identifier" in selector:return Locator([self.phone])
        if "login-code" in selector:return Locator([self.code])
        return Locator([])
    def get_by_text(self,*args,**kwargs):return Locator([])
    def get_by_role(self,role,name=None,**kwargs):
        if role=="checkbox":return Locator([])
        if hasattr(name,"match"):
            if name.match("发送验证码"):return Locator([self.send])
            if name.match("登录"):return Locator([self.login])
        return Locator([])


@pytest.mark.parametrize("received,expected", [("123456","reserved"),("654321","code_received")])
def test_browser_rejection_only_invalidates_the_submitted_code(tmp_path, received, expected):
    async def run():
        store=Store(f"sqlite:///{tmp_path/'db'}",Fernet.generate_key())
        service=RegistrationService(store,SimpleNamespace(enabled=True))
        sid=uid()
        with store.tx() as (s,_):
            s.add(Resource(id=sid,kind="registration",name="test",data=json.dumps({"platform":"WPS 灵犀","mode":"browser","status":"waiting_authorization","account_id":None}),secret=store.cipher.encrypt(b'{}').decode(),updated_at=store.clock()))
        cid=service.sms.configure(ChannelInput(name="test",provider="eomsg",base_url="https://sms.example.test/api",token="synthetic",keyword="test"))
        lease=service.sms.existing_number(cid,sid,"13800000001")
        service.sms.update(lease,{"status":"code_received"},{"code":received})
        class Executor:
            async def call(self,method,path,body=None):
                return {"stage":"verification_code_incorrect","action":None}
        hub=BrowserHub(store,service.registry,service,Executor())
        hub.persist(sid,{"sms_lease_id":lease,"browser_code_fingerprint":hashlib.sha256(b"123456").hexdigest()})
        await hub.auto_step(sid,*service.details(sid))
        data,private=service.sms.lease(lease)
        assert data["status"]==expected
        assert private.get("rejected_code")==("123456" if received=="123456" else None)
        assert not service.details(sid)[1].get("browser_code_fingerprint")
        store.engine.dispose()
    asyncio.run(run())


def test_semantic_send_and_submit_once_per_operation():
    async def run():
        page=Page();item=SimpleNamespace(current=lambda:page)
        observe=await advance_login(item,FlowInput(platform="小浣熊"))
        assert observe["action"]=="send_sms"
        request=FlowInput(platform="小浣熊",action="send_sms",phone="+8613800000001",operation="send-1")
        await advance_login(item,request);await advance_login(item,request)
        assert page.phone.value=="13800000001" and page.send.clicks==1
        observe=await advance_login(item,FlowInput(platform="小浣熊",sms_attempted=True))
        assert observe["action"]=="submit_sms"
        request=FlowInput(platform="小浣熊",action="submit_sms",code="123456",operation="verify-1")
        await advance_login(item,request);await advance_login(item,request)
        assert page.code.value=="123456" and page.login.clicks==1
    asyncio.run(run())


def test_captcha_waits_without_any_click_or_sms():
    async def run():
        page=Page(captcha=True);item=SimpleNamespace(current=lambda:page)
        result=await advance_login(item,FlowInput(platform="小浣熊",action="send_sms",phone="13800000001",operation="s"))
        assert result=={"stage":"waiting_captcha","action":None}
        assert page.send.clicks==page.login.clicks==0
    asyncio.run(run())


def test_challenge_appearing_between_observe_and_send_does_not_poison_intent(tmp_path):
    async def run():
        store=Store(f"sqlite:///{tmp_path/'db'}",Fernet.generate_key())
        service=RegistrationService(store,SimpleNamespace(enabled=True))
        sid=uid()
        material={"auto_run":True,"phone":"13800000001","sms_lease_id":"synthetic"}
        with store.tx() as (s,_):
            s.add(Resource(id=sid,kind="registration",name="test",data=json.dumps({"platform":"WPS 灵犀","mode":"browser","status":"waiting_authorization","account_id":None}),secret=store.cipher.encrypt(json.dumps(material).encode()).decode(),updated_at=store.clock()))
        page=Page();page.url="https://account.wps.cn/"
        item=SimpleNamespace(current=lambda:page)
        class Executor:
            raced=False
            async def call(self,method,path,body=None):
                if path.endswith('/flow'):
                    if body.get('action')=='send_sms' and not self.raced:
                        self.raced=True
                        page.captcha=True
                    return await advance_login(item,FlowInput(**body))
                return {}
        hub=BrowserHub(store,service.registry,service,Executor())
        await hub.auto_step(sid,*service.details(sid))
        assert page.send.clicks==0
        assert 'browser_sms_intent' not in service.details(sid)[1]
        assert service.details(sid)[0]['browser_stage']=='waiting_captcha'
        # Simulate normal completion of official verification, not a solver.
        page.captcha=False
        await hub.auto_step(sid,*service.details(sid))
        assert page.send.clicks==1
        assert service.details(sid)[1]['browser_sms_intent']
        await hub.auto_step(sid,*service.details(sid))
        assert page.send.clicks==1
        store.engine.dispose()
    asyncio.run(run())


def test_untrusted_page_never_gets_phone_or_code():
    async def run():
        page=Page();page.url="https://untrusted.example/login"
        item=SimpleNamespace(current=lambda:page)
        result=await advance_login(item,FlowInput(platform="小浣熊",action="send_sms",phone="13800000001",operation="s"))
        assert result["stage"]=="waiting_official_page" and page.phone.value is None
    asyncio.run(run())


def test_wps_phone_entry_uses_agreement_not_keep_online_then_exact_form():
    class Agreement(Element):
        checked=False
        async def is_checked(self):return self.checked
        async def check(self):self.checked=True
    class WpsPage(Page):
        url='https://account.wps.cn/'
        def __init__(self):
            super().__init__()
            self.opened=False;self.agreement=Agreement();self.keep_online=Agreement()
            self.entry=Element()
            async def click(**kwargs):
                assert self.agreement.checked
                self.opened=True;self.entry.clicks+=1
            self.entry.click=click
        def locator(self,selector):
            if selector=='span.f_icon_txt':return Locator([] if self.opened else [self.entry])
            if selector=='#loginProtocal':return Locator([] if self.opened else [self.agreement])
            if self.opened and selector=='input#phone':return Locator([self.phone])
            if self.opened and selector=='input#code':return Locator([self.code])
            return Locator([])
        def get_by_role(self,role,name=None,**kwargs):
            if role=='checkbox':return Locator([self.keep_online])
            if self.opened and role=='link' and name=='立即登录/注册':return Locator([self.login])
            return Locator([])
        def get_by_text(self,name,**kwargs):
            return Locator([self.send]) if self.opened and hasattr(name,'match') and name.match('发送验证码') else Locator([])
    async def run():
        page=WpsPage();item=SimpleNamespace(current=lambda:page)
        assert (await advance_login(item,FlowInput(platform='WPS 灵犀')))['action']=='enter_login'
        await advance_login(item,FlowInput(platform='WPS 灵犀',action='enter_login',operation='entry'))
        assert page.agreement.checked and not page.keep_online.checked and page.send.clicks==0
        assert (await advance_login(item,FlowInput(platform='WPS 灵犀')))['action']=='send_sms'
        await advance_login(item,FlowInput(platform='WPS 灵犀',action='send_sms',operation='send',phone='13800000001'))
        assert page.phone.value=='13800000001' and page.send.clicks==1
        await advance_login(item,FlowInput(platform='WPS 灵犀',action='submit_sms',operation='submit',code='123456'))
        assert page.code.value=='123456' and page.login.clicks==1
    asyncio.run(run())


def test_codearts_registration_requires_evidence_and_respects_official_risk():
    class Huawei(Page):
        url="https://reg.huaweicloud.com/registerui/cn/register.html"
        marker=""
        def get_by_text(self,name,**kwargs):
            return Locator([Element()] if hasattr(name,"search") and name.search(self.marker) else [])
        def locator(self,selector):
            if selector=='#phone-register-submit':return Locator([self.login])
            return super().locator(selector)
    async def run():
        page=Huawei();item=SimpleNamespace(current=lambda:page)
        flags={"platform":"CodeArts","registration_pending":True}
        assert (await advance_login(item,FlowInput(**flags)))["action"]=="prepare_phone"
        await advance_login(item,FlowInput(**flags,action="prepare_phone",phone="13800000001",operation="prepare"))
        assert page.phone.value=="13800000001" and page.send.clicks==0
        assert (await advance_login(item,FlowInput(**flags,phone_prepared=True)))["action"]=="send_sms"
        page.marker="当前环境存在风险"
        result=await advance_login(item,FlowInput(**flags,phone_prepared=True,action="send_sms",phone="13800000001",operation="send"))
        assert result["stage"]=="waiting_official_review" and page.send.clicks==0
        page.marker="注册成功"
        result=await advance_login(item,FlowInput(**flags,sms_attempted=True,code_attempted=True))
        assert result["stage"]=="registration_confirmed"
    asyncio.run(run())


def test_codearts_start_and_confirmation_only_open_oauth_not_account(tmp_path):
    from app.codearts_login import CodeArtsLogin
    from app.login_connectors import SUPPORTED
    async def run():
        started=await CodeArtsLogin(None).start({},"register")
        assert "register" in SUPPORTED["CodeArts"]
        assert started["authorization_url"]=="https://reg.huaweicloud.com/registerui/cn/register.html"
        assert started["codearts_oauth_url"].startswith("https://codearts.huaweicloud.com/portal/authorize?")
        assert started["codearts_registration_pending"] is True and "credential" not in started
        store=Store(f"sqlite:///{tmp_path/'db'}",Fernet.generate_key())
        registry=SimpleNamespace(enabled=True);service=RegistrationService(store,registry)
        sid=uid()
        material={**started,"phone":"13800000001","sms_lease_id":"unused","auto_run":True}
        with store.tx() as (s,_):
            s.add(Resource(id=sid,kind="registration",name="test",data=json.dumps({"platform":"CodeArts","mode":"register","status":"waiting_authorization","account_id":None}),secret=store.cipher.encrypt(json.dumps(material).encode()).decode(),updated_at=store.clock()))
        calls=[]
        class Executor:
            async def call(self,method,path,body=None):
                calls.append((path,body))
                if path.endswith("/flow"):return {"stage":"registration_confirmed"}
                return {}
        hub=BrowserHub(store,registry,service,Executor())
        data,saved=service.details(sid)
        await hub.auto_step(sid,data,saved)
        data,saved=service.details(sid)
        assert data["status"]=="waiting_authorization" and saved["codearts_registration_confirmed"]
        assert not saved["codearts_registration_pending"] and saved["codearts_oauth_opened"]
        assert any(p.endswith('/goto') and b["url"]==started["codearts_oauth_url"] for p,b in calls)
        from sqlalchemy import select
        from app.models import Account
        with store.read() as (s,_):assert list(s.scalars(select(Account)))==[]
        store.engine.dispose()
    asyncio.run(run())


def test_task_service_persists_intent_and_recovers_without_second_sms(tmp_path):
    now=[1794801600.0]
    store=Store(f"sqlite:///{tmp_path/'db'}",Fernet.generate_key(),clock=lambda:now[0])
    registry=SimpleNamespace(enabled=True)
    service=RegistrationService(store,registry)
    cid=service.sms.configure(ChannelInput(name="test",provider="eomsg",base_url="https://sms.example.test/api",token="synthetic",keyword="test"))
    sid=uid()
    with store.tx() as (s,_):
        s.add(Resource(id=sid,kind="registration",name="test",data=json.dumps({"platform":"小浣熊","mode":"official","status":"waiting_authorization","account_id":None,"expires_at":now[0]+600}),secret=store.cipher.encrypt(json.dumps({"auto_run":True,"phone":"13800000001","authorization_url":"https://xiaohuanxiong.com/authorize"}).encode()).decode(),updated_at=now[0]))
    fake=FakeExecutor();calls=[]
    original=fake.handler
    def handler(request):
        if request.url.path.endswith('/flow'):
            body=json.loads(request.content)
            if body.get("action","observe")=="observe":
                return httpx.Response(200,json={"stage":"waiting_code" if body["sms_attempted"] else "ready_to_send","action":"submit_sms" if body["sms_attempted"] else "send_sms"})
            calls.append(body["action"])
            if body["action"]=="send_sms":
                # Click accepted, HTTP reply is lost: durable intent prevents resend.
                raise httpx.ReadError("synthetic lost response")
            return httpx.Response(200,json={"stage":"sms_verify_attempted"})
        return original(request)
    client=ExecutorClient("http://browser:4190","executor-token-123456",httpx.AsyncClient(transport=httpx.MockTransport(handler),headers={"Authorization":"Bearer executor-token-123456"}))
    hub=BrowserHub(store,registry,service,client)
    lease=service.sms.existing_number(cid,sid,"13800000001")
    hub.persist(sid,{"sms_lease_id":lease})
    async def poll(_):return "123456"
    service.sms.poll=poll
    async def run():
        with pytest.raises(Exception):await hub.watch_one(sid)
        data,material=service.details(sid)
        assert material["browser_sms_intent"]
        fake.restart()
        await hub.watch_one(sid)
        await hub.watch_one(sid)
        assert calls==["send_sms"]
        assert service.details(sid)[0]["browser_stage"]=="sms_send_unconfirmed"
        hub.persist(sid,{"browser_sms_confirmed":True,"browser_sms_observation":{"confirmed":True,"status":200}})
        await hub.watch_one(sid)
        assert calls==["send_sms","submit_sms"]
        public=json.dumps(store.resources("registration"))
        assert "123456" not in public and "13800000001" not in public
        await client.close()
    try:asyncio.run(run())
    finally:store.engine.dispose()


@pytest.mark.parametrize("status,value,confirmed,rejected", [
    (400,{"message":"private upstream text"},False,True),
    (429,{},False,True),
    (408,{},False,False),
    (502,None,False,False),
    (200,{"code":"SMS_REJECTED","phone":"private"},False,True),
    (200,{"success":False},False,True),
    (200,{"unknown":"private"},False,False),
    (200,{"code":0},True,False),
    (200,{"success":True},True,False),
])
def test_sms_response_facts_reject_or_remain_uncertain_without_private_body(status,value,confirmed,rejected):
    from browser_executor.main import sms_response_observation
    result=sms_response_observation(status,value,"account.sensetime.com")
    assert result["confirmed"] is confirmed and result["rejected"] is rejected
    assert "private" not in json.dumps(result) and "SMS_REJECTED" not in json.dumps(result)


@pytest.mark.parametrize("observation", [{"status":400,"confirmed":False}, {"status":200,"confirmed":False,"rejected":True}])
def test_official_sms_refusal_finishes_and_retires_without_counting_no_sms(tmp_path,observation):
    async def run():
        store=Store(f"sqlite:///{tmp_path/'db'}",Fernet.generate_key())
        registry=SimpleNamespace(enabled=True);service=RegistrationService(store,registry)
        cid=service.sms.configure(ChannelInput(name="test",provider="eomsg",base_url="https://sms.example.test/api",token="synthetic",keyword="test"))
        sid=uid()
        material={"auto_run":True,"phone":"13800000001","browser_sms_intent":{"operation":"once"},"browser_sms_observation":observation}
        with store.tx() as (s,_):
            s.add(Resource(id=sid,kind="registration",name="test",data=json.dumps({"platform":"小浣熊","mode":"official","status":"waiting_authorization","account_id":None}),secret=store.cipher.encrypt(json.dumps(material).encode()).decode(),updated_at=store.clock()))
        class Executor:
            async def call(self,*args,**kwargs):
                raise AssertionError("definitive refusal must not call browser again")
        hub=BrowserHub(store,registry,service,Executor())
        lease=service.sms.existing_number(cid,sid,"13800000001")
        # This is a synthetic newly acquired Eomsg lease. Retirement must show
        # provider auto-release instead of inventing an explicit API release.
        service.sms.update(lease,{"existing_number":False})
        hub.persist(sid,{"sms_lease_id":lease})
        data,saved=service.details(sid)
        await hub.auto_step(sid,data,saved)
        data,saved=service.details(sid)
        assert data["status"]=="failed" and data["error"]=="sms_send_rejected"
        assert data["browser_stage"]=="sms_send_rejected" and data["browser_sms_http_status"]==observation["status"]
        assert not data.get("failure_counted") and not data["result_unresolved"]
        assert store.resources("phone_rejected") == []
        lease_data,_=service.sms.lease(lease)
        assert lease_data["status"]=="provider_auto_release" and not lease_data["release_confirmed"]
        store.engine.dispose()
    asyncio.run(run())
