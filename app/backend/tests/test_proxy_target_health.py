import asyncio
import json
import ssl
import threading

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Resource
from app.proxy_target_health import KIND, TARGET_URLS, ProxyTargetHealth, _error_code
from app.store import DomainError, Store


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'probe.db'}", Fernet.generate_key())
    nodes = [store.resource_put("proxy", f"node-{i}", {"status": "untested"}, secret=f"http://user:synthetic-secret@proxy-{i}:8080") for i in range(4)]
    yield store, nodes
    store.engine.dispose()


def mock_client(handler, calls=None):
    def factory(**kwargs):
        assert kwargs["verify"] is True
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        endpoint = kwargs.pop("proxy")
        assert endpoint.startswith("http://")
        if calls is not None:
            calls.append(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    return factory


@pytest.mark.parametrize("status,code", [(200,"target_http_ok"),(302,"target_redirect"),(401,"target_auth_required"),(403,"target_forbidden"),(429,"target_rate_limited"),(503,"target_server_error")])
def test_probe_contacts_platform_without_credentials_and_never_claims_model_success(env, status, code):
    store, nodes = env
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "HEAD"
        assert str(request.url) == TARGET_URLS["Loomy"]
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert not request.content
        return httpx.Response(status, headers={"Location":"/login"})

    service = ProxyTargetHealth(store, client_factory=mock_client(handler))
    result = asyncio.run(service.check(nodes[0], "Loomy"))
    assert len(requests) == 1
    assert result["code"] == code and result["http_status"] == status
    assert result["tcp_reachable"] and result["target_http_reachable"]
    assert not result["model_verified"] and not result["account_verified"] and not result["business_verified"]
    assert result["stored"] and result["checked_at"] >= result["started_at"]
    assert result["eligible"] is (status < 400)
    assert "synthetic-secret" not in json.dumps(result)
    assert store.resources("proxy")[0]["data"]["status"] == "untested"
    assert store.resources("proxy_health") == []


def test_tls_failure_retains_tcp_evidence_without_claiming_target_http(env):
    store, nodes = env

    async def handler(request):
        await request.extensions["trace"]("connection.connect_tcp.complete", {"private": "never-persist"})
        try:
            raise ssl.SSLCertVerificationError("synthetic-secret must never be emitted")
        except ssl.SSLError as cause:
            raise httpx.ConnectError("private-error") from cause

    service = ProxyTargetHealth(store, client_factory=mock_client(handler))
    result = asyncio.run(service.check(nodes[0], "Loomy"))
    assert result["code"] == "target_tls_error" and result["tcp_reachable"] is True
    assert not result["target_http_reachable"] and result["http_status"] is None
    assert "private" not in json.dumps(result) and "synthetic-secret" not in json.dumps(result)


@pytest.mark.parametrize("want", [ssl.SSLWantReadError,ssl.SSLWantWriteError])
@pytest.mark.parametrize("timeout,expected", [(httpx.ConnectTimeout,"proxy_connect_timeout"),(httpx.ReadTimeout,"target_timeout"),(TimeoutError,"target_timeout")])
def test_nonblocking_ssl_context_does_not_misclassify_timeout(want,timeout,expected):
    error=timeout("private-timeout")
    error.__context__=want("normal_nonblocking_retry")
    assert _error_code(error)==expected


def test_certificate_failure_remains_tls_even_when_wrapped_by_timeout():
    error=httpx.ConnectTimeout("outer")
    error.__cause__=ssl.SSLCertVerificationError("certificate verify failed")
    assert _error_code(error)=="target_tls_error"


def test_fatal_ssl_without_timeout_remains_tls_and_want_alone_is_not_fatal():
    error=httpx.ConnectError("outer")
    error.__cause__=ssl.SSLError("fatal handshake alert")
    assert _error_code(error)=="target_tls_error"
    for want in (ssl.SSLWantReadError,ssl.SSLWantWriteError):
        error.__cause__=want("normal retry")
        assert _error_code(error)=="proxy_connect_error"


def test_timeout_takes_precedence_over_non_certificate_ssl_context():
    error=httpx.ConnectTimeout("outer")
    error.__context__=ssl.SSLError("non-certificate context")
    assert _error_code(error)=="proxy_connect_timeout"


def test_nested_timeout_taxonomy_and_exception_cycles_are_bounded():
    error=httpx.ConnectError("outer")
    nested=httpx.ConnectTimeout("inner")
    error.__cause__=nested
    nested.__context__=error
    assert _error_code(error)=="proxy_connect_timeout"


def test_platform_checks_are_separate_and_failure_never_disables_node(env):
    store, nodes = env
    def handler(request):
        return httpx.Response(403 if request.url.host == "loomyad.xunfei.cn" else 200)
    service = ProxyTargetHealth(store, client_factory=mock_client(handler))
    results = asyncio.run(service.check_many([nodes[0]], ["Loomy", "WPS 灵犀"]))
    assert {row["code"] for row in results} == {"target_forbidden", "target_http_ok"}
    assert len(store.resources(KIND)) == 2
    assert store.resources("proxy")[0]["version"] == 1


def test_endpoint_change_discards_old_success(env):
    store, nodes = env
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200)
        service = ProxyTargetHealth(store, client_factory=mock_client(handler))
        pending = asyncio.create_task(service.check(nodes[0], "Loomy"))
        await entered.wait()
        with store.tx() as (s, _):
            node = s.get(Resource, nodes[0])
            # Fingerprint fence still works if an old caller forgot the version bump.
            node.secret = store.cipher.encrypt(b"http://replacement:8080").decode()
        release.set()
        result = await pending
        assert result["status"] == "stale" and result["code"] == "proxy_changed"
        assert not result["target_http_reachable"] and result["http_status"] is None
        assert store.resources(KIND)[0]["data"]["code"] == "proxy_changed"
    asyncio.run(scenario())


