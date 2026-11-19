import base64
import pytest

from app.proxy_protocols import parse_node
from app.store import DomainError


@pytest.mark.parametrize('uri,kind',[
    ('ss://'+base64.urlsafe_b64encode(b'aes-256-gcm:synthetic').decode()+'@example.com:443#test','ss'),
    ('trojan://synthetic@example.com:443?sni=example.com','trojan'),
    ('hysteria2://synthetic@example.com:443?sni=example.com&obfs=salamander&obfs-password=test','hysteria2'),
    ('hy2://synthetic@example.com:443','hysteria2'),
    ('vless://11111111-1111-1111-1111-111111111111@example.com:443?security=tls&type=ws&host=example.com&path=%2Fchat','vless'),
])
def test_supported_share_roundtrip(uri,kind):
    node=parse_node(uri)
    assert node['scheme']==kind
    assert parse_node(node['kernel'])['identity']==node['identity']
    renamed=dict(node['kernel'],name='other name')
    assert parse_node(renamed)['identity']==node['identity']


@pytest.mark.parametrize('uri',[
    'trojan://synthetic@example.com:443?allowInsecure=1',
    'hy2://synthetic@example.com:443?insecure=true',
    'ss://rc4-md5:synthetic@example.com:443',
    'ss://aes-256-gcm:synthetic@example.com:443?plugin=unknown',
    'vless://11111111-1111-1111-1111-111111111111@example.com:443?security=reality',
    'trojan://synthetic@example.com:443?type=kcp',
    'trojan://synthetic@example.com:443?sni=a&sni=b',
])
def test_reject_unsupported_connection_semantics(uri):
    with pytest.raises(DomainError) as error:parse_node(uri)
    assert 'synthetic' not in str(error.value)


def test_password_changes_do_not_share_identity():
    assert parse_node('trojan://one@example.com:443')['identity']!=parse_node('trojan://two@example.com:443')['identity']


@pytest.fixture
def ss_obfs():
    return {'type':'ss','server':'example.com','port':443,'cipher':'aes-256-gcm',
            'password':'synthetic','plugin':'obfs','plugin-opts':{'mode':'tls'}}


def test_ss_tls_obfs_preserves_clienthello_marker_and_roundtrips(ss_obfs):
    marker='Signal.Example.com:713265'
    ss_obfs['plugin-opts']['host']=marker
    node=parse_node(ss_obfs)
    assert node['kernel']['plugin-opts']['host']==marker
    assert (node['kernel']['server'],node['kernel']['port'])==('example.com',443)
    assert parse_node(node['kernel'])['identity']==node['identity']


@pytest.mark.parametrize(('mode','host'),[
    ('http','example.com:713265'), ('tls','https://example.com:713265'),
    ('tls','example.com:713265\n'), ('tls','example.com:１２３４５６'),
    ('tls','invalid..example:713265'), ('tls','example.com:713265/path'),
])
def test_ss_obfs_marker_does_not_relax_other_host_checks(ss_obfs,mode,host):
    ss_obfs['plugin-opts'].update(mode=mode,host=host)
    with pytest.raises(DomainError):
        parse_node(ss_obfs)
