"""Only generated keys and loopback peers; never contact an external service."""
import base64
import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.proxy_protocols import parse_node
from app.proxy_kernel import reserve_nodes,build_config
from app.store import DomainError,Store
from app.subscriptions import parse_subscription,save_nodes


def key(seed):return base64.b64encode(bytes([seed])*32).decode()


def wireguard():
    return {'type':'wireguard','name':'owned-egress','ip':'10.0.0.2/32',
            'private-key':key(1),'mtu':1280,'udp':True,'persistent-keepalive':25,
            'peers':[{'server':'127.0.0.1','port':51820,'public-key':key(2),
                      'reserved':[1,2,3],'allowed-ips':['0.0.0.0/0','::/0']}]}


def shadowsocks():
    return {'type':'ss','server':'127.0.0.1','port':15443,'cipher':'chacha20-ietf-poly1305',
            'password':'generated-test-only','udp':True,'plugin':'obfs',
            'plugin-opts':{'mode':'tls','host':'example.com'},'udp-over-tcp':True,'client-fingerprint':'chrome'}


@pytest.mark.parametrize('factory',[wireguard,shadowsocks])
def test_owned_configuration_roundtrips_without_dropping_fields(factory):
    node=factory();parsed=parse_node(node)
    assert parsed['kernel']=={k:v for k,v in node.items() if k!='name'}
    assert parse_node(parsed['kernel'])['identity']==parsed['identity']
    changed={**node,'name':'changed-name'}
    assert parse_node(changed)['identity']==parsed['identity']


@pytest.mark.parametrize('change',[
    {'private-key':'not-a-key'}, {'ip':'::1/128'}, {'ip':'bad'}, {'mtu':True},
    {'peers':[]}, {'peers':[{'server':'127.0.0.1','port':51820,'public-key':key(2)}]},
    {'peers':[{'server':'127.0.0.1','port':51820,'public-key':key(2),'reserved':[1,2,300],'allowed-ips':['0.0.0.0/0']}]},
    {'remote-dns-resolve':True}, {'post-up':'command'}, {'dialer-proxy':'other'},
    {'server':'127.0.0.1'}, {'dns':['example.com']},
])
def test_wireguard_invalid_or_unmapped_options_are_explicitly_rejected(change):
    with pytest.raises(DomainError) as error:parse_node({**wireguard(),**change})
    assert key(1) not in str(error.value)


@pytest.mark.parametrize('change',[
    {'plugin':'external-binary'}, {'plugin-opts':{'mode':'tls','command':'bad'}},
    {'plugin-opts':{'mode':'unknown'}}, {'udp-over-tcp':'false'},
    {'udp-over-tcp-version':3}, {'client-fingerprint':'unsupported'},
])
def test_ss_unsupported_plugin_options_are_not_discarded(change):
    with pytest.raises(DomainError):parse_node({**shadowsocks(),**change})


@pytest.mark.parametrize('factory',[wireguard,shadowsocks])
def test_manual_node_creation_encrypts_material_and_reserves_authenticated_listener(tmp_path,factory):
    store=Store(f"sqlite:///{tmp_path/'native-egress.db'}",Fernet.generate_key())
    try:
        client=TestClient(create_app(store,start_worker=False))
        response=client.post('/api/proxies',headers={'x-console-client':'unified-local'},
                             json={'name':'owned-egress','url':json.dumps([factory()])})
        assert response.status_code==201,response.text
        identity=response.json()['id']
        nodes=reserve_nodes(store)
        assert len(nodes)==1 and nodes[0]['id']==identity
        config=build_config(nodes,'generated-controller-secret',19090)
        assert config['listeners'][0]['users'][0]['password']
        assert config['listeners'][0]['proxy']=='node-'+identity
        assert config['rules']==['MATCH,REJECT']
        public=json.dumps(store.resources('proxy')+store.resources('proxy_kernel_source'))
        assert key(1) not in public and 'generated-test-only' not in public
        assert store.resources('proxy')[0]['data']['status']=='pending_kernel'
    finally:store.engine.dispose()


def test_wireguard_subscription_persists_and_deduplicates(tmp_path):
    store=Store(f"sqlite:///{tmp_path/'native-sub.db'}",Fernet.generate_key())
    try:
        sid=store.resource_put('proxy_subscription','owned-sub',{})
        nodes,rejected=parse_subscription(json.dumps({'proxies':[wireguard(),wireguard()]}))
        assert len(nodes)==1 and not rejected
        save_nodes(store,sid,nodes,rejected)
        assert len(reserve_nodes(store))==1
        assert store.resources('proxy')[0]['data']['scheme']=='wireguard'
    finally:store.engine.dispose()
