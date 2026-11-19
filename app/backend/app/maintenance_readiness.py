"""Avoid duplicating known local egress failures in automatic balance jobs.

This reads existing configuration and probe evidence only. It never changes an
account, probes an upstream, or delays manual/post-sign-in work.
"""
import json

from sqlalchemy import select

from .models import Resource
from .store import DomainError


DEPENDENCY_ERRORS={"proxy_pool_unavailable","proxy_pool_missing","missing_proxy",
                   "proxy_unavailable","proxy_retired","proxy_target_unverified",
                   "maintenance_proxy_route_invalid","proxy_route_invalid"}


class MaintenanceReadiness:
    def __init__(self,store,session):
        self.store,self.session=store,session
        kinds={"maintenance_proxy_route","proxy_binding","proxy_route","proxy",
               "proxy_pool","proxy_target_check","proxy_health"}
        rows=list(session.scalars(select(Resource).where(Resource.kind.in_(kinds))))
        self.by_name={(row.kind,row.name):row for row in rows}
        self.by_id={row.id:row for row in rows}
        self.checks={row.name:row for row in rows if row.kind=="proxy_target_check"}
        self.health={row.name:json.loads(row.data) for row in rows if row.kind=="proxy_health"}
        self.cached={}

    def ready(self,account,kind="balance"):
        from .maintenance_proxy import _decode
        try:
            route=_decode(self.by_name.get(("maintenance_proxy_route",f"{account.platform}:{kind}")))
            if route["mode"]=="inherit":
                binding=self.by_name.get(("proxy_binding",account.id))
                if binding:
                    route={"mode":"fixed","proxy_id":json.loads(binding.data).get("proxy_id")}
                else:
                    row=self.by_name.get(("proxy_route",account.platform))
                    route=json.loads(row.data) if row else {"mode":"direct"}
            key=(account.platform,route.get("mode"),route.get("proxy_id"),route.get("pool_id"))
            if key not in self.cached:
                self.cached[key]=self._ready_route(account.platform,route)
            return self.cached[key]
        except (DomainError,ValueError,TypeError,AttributeError):
            return False

    def _ready_route(self,platform,route):
        from .proxy_pool import _usable,_target_eligible,_pool_nodes,_dynamic_member,_fixed_egress_held
        mode=route.get("mode")
        if mode=="direct":
            return True
        if mode=="fixed":
            node=self.by_id.get(route.get("proxy_id"))
            return bool(_usable(node) and _target_eligible(self.store,self.session,node,platform,self.checks)
                        and not _fixed_egress_held(self.store,self.session,node,platform,self.health))
        if mode!="pool":
            return False
        pool=self.by_id.get(route.get("pool_id"))
        if not pool or pool.kind!="proxy_pool":
            return False
        held={item.get("proxy_id") for item in self.health.values() if item.get("platform")==platform
              and not item.get("model") and item.get("retry_at",0)>self.store.clock()}
        return any(_usable(node) and node.id not in held and
                   _target_eligible(self.store,self.session,node,platform,self.checks,force=_dynamic_member(pool,node))
                   for node in _pool_nodes(self.session,pool))
