"""Strict subscription normalization. Connection material never enters error text."""
import base64
import hashlib
import ipaddress
import json
import re
import uuid

from .store import DomainError

KERNEL_PROTOCOLS = {'vmess','vless','ss','trojan','hysteria2','wireguard'}


def _fail(code='invalid_proxy_node'):
    raise DomainError(code, 422)


def _host(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        _fail()
    value = value.strip('[]').lower().rstrip('.')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value):
            _fail()
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in value.split('.')):
            _fail()
        return value


def _integer(value, low, high):
    try:
        if isinstance(value, bool) or str(int(value)) != str(value):
            _fail()
        result = int(value)
    except (TypeError, ValueError):
        _fail()
    if not low <= result <= high:
        _fail()
    return result


def parse_node(value):
    """Accept classic VMess share links or a Clash VMess mapping, never raw config."""
    if isinstance(value,dict) and value.get('type') == 'wireguard':
        from .proxy_wireguard import parse_wireguard
        return parse_wireguard(value)
    if isinstance(value,str) and not value.lower().startswith('vmess://'):
        from .proxy_extra_protocols import share_uri
        value=share_uri(value)
    if isinstance(value,dict) and value.get('type') in KERNEL_PROTOCOLS-{'vmess'}:
        from .proxy_extra_protocols import parse_extra
        return parse_extra(value)
    if isinstance(value, str):
        if not value.lower().startswith('vmess://') or len(value) > 32768:
            _fail('proxy_protocol_unsupported')
        try:
            raw = value[8:]
            obj = json.loads(base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True))
            if not isinstance(obj, dict):
                _fail()
        except (ValueError, UnicodeError):
            _fail()
        if obj.get('allowInsecure') not in (None, False, 0, '0', ''):
            _fail('proxy_insecure_tls_rejected')
        net = obj.get('net', 'tcp') or 'tcp'
        if obj.get('type') not in (None, '', 'none'):
            _fail('proxy_transport_unsupported')
        tls = obj.get('tls', '')
        if tls not in ('', 'none', 'tls', None):
            _fail('proxy_tls_unsupported')
        value = {'type': 'vmess', 'name': obj.get('ps', 'VMess'), 'server': obj.get('add'),
                 'port': obj.get('port'), 'uuid': obj.get('id'), 'alterId': obj.get('aid', 0),
                 'cipher': obj.get('scy') or 'auto', 'network': net, 'tls': tls == 'tls'}
        if obj.get('sni'):
            value['servername'] = obj['sni']
        if obj.get('alpn'):
            value['alpn'] = obj['alpn'].split(',') if isinstance(obj['alpn'], str) else obj['alpn']
        if obj.get('fp'):
            value['client-fingerprint'] = obj['fp']
        if net == 'ws':
            value['ws-opts'] = {'path': obj.get('path') or '/', 'headers': {'Host': obj['host']} if obj.get('host') else {}}
        elif net == 'grpc':
            value['grpc-opts'] = {'grpc-service-name': obj.get('path') or ''}
    if not isinstance(value, dict) or value.get('type') != 'vmess':
        _fail('proxy_protocol_unsupported')
    allowed = {'type','name','server','port','uuid','alterId','cipher','network','tls','servername','alpn',
               'client-fingerprint','skip-cert-verify','udp','ws-opts','grpc-opts'}
    if set(value) - allowed:
        _fail('proxy_options_unsupported')
    if value.get('skip-cert-verify') not in (None, False):
        _fail('proxy_insecure_tls_rejected')
    if not isinstance(value.get('tls', False), bool) or not isinstance(value.get('udp', False), bool):
        _fail()
    try:
        identity = str(uuid.UUID(str(value.get('uuid'))))
    except (ValueError, AttributeError):
        _fail()
    network = value.get('network') or 'tcp'
    if network not in {'tcp', 'ws', 'grpc'}:
        _fail('proxy_transport_unsupported')
    cipher = value.get('cipher') or 'auto'
    if cipher not in {'auto','aes-128-gcm','chacha20-poly1305','none','zero'}:
        _fail('proxy_cipher_unsupported')
    kernel = {'type':'vmess','server':_host(value.get('server')), 'port':_integer(value.get('port'),1,65535),
              'uuid':identity,'alterId':_integer(value.get('alterId',0),0,65535),'cipher':cipher,
              'network':network,'tls':value.get('tls',False),'udp':value.get('udp',False)}
    if value.get('servername'):
        kernel['servername'] = _host(value['servername'])
    if value.get('alpn'):
        alpn = value['alpn']
        if not isinstance(alpn,list) or not alpn or any(x not in {'h2','http/1.1'} for x in alpn):
            _fail('proxy_tls_unsupported')
        kernel['alpn'] = alpn
    if value.get('client-fingerprint'):
        fp = value['client-fingerprint']
        if fp not in {'chrome','firefox','safari','ios','android','edge','360','qq','random','randomized'}:
            _fail('proxy_tls_unsupported')
        kernel['client-fingerprint'] = fp
    if network == 'ws':
        opts = value.get('ws-opts', {})
        if not isinstance(opts,dict) or set(opts) - {'path','headers','max-early-data','early-data-header-name'}:
            _fail('proxy_transport_unsupported')
        path = opts.get('path') or '/'
        headers = opts.get('headers') or {}
        if not isinstance(path,str) or not path.startswith('/') or len(path)>4096 or any(c in path for c in '\r\n'):
            _fail()
        if not isinstance(headers,dict) or any(k.lower() != 'host' or not isinstance(v,str) or len(v)>253 or any(c in v for c in '\r\n') for k,v in headers.items()):
            _fail('proxy_transport_unsupported')
        ws = {'path':path,'headers':{'Host':v for k,v in headers.items()}}
        if 'max-early-data' in opts:
            ws['max-early-data'] = _integer(opts['max-early-data'],0,65535)
        if 'early-data-header-name' in opts:
            if opts['early-data-header-name'] != 'Sec-WebSocket-Protocol':
                _fail('proxy_transport_unsupported')
            ws['early-data-header-name'] = opts['early-data-header-name']
        kernel['ws-opts'] = ws
    elif network == 'grpc':
        opts = value.get('grpc-opts', {})
        if not isinstance(opts,dict) or set(opts) - {'grpc-service-name'}:
            _fail('proxy_transport_unsupported')
        service = opts.get('grpc-service-name','')
        if not isinstance(service,str) or len(service)>256 or any(c in service for c in '\r\n'):
            _fail()
        kernel['grpc-opts'] = {'grpc-service-name':service}
    if (network != 'ws' and value.get('ws-opts')) or (network != 'grpc' and value.get('grpc-opts')):
        _fail('proxy_transport_unsupported')
    digest = hashlib.sha256(json.dumps(kernel,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'name':str(value.get('name') or 'VMess')[:100], 'scheme':'vmess', 'identity':digest, 'kernel':kernel}
