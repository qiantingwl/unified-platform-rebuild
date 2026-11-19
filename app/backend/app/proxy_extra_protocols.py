"""Strict recommended-source protocols; only normalized outbound keys survive."""
import base64
import hashlib
import json
import re
from urllib.parse import urlsplit, unquote, parse_qsl


def share_uri(uri):
    from .proxy_protocols import _fail
    def decode(value):
        try:
            return base64.b64decode(value+'='*(-len(value)%4),altchars=b'-_',validate=True).decode()
        except (ValueError,UnicodeError):
            _fail()
    if len(uri)>32768:
        _fail()
    try:
        parsed=urlsplit(uri)
        kind='hysteria2' if parsed.scheme=='hy2' else parsed.scheme
        if kind not in {'ss','trojan','vless','hysteria2'}:
            _fail('proxy_protocol_unsupported')
        pairs=parse_qsl(parsed.query,keep_blank_values=True)
        if len(dict(pairs))!=len(pairs):
            _fail('proxy_options_unsupported')
        query=dict(pairs)
        allowed={'sni','peer','security','type','host','path','serviceName','fp','alpn',
                 'allowInsecure','insecure','encryption','flow','obfs','obfs-password'}
        if set(query)-allowed:
            _fail('proxy_options_unsupported')
        if any(query.get(k,'0').lower() not in {'0','false',''} for k in ('allowInsecure','insecure')):
            _fail('proxy_insecure_tls_rejected')
        if kind=='ss' and '@' not in parsed.netloc:
            parsed=urlsplit('ss://'+decode(uri[5:].split('#',1)[0]))
        if not parsed.hostname or not parsed.port or '@' not in parsed.netloc:
            _fail()
        userinfo=unquote(parsed.netloc.rsplit('@',1)[0])
        node={'type':kind,'name':unquote(urlsplit(uri).fragment) or kind.upper(),
              'server':parsed.hostname,'port':parsed.port}
        if kind=='ss':
            if query:_fail('proxy_options_unsupported')
            if ':' not in userinfo:userinfo=decode(userinfo)
            method,password=userinfo.split(':',1)
            node.update(cipher=method,password=password)
        elif kind in {'trojan','hysteria2'}:
            node['password']=userinfo
        else:
            node['uuid']=userinfo
            if query.get('encryption') not in (None,'','none'):_fail('proxy_cipher_unsupported')
            if query.get('security') not in (None,'','none','tls'):_fail('proxy_tls_unsupported')
            node['tls']=query.get('security')=='tls'
            if query.get('flow'):_fail('proxy_transport_unsupported')
        if kind in {'trojan','hysteria2'} and query.get('security') not in (None,'','tls'):
            _fail('proxy_tls_unsupported')
        sni=query.get('sni') or query.get('peer')
        if sni:node['sni' if kind in {'trojan','hysteria2'} else 'servername']=sni
        if query.get('alpn'):node['alpn']=query['alpn'].split(',')
        if query.get('fp'):node['client-fingerprint']=query['fp']
        if kind=='hysteria2':
            if query.get('type') not in (None,'','udp'):_fail('proxy_transport_unsupported')
            if query.get('obfs'):node['obfs']=query['obfs']
            if query.get('obfs-password'):node['obfs-password']=query['obfs-password']
        elif kind!='ss':
            network=query.get('type') or 'tcp'
            node['network']=network
            if network=='ws':node['ws-opts']={'path':query.get('path') or '/','headers':{'Host':query['host']} if query.get('host') else {}}
            elif network=='grpc':node['grpc-opts']={'grpc-service-name':query.get('serviceName') or ''}
        return node
    except (ValueError,UnicodeError):
        _fail()


def _obfs_host(value, mode):
    from .proxy_protocols import _host
    # Mihomo sends this simple-obfs TLS field verbatim in its synthetic
    # ClientHello. It is distinct from the dial address and upstream TLS SNI.
    if mode == 'tls' and isinstance(value, str):
        match = re.fullmatch(r'([A-Za-z0-9.-]{1,253}):[0-9]{6}', value)
        if match:
            _host(match[1])
            return value
    return _host(value)


