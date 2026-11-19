"""App-owned mihomo process with stable, authenticated internal listeners.

Only normalized proxy material is accepted. Subscription configuration cannot
set rules, controllers, file paths or inbound listeners. Task restarts leave
this process and forwarding connections alone.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
from urllib.parse import quote

import httpx
from sqlalchemy import select

from .models import Resource
from .proxy_protocols import parse_node
from .store import DomainError, uid


def _row(session, kind, name):
    return session.scalar(select(Resource).where(Resource.kind == kind, Resource.name == name))


def reserve_nodes(store):
    """Persist immutable listener ports/auth; never reuse a removed node's port."""
    result = []
    with store.tx() as (s, state):
        slots = {r.name:r for r in s.scalars(select(Resource).where(Resource.kind == 'proxy_kernel_slot'))}
        used = {json.loads(r.data)['port'] for r in slots.values()}
        for source in s.scalars(select(Resource).where(Resource.kind == 'proxy_kernel_source').order_by(Resource.name)):
            node = s.get(Resource, source.name)
            if not node or not source.secret:
                continue
            data = json.loads(node.data)
            if data.get('status') in {'disabled','retired'}:
                continue
            raw = json.loads(store.cipher.decrypt(source.secret.encode()).decode())
            parsed = parse_node(raw)
            if parsed['identity'] != data.get('identity'):
                raise DomainError('kernel_material_mismatch', 409)
            slot = slots.get(node.id)
            if slot is None:
                port = next((p for p in range(20000,30000) if p not in used), None)
                if port is None:
                    raise DomainError('kernel_ports_exhausted', 503)
                slot = Resource(id=uid(),kind='proxy_kernel_slot',name=node.id,version=1,
                    data=json.dumps({'port':port}),updated_at=store.clock(),
                    secret=store.cipher.encrypt(secrets.token_urlsafe(32).encode()).decode())
                s.add(slot)
                slots[node.id]=slot
                used.add(port)
            result.append({'id':node.id,'version':node.version,'identity':parsed['identity'],
                'kernel':parsed['kernel'],'port':json.loads(slot.data)['port'],
                'password':store.cipher.decrypt(slot.secret.encode()).decode()})
    return result


def build_config(nodes, controller_secret, controller_port):
    proxies, listeners = [], []
    for node in nodes:
        tag = 'node-' + node['id']
        proxies.append(dict(node['kernel'],name=tag))
        listeners.append({'name':'in-'+node['id'],'type':'http','port':node['port'],
            'listen':'0.0.0.0','proxy':tag,'users':[{'username':'unified','password':node['password']}]})
    return {'mode':'rule','log-level':'silent','ipv6':False,'allow-lan':True,
        'external-controller':f'127.0.0.1:{controller_port}','secret':controller_secret,
        'listeners':listeners,'proxies':proxies,'rules':['MATCH,REJECT'],
        'dns':{'enable':False},'profile':{'store-selected':False}}


def owned_listener_ports(pid):
    """Linux production check: a foreign process's open port is not readiness."""
    sockets=set()
    for fd in Path(f'/proc/{pid}/fd').iterdir():
        try:
            target=os.readlink(fd)
        except FileNotFoundError:
            continue
        if target.startswith('socket:['):
            sockets.add(target[8:-1])
    ports=set()
    for protocol in ('tcp','tcp6'):
        for line in Path(f'/proc/{pid}/net/{protocol}').read_text().splitlines()[1:]:
            columns=line.split()
            if len(columns)>9 and columns[3]=='0A' and columns[9] in sockets:
                address,port=columns[1].split(':')
                ports.add((address,int(port,16)))
    return ports


def publish_nodes(store, nodes, endpoint_host):
    """Publish only the material/version which the live controller loaded."""
    with store.tx() as (s, state):
        changed = 0
        for item in nodes:
            row = s.get(Resource,item['id'])
            if not row or row.version != item['version']:
                continue
            data = json.loads(row.data)
            if data.get('identity') != item['identity'] or data.get('status') in {'disabled','retired'}:
                continue
            endpoint = f"http://unified:{quote(item['password'],safe='')}@{endpoint_host}:{item['port']}"
            old = store.cipher.decrypt(row.secret.encode()).decode() if row.secret else None
            if old != endpoint:
                row.secret = store.cipher.encrypt(endpoint.encode()).decode()
                row.version += 1
            if data.get('kernel_state') != 'ready' or data.get('status') == 'pending_kernel' or old != endpoint:
                data['kernel_state']='ready'
                if data.get('status') == 'pending_kernel':
                    data['status']='untested'
                row.data, row.updated_at = json.dumps(data),store.clock()
                changed += 1
        if changed:
            store.emit(s,state,'proxy','kernel_ready')
    return changed


