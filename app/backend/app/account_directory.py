"""Database-backed account directory; no credential payloads or snapshot scans."""
from sqlalchemy import select, func, case, and_, or_, literal, union_all, cast
from fastapi import Query
from sqlalchemy.orm import aliased
from collections import Counter
from .models import Account, Lease, ModelState, Job, Event, ApiKey
from .store import TEMPORARY_MODEL_BLOCKS, day_at


def identity(a):
    from .native_identity import safe_label
    value = a.external_id
    hint = value[:2] + "…" + value[-3:] if len(value) > 6 else "***" + value[-2:]
    internal = any(word in a.label for word in ("迁入验收", "副本验收", "线上测试", "沙盒"))
    original = safe_label(a.original_label) or (None if internal else safe_label(a.label))
    return dict(original_label=original, display_name=original or f"{a.platform} · {hint}", identity_hint=hint, external_id_hint=hint, source_label=None)


def directory_query(store, s, now):
    busy = select(Lease.account_id.label('aid'), func.count().label('busy')).where(Lease.expires_at > now).group_by(Lease.account_id).subquery()
    # Seek the newest completed balance for this account and credential.
    # The old window ranked the entire history, even for a single account lookup.
    last = aliased(Job, name='directory_latest_balance')
    balance_job = (select(Job.id)
        .where(Job.account_id == Account.id, Job.kind == 'balance',
               Job.credential_version == Account.credential_version,
               Job.status.in_(['succeeded', 'failed']), Job.finished_at.is_not(None))
        .order_by(Job.finished_at.desc(), Job.finished_revision.desc().nulls_last(),
                  Job.created_at.desc(), Job.id.desc())
        .limit(1).correlate(Account).scalar_subquery())
    count = func.coalesce(busy.c.busy, 0)
    supported = [p for p,c in store.real_capabilities.items() if 'chat' in c]
    from .key_policy import read as key_policy
    from .dispatch_policy import route_scope
    from .store import DomainError
    options=[];seen=set();member_queries={}
    for key in s.scalars(select(ApiKey).where(ApiKey.enabled.is_(True),ApiKey.revoked_at.is_(None))):
        policy=key_policy(s,key)
        if policy['expires_at'] is not None and policy['expires_at']<=now: continue
        for model in policy['models']:
            if model=='sandbox-echo': continue
            try: scope=route_scope(s,key.platform,key.id,model,model)
            except DomainError: continue
            members=tuple(sorted(set(scope['account_ids']))) if scope['account_ids'] is not None else None
            signature=(key.platform,scope['upstream_model'],members)
            if signature in seen:continue
            seen.add(signature)
            eligible=and_(Account.platform==key.platform,Account.sandbox.is_(False))
            if members is not None:
                if members not in member_queries:
                    import json
                    value=literal(json.dumps(members))
                    if s.get_bind().dialect.name=='postgresql':
                        from sqlalchemy.dialects.postgresql import JSONB
                        expanded=func.jsonb_array_elements_text(cast(value,JSONB)).table_valued('value')
                    else:expanded=func.json_each(value).table_valued('value')
                    member_queries[members]=select(expanded.c.value)
                eligible=and_(eligible,Account.id.in_(member_queries[members]))
            options.append(select(Account.id.label('aid'),literal(scope['upstream_model']).label('model')).where(eligible))
    if not options:
        options=[select(Account.id.label('aid'),literal('').label('model')).where(Account.id.is_(None))]
    if len(options)>400:
        chunks=[union_all(*options[i:i+400]).cte('directory_route_chunk_'+str(i)) for i in range(0,len(options),400)]
        options=[select(chunk.c.aid,chunk.c.model) for chunk in chunks]
    available=(union_all(*options) if len(options)>1 else options[0]).cte('directory_model_routes')
    blocked=and_(ModelState.account_id==available.c.aid,ModelState.verified_at.is_(None),
        or_(ModelState.reason=='quota',ModelState.model==available.c.model,ModelState.model=='*'),
        or_(and_(ModelState.model != '*', ModelState.reason.not_in(sorted(TEMPORARY_MODEL_BLOCKS))),ModelState.review_at>now))
    readiness=select(available.c.aid,
        func.max(case((ModelState.id.is_(None),1),else_=0)).label('usable'),
        func.coalesce(func.max(case((ModelState.reason=='quota','quota'),else_=None)),func.min(ModelState.reason)).label('block')
        ).select_from(available).outerjoin(ModelState,blocked).group_by(available.c.aid).subquery()
    route=or_(Account.sandbox.is_(True),readiness.c.aid.is_not(None))
    usable_route=or_(Account.sandbox.is_(True),readiness.c.usable==1)
    block=readiness.c.block
    ready=case((count>=Account.concurrency,'busy'),else_='ready')
    routed=case((usable_route,ready),(route,block if block is not None else 'no_route'),else_='no_route')
    no_checkin=[p for p,c in store.real_capabilities.items() if 'checkin' not in c]
    status=case((~Account.enabled,'manually_disabled'),(and_(~Account.sandbox,Account.platform.not_in(supported)),'adapter_unavailable'),(Account.credential_status=='missing','missing_credential'),(Account.credential_status=='invalid','needs_relogin'),(Account.credential_status=='disabled','upstream_disabled'),(Account.credential_status=='suspect','credential_review'),(Account.credential_status!='valid','credential_pending'),(or_(Account.balance.is_(None),Account.balance_version!=Account.credential_version,Account.balance_at.is_(None)),'balance_pending'),(Account.balance<=0,case((and_(~Account.sandbox,Account.platform.in_(no_checkin)),'no_balance'),(Account.checkin_day==day_at(now),'balance_exhausted'),else_='checkin_pending')),else_=routed).label('directory_status')
    balance = case((last.status == 'failed','failed'),(Account.balance.is_(None),'unknown'),(or_(Account.balance_version != Account.credential_version, Account.balance_at.is_(None),Account.balance_at < now-3600),'stale'),else_='verified').label('balance_state')
    return select(Account, count.label('busy'),status,balance,last.error_code.label('balance_error')).outerjoin(readiness,readiness.c.aid==Account.id).outerjoin(busy,busy.c.aid==Account.id).outerjoin(last,last.id==balance_job)


