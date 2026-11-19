import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from types import SimpleNamespace

from app.models import Resource
from app.store import Store,DomainError
from app.subscriptions import save_nodes,parse_subscription,save_schedule,ScheduleInput
from app.subscription_scheduler import SubscriptionScheduler
from app.upstream import Transport


@pytest.fixture
def store(tmp_path):
    s=Store(f'sqlite:///{tmp_path/"recommended.db"}',Fernet.generate_key())
    yield s
    s.engine.dispose()


def test_native_profile_refresh_saves_metrics_and_retires_dropped_selection(store):
    sid=store.resource_put('proxy_subscription','recommend',{'source_profile':'leidahub_fast','auto_update':True,'recommendation_limit':100},secret='https://ignored.invalid')
    old,rejected=parse_subscription('http://user:synthetic@previous.invalid:8123')
    save_nodes(store,sid,old,rejected)
    payload={'tested':True,'test_mode':'real','updated_at':'2026-11-18T01:00:00Z','vantages':[{'cc':'JP'}],
        'best':[['trojan://synthetic@example.com:443',4,[20],[12.5],5],['trojan://unmeasured@example.com:443',1,[20],[None],2]]}
    def handler(request):
        assert str(request.url)=='https://jiedianleida.com/'
        return httpx.Response(200,text='<script id="nodes-data">'+json.dumps(payload)+'</script>')
    def factory(**_):
        t=Transport(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        t.owned=True
        return t
    service=SubscriptionScheduler(store,SimpleNamespace(enabled=True,transport_factory=factory))
    summary=asyncio.run(service.sync(sid))
    assert summary['partial_retained'] is False
    nodes=store.resources('proxy')
    assert len(nodes)==2
    assert next(n for n in nodes if n['data']['scheme']=='http')['data']['status']=='retired'
    new=next(n for n in nodes if n['data']['scheme']=='trojan')
    assert new['data']['status']=='pending_kernel' and not new['has_secret']
    assert new['data']['subscription_source_metrics'][sid]['speed_mbps']==12.5
    meta=store.resources('proxy_subscription')[0]['data']['source_metadata']
    assert meta['selected']==1 and meta['ovh_verified'] is False


def test_changing_selection_invalidates_old_inflight_result(store):
    sid=store.resource_put('proxy_subscription','s',{'auto_update':True},secret='https://example.com')
    service=SubscriptionScheduler(store,SimpleNamespace(enabled=True))
    token,_=service.claim(sid)
    source=store.resources('proxy_subscription')[0]
    save_schedule(store,sid,ScheduleInput(expected_version=source['version'],auto_update=True,source_profile='leidahub_fast'))
    nodes,rejected=parse_subscription('http://user:synthetic@host:8123')
    with pytest.raises(DomainError,match='subscription_sync_superseded'):
        save_nodes(store,sid,nodes,rejected,token)
    assert not store.resources('proxy')
