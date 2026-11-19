import json

import pytest

from app.subscriptions import parse_subscription


@pytest.mark.parametrize('extra,reason',[
    ({'tls':True},'proxy_tls_unsupported'),
    ({'tls':'false'},'proxy_tls_unsupported'),
    ({'dialer-proxy':'another'},'proxy_options_unsupported'),
    ({'skip-cert-verify':True},'proxy_options_unsupported'),
])
def test_socks_connection_options_are_not_silently_discarded(extra,reason):
    nodes,rejected=parse_subscription(json.dumps({'proxies':[{'type':'socks5','server':'example.com','port':1080,**extra}]}))
    assert not nodes
    assert rejected==[{'index':0,'reason':reason}]


def test_http_tls_still_normalizes_to_https_without_losing_authentication():
    nodes,rejected=parse_subscription(json.dumps({'proxies':[{'type':'http','server':'example.com','port':443,'tls':True,'username':'example','password':'secret'}]}))
    assert not rejected
    assert nodes[0]['url']=='https://example:secret@example.com:443'
