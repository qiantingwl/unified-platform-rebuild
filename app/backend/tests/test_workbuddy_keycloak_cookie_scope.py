"""r18 regression: user's 7863 cookiejar.go compatibility, real httpx jar."""
import asyncio

import httpx
import pytest

from app.upstream import Transport
from app.workbuddy_sms import WorkBuddySMS


@pytest.mark.parametrize('header', [
    'AUTH_SESSION_ID=synthetic; Version=1; Path="/auth/realms/copilot/"; Secure; HttpOnly; SameSite=None',
    'AUTH_SESSION_ID=synthetic; Path=/auth/realms/copilot/; Secure',
    'AUTH_SESSION_ID=synthetic; Version=1; Secure',
])
def test_broker_receives_realm_cookie_but_other_paths_do_not(header):
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/openid-connect/auth'):
            return httpx.Response(200, headers=[('set-cookie', header), ('set-cookie', 'ordinary=kept; Path=/console/')])
        return httpx.Response(200)
    async def flow():
        transport = Transport(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        adapter = WorkBuddySMS(transport)
        try:
            for path in ['/auth/realms/copilot/protocol/openid-connect/auth',
                         '/auth/realms/copilot/login-actions/first-broker-login',
                         '/auth/realms/other/login-actions/first-broker-login', '/console/accounts']:
                await adapter.raw('GET', adapter.console + path)
            assert len([c for c in transport.client.cookies.jar if c.name == 'AUTH_SESSION_ID']) == 1
        finally:
            await transport.close()
    asyncio.run(flow())
    assert 'AUTH_SESSION_ID=synthetic' in seen[1].headers.get('cookie', '')
    assert 'AUTH_SESSION_ID' not in seen[2].headers.get('cookie', '')
    assert 'AUTH_SESSION_ID' not in seen[3].headers.get('cookie', '')
    assert 'ordinary=kept' in seen[3].headers.get('cookie', '')


def test_malformed_cross_domain_cookie_is_not_accepted():
    async def flow():
        def handler(request):
            return httpx.Response(200, headers={'set-cookie': 'AUTH_SESSION_ID=synthetic; Domain=unrelated.example; Path="/auth/realms/copilot/"; Version=1'})
        transport = Transport(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            await WorkBuddySMS(transport).raw('GET', 'https://www.codebuddy.cn/auth/realms/copilot/protocol/openid-connect/auth')
            assert not list(transport.client.cookies.jar)
        finally:
            await transport.close()
    asyncio.run(flow())


@pytest.mark.parametrize('url', [
    'https://oauth2.account.tencent.com/auth/realms/copilot/auth',
    'https://www.codebuddy.cn/console/accounts',
])
def test_normalization_never_applies_outside_console_realm(url):
    async def flow():
        header = 'ordinary=synthetic; Path="/limited/"; Version=1'
        def handler(request):
            return httpx.Response(200, headers={'set-cookie': header})
        transport = Transport(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            _, headers, _ = await WorkBuddySMS(transport).raw('GET', url)
            assert headers['set-cookie'] == header
            assert [c.path for c in transport.client.cookies.jar] == ['%22/limited/%22']
        finally:
            await transport.close()
    asyncio.run(flow())
