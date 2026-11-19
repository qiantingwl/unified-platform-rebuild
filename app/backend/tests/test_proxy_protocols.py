import base64
import json

import pytest

from app.proxy_protocols import parse_node
from app.store import DomainError


def node(**kwargs):
    return {'type':'vmess','server':'example.com','port':443,'uuid':'1d9f8f99-1593-40d0-9802-f6ebc0f29077',**kwargs}


def test_share_and_clash_canonical_identity():
    raw={'v':'2','ps':'share','add':'EXAMPLE.com','port':'443','id':node()['uuid'],'aid':'0','net':'ws',
         'type':'none','tls':'tls','sni':'example.com','host':'example.com','path':'/proxy'}
    uri='vmess://'+base64.b64encode(json.dumps(raw).encode()).decode().rstrip('=')
    a=parse_node(uri)
    b=parse_node(node(name='renamed',network='ws',tls=True,servername='example.com',
                      **{'ws-opts':{'path':'/proxy','headers':{'Host':'example.com'}}}))
    assert a['identity']==b['identity']
    assert parse_node(a['kernel'])['identity']==a['identity']


@pytest.mark.parametrize('change',[{'uuid':'03a0d384-dc57-4d83-9443-bf92cff88018'}, {'port':8443},
                                  {'tls':True},{'network':'ws'},{'cipher':'chacha20-poly1305'}])
def test_connection_changes_do_not_merge(change):
    assert parse_node(node())['identity']!=parse_node(node(**change))['identity']


@pytest.mark.parametrize('change',[{'skip-cert-verify':True},{'reality-opts':{}},{'network':'kcp'},
                                  {'port':0},{'uuid':'secret-invalid'},{'tls':'false'},{'server':'user@host'},
                                  {'network':'ws','ws-opts':{'path':'/\r\nsecret'}}, {'port':True}])
def test_unsafe_or_unsupported_material_never_silently_downgrades(change):
    with pytest.raises(DomainError) as failure:
        parse_node(node(**change))
    assert 'secret' not in str(failure.value)


@pytest.mark.parametrize('value',['vmess://bad','vmess://W10=','vless://secret',{}, {'type':'http'}])
def test_bad_material_is_sanitized(value):
    with pytest.raises(DomainError):
        parse_node(value)
