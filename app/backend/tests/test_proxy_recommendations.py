import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app import proxy_recommendations as source
from app.store import DomainError


def uri(i):
    node={'v':'2','ps':'fake','add':'example.com','port':'443','id':f'12345678-1234-1234-1234-123456789ab{i}', 'aid':'0','scy':'auto','net':'tcp','tls':'tls'}
    return 'vmess://'+base64.b64encode(json.dumps(node).encode()).decode()


def page(rows, **changes):
    data={'tested':True,'test_mode':'real','updated_at':'2026-11-17T14:02:01+00:00','vantages':[{'cc':'JP'},{'cc':'SG'}],'best':rows}
    data.update(changes)
    return '<script type="application/json" id="nodes-data">'+json.dumps(data)+'</script>'


def row(i, speed, stable=1):
    return [uri(i),stable,[50,100],[speed,None],28]


def test_sorts_measured_unique_nodes_before_limit():
    nodes,rejected,metadata=source.parse_recommended(page([row(0,10),row(1,20),row(1,30),row(2,0),row(3,40,0)]),limit=2)
    assert [x['source_metrics']['speed_mbps'] for x in nodes]==[30,10]
    assert [x['source_metrics']['vantages'] for x in nodes]==[['JP'],['JP']]
    assert metadata=={'source_updated_at':'2026-11-17T14:02:01+00:00','considered':5,'speed_tested':3,'supported':2,'selected':2,'limit':2,'speed_unit':'MB/s','speed_origin':'source_reported','ovh_verified':False,'selection_complete':True}
    assert len(rejected)==2
    assert 'vmess://' not in json.dumps(metadata)+json.dumps(rejected)


@pytest.mark.parametrize('html',[page([row(0,0)]),page([row(0,1)],tested=False),'not html',page([row(0,1)])+page([row(0,1)])])
def test_empty_unmeasured_or_bad_page_never_returns_empty_success(html):
    with pytest.raises(DomainError):source.parse_recommended(html)


@pytest.mark.parametrize('limit',[0,501,True,'100'])
def test_bad_limit(limit):
    with pytest.raises(DomainError):source.parse_recommended(page([row(0,1)]),limit=limit)


def test_unsupported_or_malformed_node_reason_has_no_material():
    bad='unknown://private-secret@example.com:1'
    nodes,rejected,_=source.parse_recommended(page([[bad,1,[1,2],[99,None],1],row(0,1)]))
    assert len(nodes)==1 and len(rejected)==1
    assert 'private-secret' not in json.dumps(rejected)


def test_nonfinite_and_boolean_speed_not_eligible():
    rows=[row(0,float('inf')),row(1,float('nan')),row(2,True),row(3,1)]
    nodes,_,meta=source.parse_recommended(page(rows))
    assert len(nodes)==1 and meta['speed_tested']==1


def test_fetch_fixed_url_and_refuses_redirect():
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(302,headers={'Location':'http://127.0.0.1/private'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(DomainError,match='subscription_fetch_failed'):
                await source.fetch_recommended(SimpleNamespace(client=client))
    asyncio.run(run())
    assert len(calls)==1 and str(calls[0].url)==source.SOURCE_URL


def test_fetch_success_and_size_limit(monkeypatch):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,text=page([row(0,10)])))) as client:
            result=await source.fetch_recommended(SimpleNamespace(client=client))
            assert result[2]['selected']==1
        monkeypatch.setattr(source,'MAX_BYTES',32)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,content=b'x'*33))) as client:
            with pytest.raises(DomainError,match='subscription_too_large'):
                await source.fetch_recommended(SimpleNamespace(client=client))
    asyncio.run(run())


def test_bad_row_marks_selection_incomplete_but_unsupported_node_does_not():
    _,_,metadata=source.parse_recommended(page([row(0,1),['bad']]))
    assert metadata['selection_complete'] is False
    _,_,metadata=source.parse_recommended(page([row(0,1),['unsupported://private',1,[1,2],[1,None],1]]))
    assert metadata['selection_complete'] is True