def test_old_check_cannot_overwrite_newer_result_for_same_node_and_platform(env):
    store, nodes = env
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await release.wait()
                return httpx.Response(200)
            return httpx.Response(429)
        service = ProxyTargetHealth(store, client_factory=mock_client(handler))
        first = asyncio.create_task(service.check(nodes[0], "Loomy"))
        await entered.wait()
        latest = await service.check(nodes[0], "Loomy")
        release.set()
        old = await first
        assert latest["code"] == "target_rate_limited" and latest["stored"]
        assert old["code"] == "check_superseded" and not old["stored"]
        assert store.resources(KIND)[0]["data"]["code"] == "target_rate_limited"
    asyncio.run(scenario())


def test_batch_has_bounded_parallelism_and_explicit_targets(env):
    store, nodes = env
    async def scenario():
        active, maximum, count = 0, 0, 0
        async def handler(request):
            nonlocal active, maximum, count
            active += 1
            maximum = max(maximum, active)
            count += 1
            await asyncio.sleep(.01)
            active -= 1
            return httpx.Response(200)
        service = ProxyTargetHealth(store, concurrency=2, client_factory=mock_client(handler))
        result = await service.check_many(nodes, ["Loomy", "WPS 灵犀"])
        assert len(result) == 8 and count == 12 and maximum <= 2
        assert {row["platform"] for row in result} == {"Loomy", "WPS 灵犀"}
        with pytest.raises(DomainError) as error:
            await service.check_many([], ["Loomy"])
        assert error.value.code == "invalid_proxy_check_batch"
        with pytest.raises(DomainError):
            await service.check_many([str(x) for x in range(257)], ["Loomy"])
        assert count == 12
    asyncio.run(scenario())


def test_cancelled_probe_leaves_terminal_record(env):
    store, nodes = env
    async def scenario():
        entered = asyncio.Event()
        async def handler(request):
            entered.set()
            await asyncio.Event().wait()
        service = ProxyTargetHealth(store, client_factory=mock_client(handler))
        pending = asyncio.create_task(service.check(nodes[0], "Loomy"))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert store.resources(KIND)[0]["data"]["status"] == "cancelled"
    asyncio.run(scenario())


def test_cancel_during_db_start_joins_late_transaction_then_finishes(env):
    store, nodes = env
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        service = ProxyTargetHealth(store, client_factory=mock_client(lambda request: pytest.fail("No target call after cancelled start")))
        original = service._begin
        def slow_begin(*args):
            entered.set()
            assert release.wait(2)
            return original(*args)
        service._begin = slow_begin
        pending = asyncio.create_task(service.check(nodes[0], "Loomy"))
        await asyncio.to_thread(entered.wait)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert store.resources(KIND)[0]["data"]["status"] == "cancelled"
    asyncio.run(scenario())


def test_disabled_checks_and_unknown_targets_do_not_start_or_scan(env):
    store, nodes = env
    service = ProxyTargetHealth(store, enabled=False)
    with pytest.raises(DomainError) as error:
        asyncio.run(service.check(nodes[0], "Loomy"))
    assert error.value.code == "network_execution_disabled"
    with pytest.raises(DomainError):
        asyncio.run(ProxyTargetHealth(store).check(nodes[0], "https://untrusted.example"))
    assert store.resources(KIND) == []


def test_405_falls_back_to_one_header_only_get(env):
    store, nodes = env
    methods = []

    class MustNotRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("Probe must never read target body")
            yield b""

    def handler(request):
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        assert request.headers["range"] == "bytes=0-8191"
        return httpx.Response(200, stream=MustNotRead())

    result = asyncio.run(ProxyTargetHealth(store, client_factory=mock_client(handler)).check(nodes[0], "Loomy"))
    assert methods == ["HEAD", "GET"]
    assert result["eligible"] and result["method"] == "GET"
    assert result["targets"][0]["body_bytes_read"] == 0


