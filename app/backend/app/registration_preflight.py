"""Local registration diagnostics, with no purchase, SMS, probe or mutation.

Unlike 7866 engine.preflight, this endpoint deliberately does not query an SMS
provider balance. Configuration completeness is not real registration success.
"""
import json
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken
from pydantic import BaseModel,ConfigDict,Field

from .login_connectors import SUPPORTED
from .models import Resource
from .proxy_pool import select_registration_proxy
from .store import DomainError


class PreflightInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    platform:str=Field(min_length=1,max_length=60)
    mode:str=Field(min_length=1,max_length=40)
    sms_channel_id:str | None=Field(default=None,max_length=100)
    proxy_id:str | None=Field(default=None,max_length=100)
    account_id:str | None=Field(default=None,max_length=100)


def registration_preflight(service,body):
    store=service.store
    checks=[]
    def add(code,status,label):
        checks.append({"code":code,"status":status,"label":label})

    supported=body.mode in SUPPORTED.get(body.platform,[])
    add("registration_mode","pass" if supported else "fail",
        "平台和登录方式已接入" if supported else "平台或登录方式尚未接入")
    if not service.registry.enabled:
        add("upstream_execution","warning","当前上游执行开关未开启；本次只检查配置")

    account_valid=True
    if body.account_id:
        with store.read() as (s,_):
            try:
                account=store.account(s,body.account_id)
                account_valid=account.platform==body.platform and not account.sandbox
            except DomainError:
                account_valid=False
        add("account_target","pass" if account_valid else "fail",
            "目标账户与平台一致" if account_valid else "目标账户不存在、平台不符或不是实际账户")

    if body.sms_channel_id:
        try:
            with store.read() as (s,_):
                row=s.get(Resource,body.sms_channel_id)
                if not row or row.kind!="sms_channel":
                    raise DomainError("sms_channel_not_found")
                enabled=json.loads(row.data).get("enabled") is not False
                data=json.loads(store.cipher.decrypt(row.secret.encode())) if row.secret else {}
            if not enabled:
                add("sms_channel","fail","所选接码渠道未开启")
            else:
                provider=data.get("provider")
                base=urlsplit(str(data.get("base_url") or ""))
                pairs=data.get("pairs") or []
                tokens=data.get("tokens") or [data.get("token")]
                token_ok=any(isinstance(t,str) and bool(t.strip()) for t in tokens)
                pair_ok=bool(pairs) and all(isinstance(p,dict) and p.get("token") and p.get("project") for p in pairs)
                project_ok=bool(data.get("project")) or pair_ok
                keyword_ok=bool(data.get("keyword")) or bool(data.get("keyword_get") and data.get("keyword_msg"))
                complete=(base.scheme=="https" and bool(base.hostname) and not base.username
                          and provider in {"haozhuma","eomsg"} and (token_ok or pair_ok)
                          and (project_ok if provider=="haozhuma" else keyword_ok))
                add("sms_channel","pass" if complete else "fail",
                    "所选接码渠道配置完整；余额和号源未实测" if complete else "接码渠道缺少有效接口、凭据或项目/关键词配置")
        except (DomainError,InvalidToken,ValueError,TypeError,AttributeError):
            add("sms_channel","fail","所选接码渠道不存在或配置无法读取")
    elif body.mode in {"sms","register","browser"} or body.platform=="小浣熊" and body.mode=="official":
        add("sms_channel","warning","未选择接码渠道；自动取号收码未就绪，可按所选方式提供现有号码和验证码")
    else:
        add("sms_channel","not_required","当前方式不要求接码渠道")

    if body.mode=="password":
        add("login_material","unverified","此预检不接收密码；提交登录时仍需提供有效号码和密码")
    elif body.mode=="verified_import":
        add("login_material","unverified","此预检不接收凭证；导入时仍需核对真实身份和凭证")

    try:
        if not account_valid:
            raise DomainError("account_not_found")
        with store.read() as (s,_):
            _,endpoint=select_registration_proxy(store,s,body.platform,"local-preflight",body.proxy_id,body.account_id)
        if endpoint:
            parsed=urlsplit(endpoint)
            if parsed.scheme not in {"http","https","socks5"} or not parsed.hostname:
                raise ValueError()
            _=parsed.port  # invalid ports must not be advertised as configured
            add("proxy_configuration","pass","已配置可选代理；仅核对本地配置和现有状态，出口连通性未实测")
        else:
            add("proxy_configuration","warning","当前配置为直连；未验证该出口是否满足平台要求")
    except (DomainError,InvalidToken,ValueError,TypeError,AttributeError,KeyError):
        add("proxy_configuration","fail","代理配置缺失、已停用或当前代理池无可选节点")

    browser_required=(body.mode in {"browser","official","oauth_qq","oauth_wechat"}
                      or body.platform=="CodeArts" and body.mode=="register")
    if browser_required:
        heartbeat=store.service_status("tasks")
        health=heartbeat.get("browser_executor")
        health=health if isinstance(health,dict) else {}
        if not heartbeat.get("online"):
            add("browser_executor","unverified","缺少近期任务服务心跳，无法确认浏览器执行器在线")
        elif health.get("status")!="ok":
            add("browser_executor","fail","浏览器执行器未配置、不可达或尚未就绪")
        elif health.get("last_error"):
            add("browser_executor","warning","浏览器执行器在线，但最近报告过执行错误，需查看任务记录")
        elif (type(health.get("sessions")) is int and type(health.get("max_sessions")) is int
              and health["max_sessions"]>0 and health["sessions"]>=health["max_sessions"]):
            add("browser_executor","warning","浏览器执行器在线，现有会话已占满，需等待空闲位置")
        else:
            add("browser_executor","pass","浏览器执行器近期心跳正常；页面和授权流程尚未实测")
    else:
        add("browser_executor","not_required","当前方式不依赖浏览器执行器")
    add("real_registration","unverified","未购号、未发短信、未访问上游；号源、验证码和真实注册结果尚未验证")
    return {"checks":checks,"configuration_ok":not any(c["status"]=="fail" for c in checks),
            "registration_verified":False,"read_only":True,"checked_at":store.clock()}


def mount_registration_preflight(app,service):
    @app.post("/api/registration/preflight")
    def preflight(body:PreflightInput):
        return registration_preflight(service,body)