def parse_extra(value):
    from .proxy_protocols import _fail, _host, _integer, parse_node
    kind=value['type']
    common={'type','name','server','port','udp'}
    tls={'sni','servername','skip-cert-verify','alpn','client-fingerprint'}
    transport={'network','ws-opts','grpc-opts'}
    allowed={'ss':common|{'cipher','password','plugin','plugin-opts','udp-over-tcp','udp-over-tcp-version','client-fingerprint'},'trojan':common|tls|transport|{'password'},
        'hysteria2':common|tls|{'password','obfs','obfs-password'},'vless':common|tls|transport|{'uuid','tls'}}[kind]
    if set(value)-allowed:_fail('proxy_options_unsupported')
    if value.get('skip-cert-verify') not in (None,False):_fail('proxy_insecure_tls_rejected')
    if not isinstance(value.get('udp',False),bool):_fail()
    kernel={'type':kind,'server':_host(value.get('server')),'port':_integer(value.get('port'),1,65535),'udp':value.get('udp',False)}
    if kind=='ss':
        cipher=value.get('cipher')
        if cipher not in {'aes-128-gcm','aes-256-gcm','chacha20-ietf-poly1305','2022-blake3-aes-128-gcm','2022-blake3-aes-256-gcm','2022-blake3-chacha20-poly1305'}:
            _fail('proxy_cipher_unsupported')
        kernel['cipher']=cipher
        # Only Mihomo's built-in simple-obfs contract is accepted here.
        # Unknown plugins/options remain a hard error instead of disappearing.
        if 'plugin' in value or 'plugin-opts' in value:
            opts=value.get('plugin-opts')
            if value.get('plugin')!='obfs' or not isinstance(opts,dict) or set(opts)-{'mode','host'}:
                _fail('proxy_options_unsupported')
            if opts.get('mode') not in ('http','tls'):
                _fail('proxy_transport_unsupported')
            normalized={'mode':opts['mode']}
            if 'host' in opts:normalized['host']=_obfs_host(opts['host'],opts['mode'])
            kernel.update(plugin='obfs',**{'plugin-opts':normalized})
        if 'udp-over-tcp' in value:
            if type(value['udp-over-tcp']) is not bool:_fail()
            kernel['udp-over-tcp']=value['udp-over-tcp']
        if 'udp-over-tcp-version' in value:
            if value.get('udp-over-tcp') is not True:_fail('proxy_options_unsupported')
            kernel['udp-over-tcp-version']=_integer(value['udp-over-tcp-version'],1,2)
        if 'client-fingerprint' in value:
            fp=value['client-fingerprint']
            if not isinstance(fp,str) or fp not in {'chrome','firefox','safari','ios','android','edge','360','qq','random','randomized'}:
                _fail('proxy_tls_unsupported')
            kernel['client-fingerprint']=fp
    if kind in {'ss','trojan','hysteria2'}:
        password=value.get('password')
        if not isinstance(password,str) or not password or len(password)>2048 or '\x00' in password:_fail()
        kernel['password']=password
    if kind in {'vless','trojan'}:
        normalized_input={k:v for k,v in value.items() if k in transport|{'alpn','client-fingerprint'}}
        normalized_input.update(type='vmess',server=kernel['server'],port=kernel['port'],
            uuid=value.get('uuid') if kind=='vless' else '00000000-0000-0000-0000-000000000000',
            tls=value.get('tls',False) if kind=='vless' else True)
        if value.get('sni') or value.get('servername'):normalized_input['servername']=value.get('sni') or value.get('servername')
        normalized=parse_node(normalized_input)['kernel']
        for key in ('network','ws-opts','grpc-opts','alpn','client-fingerprint'):
            if key in normalized:kernel[key]=normalized[key]
        if 'servername' in normalized:kernel['sni' if kind=='trojan' else 'servername']=normalized['servername']
        if kind=='vless':kernel.update(uuid=normalized['uuid'],tls=normalized['tls'])
    elif kind=='hysteria2':
        if value.get('sni') or value.get('servername'):kernel['sni']=_host(value.get('sni') or value['servername'])
        if value.get('obfs'):
            if value['obfs']!='salamander' or not isinstance(value.get('obfs-password'),str) or not value['obfs-password'] or len(value['obfs-password'])>2048:_fail('proxy_transport_unsupported')
            kernel.update(obfs='salamander',**{'obfs-password':value['obfs-password']})
        elif value.get('obfs-password'):_fail('proxy_transport_unsupported')
        if value.get('alpn') or value.get('client-fingerprint'):_fail('proxy_options_unsupported')
    digest=hashlib.sha256(json.dumps(kernel,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'name':str(value.get('name') or kind.upper())[:100],'scheme':kind,'identity':digest,'kernel':kernel}
