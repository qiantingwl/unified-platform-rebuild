"""Native WireGuard outbound subset, checked against Mihomo v1.19.34.

Accept explicit node configuration only. No registration, key generation,
host routing, TUN devices, or implicit reuse of another service's settings.
"""
import base64
import hashlib
import ipaddress
import json

from .proxy_protocols import _fail, _host, _integer


def _key(value):
    try:
        if not isinstance(value,str):
            _fail()
        raw=base64.b64decode(value,validate=True)
        if len(raw)!=32 or not any(raw):
            _fail()
        return base64.b64encode(raw).decode()
    except (ValueError,TypeError):
        _fail()


def _address(value,version=None,network=False):
    try:
        if not isinstance(value,str):
            _fail()
        address=ipaddress.ip_network(value,strict=False) if network else ipaddress.ip_interface(value)
        if version and address.version!=version:
            _fail()
        return str(address)
    except ValueError:
        _fail()


PEER_FIELDS={'server','port','public-key','pre-shared-key','reserved','allowed-ips'}


def _peer(value,require_routes=False):
    if not isinstance(value,dict) or set(value)-PEER_FIELDS:
        _fail('proxy_options_unsupported')
    result={'server':_host(value.get('server')),'port':_integer(value.get('port'),1,65535),
            'public-key':_key(value.get('public-key'))}
    if 'pre-shared-key' in value:
        result['pre-shared-key']=_key(value['pre-shared-key'])
    if 'reserved' in value:
        reserved=value['reserved']
        if not isinstance(reserved,list) or len(reserved)!=3:
            _fail()
        result['reserved']=[_integer(x,0,255) for x in reserved]
    if 'allowed-ips' in value:
        routes=value['allowed-ips']
        if not isinstance(routes,list) or not 1<=len(routes)<=128:
            _fail()
        result['allowed-ips']=sorted(set(_address(x,network=True) for x in routes))
    elif require_routes:
        _fail('wireguard_routes_required')
    return result


def parse_wireguard(value):
    allowed={'type','name','ip','ipv6','private-key','mtu','udp','persistent-keepalive',
             'peers','remote-dns-resolve','dns'}|PEER_FIELDS
    if set(value)-allowed:
        _fail('proxy_options_unsupported')
    kernel={'type':'wireguard','private-key':_key(value.get('private-key'))}
    for field,version in [('ip',4),('ipv6',6)]:
        if field in value:
            kernel[field]=_address(value[field],version)
    if 'ip' not in kernel and 'ipv6' not in kernel:
        _fail('wireguard_address_required')
    for field in ('udp','remote-dns-resolve'):
        if field in value:
            if type(value[field]) is not bool:
                _fail()
            kernel[field]=value[field]
    for field,low,high in [('mtu',576,9000),('persistent-keepalive',0,65535)]:
        if field in value:
            kernel[field]=_integer(value[field],low,high)
    if 'peers' in value:
        peers=value['peers']
        if set(value)&PEER_FIELDS or not isinstance(peers,list) or not 1<=len(peers)<=32:
            _fail('proxy_options_unsupported')
        kernel['peers']=[_peer(peer,require_routes=True) for peer in peers]
    else:
        # Mihomo's flat single-peer IPC path installs default routes and does
        # not consume a top-level allowed-ips. Require peers for custom routes.
        if 'allowed-ips' in value:
            _fail('wireguard_routes_require_peers')
        kernel.update(_peer({key:value[key] for key in PEER_FIELDS if key in value}))
    if 'dns' in value:
        dns=value['dns']
        if not isinstance(dns,list) or not 1<=len(dns)<=8:
            _fail()
        try:
            kernel['dns']=[str(ipaddress.ip_address(x)) for x in dns if isinstance(x,str)]
        except ValueError:
            _fail('proxy_options_unsupported')
        if len(kernel['dns'])!=len(dns):
            _fail()
    if kernel.get('remote-dns-resolve') and not kernel.get('dns'):
        _fail('wireguard_dns_required')
    digest=hashlib.sha256(json.dumps(kernel,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'name':str(value.get('name') or 'WireGuard')[:100],'scheme':'wireguard','identity':digest,'kernel':kernel}