def serialize(row):
    a=row[0]
    result={name:getattr(a,name) for name in ('id','platform','enabled','sandbox','credential_status','credential_version','balance','balance_at','balance_version','checkin_day','relogin_day','relogin_failures','concurrency','priority','balance_interval_seconds','daily_checkin_enabled','daily_checkin_minute','created_at','updated_at')}
    result.update(identity(a)); result['label']=result['display_name']
    result.update(busy=row.busy,status=row.directory_status,balance_state=row.balance_state,balance_error=row.balance_error,model_readiness={},model_blocks=[])
    return result


def mount_account_directory(app,store):
    @app.get('/api/accounts')
    def accounts(page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=200),q:str='',platform:str='',status:str='',balance_state:str='',sort:str='default',capability:str=''):
        now=store.clock()
        with store.read() as (s,_):
            stmt=directory_query(store,s,now)
            if platform: stmt=stmt.where(Account.platform==platform)
            if q: stmt=stmt.where(or_(Account.label.icontains(q,autoescape=True),Account.original_label.icontains(q,autoescape=True),Account.external_id.icontains(q,autoescape=True),Account.id.icontains(q,autoescape=True)))
            if capability:
                allowed=[p for p,c in store.real_capabilities.items() if capability in c]
                stmt=stmt.where(Account.platform.in_(allowed))
            if balance_state and balance_state != 'all': stmt=stmt.where(stmt.selected_columns.balance_state==balance_state)
            base=stmt.subquery()
            aggregates=[dict(row._mapping) for row in s.execute(select(base.c.directory_status,base.c.balance_state,
                func.count().label('total'),func.sum(base.c.balance).label('balance_sum'),func.count(base.c.balance).label('known')
                ).group_by(base.c.directory_status,base.c.balance_state))]
            counts=Counter()
            for group in aggregates:counts[group['directory_status']]+=group['total']
            if status and status not in ('all','全部状态'):
                stmt=stmt.where(stmt.selected_columns.directory_status==status)
                selected=[group for group in aggregates if group['directory_status']==status]
            else:selected=aggregates
            total=sum(group['total'] for group in selected)
            states=Counter();status_totals=Counter()
            for group in selected:
                states[group['balance_state']]+=group['total'];status_totals[group['directory_status']]+=group['total']
            known=sum(group['known'] for group in selected)
            observed=[group['balance_sum'] for group in selected if group['balance_sum'] is not None]
            verified=[group['balance_sum'] for group in selected if group['balance_state']=='verified' and group['balance_sum'] is not None]
            sums=(sum(observed) if observed else None,sum(verified) if verified else None,known)
            order={'high':Account.balance.desc().nulls_last(),'low':Account.balance.asc().nulls_last(),'checked':Account.balance_at.asc().nulls_last()}.get(sort,Account.created_at.asc())
            actual_page = min(page, max(1, (total + page_size - 1) // page_size))
            rows=s.execute(stmt.order_by(order,Account.id).offset((actual_page-1)*page_size).limit(page_size)).all()
            summary=dict(total=total,ready=status_totals.get('ready',0),busy=status_totals.get('busy',0),unavailable=total-status_totals.get('ready',0)-status_totals.get('busy',0),known=sums[2],knownSum=sums[0],sum=sums[1],**{k:states.get(k,0) for k in ('verified','stale','unknown','failed')})
            return dict(items=[serialize(r) for r in rows],total=total,page=actual_page,page_size=page_size,summary=summary,status_counts=counts,as_of=now,business_day=day_at(now))

    @app.get('/api/accounts/{account_id}')
    def detail(account_id:str):
        with store.read() as (s,_):
            store.account(s,account_id)
            now = store.clock()
            result = serialize(s.execute(directory_query(store,s,now).where(Account.id==account_id)).one())
            result['model_blocks'] = [dict(model=b.model,scope='account' if b.reason=='quota' or b.model=='*' else 'model',reason=b.reason,review_at=b.review_at,verified_at=b.verified_at,attempts=b.attempts) for b in s.scalars(select(ModelState).where(ModelState.account_id==account_id,ModelState.verified_at.is_(None))) if store.block_active(b, now)]
            return result

    @app.get('/api/accounts/{account_id}/events')
    def events(account_id:str,page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=200)):
        with store.read() as (s,_):
            store.account(s,account_id)
            where=Event.account_id==account_id
            rows=s.scalars(select(Event).where(where).order_by(Event.id.desc()).offset((page-1)*page_size).limit(page_size))
            return dict(items=[{k:getattr(r,k) for k in ('id','revision','kind','code','account_id','job_id','created_at')} for r in rows],total=s.scalar(select(func.count()).select_from(Event).where(where)),page=page,page_size=page_size,as_of=store.clock())


def substitute_balance(store,s,now):
    return directory_query(store,s,now).selected_columns.balance_state