class ProxyKernel:
    def __init__(self, store, *, enabled=False, binary=None, directory=None, endpoint_host=None, controller_port=19090):
        self.store,self.enabled=store,enabled
        self.binary=binary or os.getenv('UNIFIED_PROXY_KERNEL_BINARY','/usr/local/bin/mihomo')
        self.directory=Path(directory or os.getenv('UNIFIED_PROXY_KERNEL_DIR','/app/.local/proxy-kernel'))
        self.endpoint_host=endpoint_host or os.getenv('UNIFIED_PROXY_KERNEL_HOST','app')
        self.controller_port=controller_port
        self.secret=secrets.token_urlsafe(32)
        self.process=None
        self.loaded_hash=None
        self.running=False
        self.last_tick=None
        self.last_error=None
        self.nodes=0
        self.engine_version=None
        self.generation=0
        self.loaded_at=None
        self.listeners_ready=False
        self.checking=False
        self.last_verified_at=None

    @property
    def status(self):
        alive=self.process is not None and self.process.returncode is None
        running=self.running and alive
        return {'enabled':self.enabled,'running':running,'nodes':self.nodes,
                'last_tick':self.last_tick,'last_error':self.last_error,'engine':'mihomo',
                'version':self.engine_version,'generation':self.generation,'loaded_at':self.loaded_at,
                'listeners_ready':self.listeners_ready and running,
                'checking':self.checking,'last_verified_at':self.last_verified_at}

    def _write(self, config):
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=self.directory/'candidate.json'
        # Parent is private; explicit chmod also handles an old existing file.
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(descriptor,'w',encoding='utf8') as stream:
            json.dump(config,stream)
        os.chmod(path,0o600)
        return path

    async def _validate(self,path):
        proc=await asyncio.create_subprocess_exec(self.binary,'-t','-d',str(self.directory),'-f',str(path),
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        try:
            code=await asyncio.wait_for(proc.wait(),20)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if code:
            raise DomainError('kernel_config_rejected',503)

    async def _controller(self, method, path, payload=None):
        async with httpx.AsyncClient(trust_env=False,timeout=20) as client:
            response=await client.request(method,f'http://127.0.0.1:{self.controller_port}'+path,
                headers={'Authorization':'Bearer '+self.secret},json=payload)
            if response.status_code >= 300:
                raise DomainError('kernel_controller_failed',503)
            return response.json() if response.content else {}

    async def _verify_loaded(self,nodes):
        if self.process is None or self.process.returncode is not None:
            raise DomainError('kernel_not_running',503)
        actual=await self._controller('GET','/proxies')
        if any('node-'+n['id'] not in actual.get('proxies',{}) for n in nodes):
            raise DomainError('kernel_load_incomplete',503)
        owned=await asyncio.to_thread(owned_listener_ports,self.process.pid)
        required={('00000000',n['port']) for n in nodes} | {('0100007F',self.controller_port)}
        for node in nodes:
            if ('00000000',node['port']) not in owned and ('0'*32,node['port']) in owned:
                # Go can bind an IPv4 wildcard as dual-stack IPv6. Ownership
                # alone does not prove IPv4 clients can use the resulting port.
                _,writer=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',node['port']),2)
                writer.close()
                await writer.wait_closed()
                owned.add(('00000000',node['port']))
        if not required <= owned:
            raise DomainError('kernel_listener_not_owned',503)

    async def tick(self):
        self.last_tick=self.store.clock()
        if not self.enabled:
            self.listeners_ready=False
            return
        self.checking=True
        try:
            await self._tick()
        except BaseException:
            self.listeners_ready=False
            raise
        finally:
            self.checking=False

    async def _tick(self):
        # Keep the last verified state during a routine inspection. Clearing it
        # before reading the database makes a healthy process flicker as broken
        # every 15 seconds. A failed inspection still revokes readiness above.
        if not shutil.which(self.binary):
            raise DomainError('kernel_binary_missing',503)
        nodes=await asyncio.to_thread(reserve_nodes,self.store)
        config=build_config(nodes,self.secret,self.controller_port)
        digest=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
        alive=self.process is not None and self.process.returncode is None
        if alive and digest == self.loaded_hash:
            await self._verify_loaded(nodes)
            # A source refresh can update versions while the normalized config
            # stays identical; republish safely using this fresh snapshot.
            await asyncio.to_thread(publish_nodes,self.store,nodes,self.endpoint_host)
            self.running=True
            self.nodes=len(nodes)
            self.listeners_ready=True
            self.last_verified_at=self.store.clock()
            self.last_error=None
            return
        path=await asyncio.to_thread(self._write,config)
        await self._validate(path)
        self.listeners_ready=False
        if alive:
            # No force flag: identical listeners retain their live sockets.
            await self._controller('PUT','/configs',{'path':str(path.resolve())})
        else:
            self.running=False
            self.loaded_hash=None
            self.process=await asyncio.create_subprocess_exec(self.binary,'-d',str(self.directory),'-f',str(path),
                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            for attempt in range(40):
                if self.process.returncode is not None:
                    raise DomainError('kernel_start_failed',503)
                try:
                    version=await self._controller('GET','/version')
                    self.engine_version=str(version.get('version','unknown'))[:80]
                    break
                except (httpx.HTTPError,DomainError):
                    await asyncio.sleep(.25)
            else:
                raise DomainError('kernel_start_timeout',503)
        # A successful config API response can hide a bind failure. Confirm
        # every listener belongs to this exact child before publishing URLs.
        await self._verify_loaded(nodes)
        await asyncio.to_thread(publish_nodes,self.store,nodes,self.endpoint_host)
        self.loaded_hash=digest
        self.generation+=1
        self.loaded_at=self.store.clock()
        self.listeners_ready=True
        self.last_verified_at=self.store.clock()
        self.running=True
        self.nodes=len(nodes)
        self.last_error=None

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(),5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.running=False
        self.listeners_ready=False

    async def run(self):
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.last_error=error.code if isinstance(error,DomainError) else 'kernel_update_failed'
                    self.running=self.process is not None and self.process.returncode is None
                await asyncio.sleep(15)
        finally:
            await self.close()