@pytest.mark.parametrize("location", ["https://elsewhere.example/login", "http://loomyad.xunfei.cn/login", "https://loomyad.xunfei.cn:8443/login", "https://user@loomyad.xunfei.cn/login", ""])
def test_redirect_is_not_followed_or_admitted_outside_same_https_origin(env, location):
    store, nodes = env
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": location})
    result = asyncio.run(ProxyTargetHealth(store, client_factory=mock_client(handler)).check(nodes[0], "Loomy"))
    assert len(seen) == 1 and result["target_http_reachable"]
    assert not result["eligible"]
    assert result["code"] in {"target_external_redirect", "target_redirect_missing_location"}


def test_wps_checks_both_actual_api_hosts_and_does_not_admit_partial_success(env):
    store, nodes = env
    seen = []
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200 if request.url.host == "lingxi.wps.cn" else 403)
    result = asyncio.run(ProxyTargetHealth(store, client_factory=mock_client(handler)).check(nodes[0], "WPS 灵犀"))
    assert seen == ["lingxi.wps.cn", "lingxi.kdocs.cn"]
    assert result["target_http_reachable"] and not result["eligible"]
    assert result["code"] == "target_forbidden"
    assert [r["eligible"] for r in result["targets"]] == [True, False]
    assert result["verification_scope"] == "https_target_origins_only"


def test_probe_timeout_is_bounded_and_has_a_terminal_classification(env):
    store, nodes = env
    async def handler(request):
        await asyncio.Event().wait()
    service = ProxyTargetHealth(store, timeout=.01, client_factory=mock_client(handler))
    result = asyncio.run(service.check(nodes[0], "Loomy"))
    assert result["code"] == "target_timeout"
    assert result["status"] == "completed" and not result["eligible"]


def test_target_origins_match_the_current_adapters():
    from urllib.parse import urlsplit
    from app.adapters import Loomy, Raccoon, WorkBuddy, WorkBuddyInternational, WPS
    from app.vendor.mirai.codearts import endpoints as codearts
    from app.vendor.mirai.wps_lingxi import endpoints as wps
    for platform, base in [("Loomy",Loomy.base),("小浣熊",Raccoon.base),("WorkBuddy 国内",WorkBuddy.base),("WorkBuddy 国际",WorkBuddyInternational.base),("WPS 灵犀",WPS.base),("CodeArts",codearts.BASE)]:
        assert urlsplit(TARGET_URLS[platform]).netloc == urlsplit(base).netloc
    from app.proxy_target_health import TARGET_GROUPS
    assert TARGET_GROUPS["WPS 灵犀"] == wps.CHAT_BASES


def test_refresh_keeps_previous_evidence_until_new_result_commits(env):
    store,nodes=env
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        count=0
        async def handler(request):
            nonlocal count
            count+=1
            if count==1:return httpx.Response(200)
            entered.set();await release.wait()
            return httpx.Response(403)
        service=ProxyTargetHealth(store,client_factory=mock_client(handler))
        first=await service.check(nodes[0],"Loomy")
        pending=asyncio.create_task(service.check(nodes[0],"Loomy"))
        await entered.wait()
        during=store.resources(KIND)[0]["data"]
        assert during["eligible"] and during["checked_at"]==first["checked_at"]
        assert during["inflight_check_id"]!=first["check_id"]
        release.set();await pending
        assert not store.resources(KIND)[0]["data"]["eligible"]
    asyncio.run(scenario())


@pytest.mark.parametrize("platform",["Loomy","WorkBuddy 国内"])
def test_exact_api_only_root_404_is_connectivity_only(env,platform):
    store,nodes=env
    seen=[]
    def handler(request):
        seen.append((request.method,str(request.url)))
        return httpx.Response(404)
    result=asyncio.run(ProxyTargetHealth(store,client_factory=mock_client(handler)).check(nodes[0],platform))
    assert seen==[("HEAD",TARGET_URLS[platform])]
    assert result["http_status"]==404 and result["code"]=="target_root_not_found"
    assert result["eligible"] and result["tcp_reachable"] and result["target_http_reachable"]
    assert result["verification_scope"]=="https_target_origins_only"
    assert not result["model_verified"] and not result["account_verified"] and not result["business_verified"]


@pytest.mark.parametrize("platform",["小浣熊","WPS 灵犀","WorkBuddy 国际","CodeArts","Marvis"])
def test_other_platform_404_does_not_get_api_only_root_exception(env,platform):
    store,nodes=env
    result=asyncio.run(ProxyTargetHealth(store,client_factory=mock_client(lambda request:httpx.Response(404))).check(nodes[0],platform))
    assert result["http_status"]==404 and result["code"]=="target_http_error"
    assert result["target_http_reachable"] and not result["eligible"]
    assert not result["model_verified"] and not result["business_verified"]
