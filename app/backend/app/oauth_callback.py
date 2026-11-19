"""Authenticated local OAuth handoff; callback material never enters HTML/logs."""
import asyncio
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .store import DomainError


class CallbackInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    url:SecretStr=Field(max_length=16384)


PAGE='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>接收登录授权</title>
<body><main><h1>登录授权</h1><p id="status">正在核对回调，请保留此页面。</p><button id="retry" hidden>重新提交已收到的回调</button><p><a id="next" hidden rel="noreferrer">继续官方授权</a></p><p><a href="/#register">返回账户平台</a></p></main>
<script>const callback=location.href;history.replaceState(null,'','/oauth/callback');const message=document.getElementById('status'),retry=document.getElementById('retry'),next=document.getElementById('next');async function receive(){retry.hidden=true;try{const r=await fetch('/api/registration/oauth/receive',{method:'POST',headers:{'Content-Type':'application/json','X-Console-Client':'unified-local'},body:JSON.stringify({url:callback})});const b=await r.json();if(!r.ok){message.textContent=b.error?.code==='login_required'?'请先在管理面板登录，再返回这里重新提交。':'回调尚未保存，请回管理面板查看当前任务。';retry.hidden=false;return;}message.textContent=b.status==='succeeded'?'凭证已核验并保存，可以返回注册页面。':'回调已保存，请继续授权或回面板查看结果。';if(b.authorization_url){next.href=b.authorization_url;next.textContent='继续官方授权';next.hidden=false;}}catch{message.textContent='连接暂时中断，请重新提交这份回调；不要重新发起登录。';retry.hidden=false;}}retry.onclick=receive;receive();</script></body></html>'''


def mount_oauth_callback(app,service,browser):
    @app.post("/api/registration/{sid}/external-link")
    def external_link(sid:str,response:Response):
        data,material=service.details(sid)
        if data["platform"]!="CodeArts" or data["status"]!="waiting_authorization":
            raise DomainError("registration_state_conflict")
        if data["expires_at"]<=service.store.clock():
            raise DomainError("verification_expired")
        nonce=secrets.token_urlsafe(32)
        browser.persist(sid,{"external_nonce_hash":hashlib.sha256(nonce.encode()).hexdigest()})
        response.set_cookie("unified_oauth_launch",sid+"."+nonce,httponly=True,samesite="lax",max_age=600,path="/api/registration/oauth/receive")
        return {"url":material["authorization_url"]}

    @app.get("/oauth/callback")
    def callback_page():
        return HTMLResponse(PAGE,headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer","X-Frame-Options":"DENY","Content-Security-Policy":"default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})

    @app.post("/api/registration/oauth/receive")
    async def receive(body:CallbackInput,request:Request):
        cookie=request.cookies.get("unified_oauth_launch","")
        if "." not in cookie:
            raise DomainError("oauth_launch_required")
        sid,nonce=cookie.split(".",1)
        data,material=(await asyncio.to_thread(service.details, sid))
        digest=hashlib.sha256(nonce.encode()).hexdigest()
        if not secrets.compare_digest(digest,material.get("external_nonce_hash","")):
            raise DomainError("oauth_launch_required")
        url=body.url.get_secret_value()
        parsed,expected=urlsplit(url),urlsplit(material.get("callback_url",""))
        if data["platform"]!="CodeArts" or (parsed.scheme,parsed.netloc,parsed.path)!=(expected.scheme,expected.netloc,expected.path):
            raise DomainError("invalid_oauth_callback",422)
        params=parse_qs(parsed.query)
        state=params.get("state",params.get("ticket_id"))
        if state is not None and state!=[material.get("state")]:
            raise DomainError("oauth_state_mismatch",422)
        if data["status"]!="waiting_authorization":
            raise DomainError("registration_state_conflict")
        (await asyncio.to_thread(browser.persist, sid,{"captured_callback":url}))
        result=await service.run(sid,code=url,continuation=True)
        _,saved=(await asyncio.to_thread(service.details, sid))
        return {"status":result["status"],"authorization_url":saved.get("authorization_url") if result["status"]=="waiting_authorization" else None}
