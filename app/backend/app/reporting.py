"""Bounded SQL aggregates for operational traffic reports."""
from sqlalchemy import select,func,case,and_
from .models import RequestLog as R,Account,ApiKey
from .store import DomainError
from .account_directory import identity


def analyze(store,start,end,basis,platform=None,model=None,account_id=None,all_time=False,top_limit=20,rank_by='total_tokens',key_id=None,client_source=None):
    if end<=start or (not all_time and end-start>366*86400) or basis not in ('start','end') or rank_by not in ('total_tokens','total'):
        raise DomainError('invalid_time_window',422)
    stamp=R.started_at if basis=='start' else R.ended_at
    filters=[stamp>=start,stamp<end]
    for column,value in ((R.platform,platform),(R.model,model),(R.account_id,account_id),(R.key_id,key_id),(R.client_source,client_source)):
        if value: filters.append(column.is_(None) if value=="unknown" and column.key in ("key_id","client_source") else column==value)
    cached=and_(R.cached_tokens.is_not(None),R.input_tokens.is_not(None),R.cached_tokens>=0,R.cached_tokens<=R.input_tokens)
    fields=[func.count().label('total')]
    fields += [func.sum(case((R.status==status,1),else_=0)).label(status) for status in ('succeeded','failed','cancelled','inflight')]
    total_tokens=func.coalesce(R.total_tokens,R.input_tokens+R.output_tokens)
    fields += [func.sum(getattr(R,k)).label(k) for k in ('input_tokens','output_tokens')]
    fields += [func.sum(total_tokens).label('total_tokens')]
    fields += [func.sum(case((cached,1),else_=0)).label('cache_reported_requests'),func.sum(case((cached,R.cached_tokens),else_=None)).label('cached_tokens'),func.sum(case((cached,R.input_tokens),else_=None)).label('cache_input_tokens')]
    def aggregate(s,dimensions=()):
        stmt=select(*dimensions,*fields).where(*filters)
        if dimensions: stmt=stmt.group_by(*dimensions)
        return [dict(row._mapping) for row in s.execute(stmt)]
    def normalize(row):
        for k in ('succeeded','failed','cancelled','inflight','cache_reported_requests'): row[k]=row[k] or 0
        row['cache_missing_requests']=row['total']-row['cache_reported_requests']
        row['cache_ratio']=row['cached_tokens']/row['cache_input_tokens'] if row['cache_input_tokens'] else None
        return row
    with store.read() as (s,_):
        summary=normalize(aggregate(s)[0])
        groups=[]
        for row in aggregate(s,(R.platform,R.model)):
            normalize(row)
            row.update(statuses={k:row[k] for k in ('succeeded','failed','cancelled','inflight')},cache=dict(reported_requests=row['cache_reported_requests'],missing_requests=row['cache_missing_requests'],input_tokens=row['cache_input_tokens'],cached_tokens=row['cached_tokens'],ratio=row['cached_tokens']/row['cache_input_tokens'] if row['cache_input_tokens'] else None))
            errors=s.execute(select(R.error_code,func.count()).where(*filters,R.platform==row['platform'],R.model==row['model'],R.status.not_in(['succeeded','inflight'])).group_by(R.error_code)).all()
            row['errors']={k or 'unknown':v for k,v in errors}
            # SQL aggregates retain useful latency without loading request history.
            for name,column in (('duration',R.ended_at),('effective_output',R.first_content_at),('placeholder',R.placeholder_at)):
                n,average,maximum=s.execute(select(func.count(column),func.avg(column-R.started_at),func.max(column-R.started_at)).where(*filters,R.platform==row['platform'],R.model==row['model'])).one()
                row[name]=dict(count=n,mean=average,max=maximum,p50=None,p95=None)
            groups.append(row)
        import math
        bucket_seconds=3600 if end-start<=2*86400 else max(86400,math.ceil((end-start)/(400*86400))*86400)
        bucket=func.floor((stamp+28800)/bucket_seconds)*bucket_seconds-28800
        trend=[]
        for row in aggregate(s,(bucket.label('bucket'),)):
            normalize(row);row.update(start=row.pop('bucket'));row['end']=row['start']+bucket_seconds;trend.append(row)
        ranking=func.sum(total_tokens).desc().nulls_last() if rank_by=='total_tokens' else func.count().desc()
        top=s.execute(select(R.account_id,R.platform,func.count().label('total'),func.sum(total_tokens).label('total_tokens'),func.sum(case((R.status=='succeeded',1),else_=0)).label('succeeded')).where(*filters).group_by(R.account_id,R.platform).order_by(ranking,R.account_id).limit(top_limit)).all()
        accounts={a.id:a for a in s.scalars(select(Account).where(Account.id.in_([r.account_id for r in top])))}
        tops=[]
        for r in top:
            row=dict(r._mapping);a=accounts.get(r.account_id);safe=identity(a) if a else {};row.update(label=safe.get('display_name','已归档账户'),identity_hint=safe.get('identity_hint'));tops.append(row)
        distribution=lambda column:[dict(name=r[0],total=r[1],total_tokens=r[2]) for r in s.execute(select(column,func.count(),func.sum(total_tokens)).where(*filters).group_by(column).order_by(func.sum(total_tokens).desc().nulls_last(),column))]
        platforms,models=distribution(R.platform),distribution(R.model)
        day=func.floor((stamp+28800)/86400)
        summary['active_days']=s.scalar(select(func.count(func.distinct(day))).where(*filters))
        summary['top_model']=models[0] if models else None
        today=math.floor((store.clock()+28800)/86400)
        daily=select(day.label('day')).where(stamp.is_not(None),stamp<=store.clock(),*filters[2:]).group_by(day).subquery()
        numbered=select(daily.c.day,(func.row_number().over(order_by=daily.c.day.desc())).label('position')).subquery()
        latest=s.scalar(select(func.max(daily.c.day)))
        summary['current_streak']=s.scalar(select(func.count()).select_from(numbered).where(numbered.c.day+numbered.c.position==latest+1)) if latest is not None and latest>=today-1 else 0
        summary['range_days']=math.ceil((end-start)/86400)
        activity=aggregate(s,(day.label('day'),))
        for row in activity:
            normalize(row);row['start']=row['day']*86400-28800
        activity.sort(key=lambda r:r['day'])
        clients=[dict(r._mapping) for r in s.execute(select(R.client_source,*fields).where(*filters).group_by(R.client_source).order_by(func.sum(total_tokens).desc().nulls_last(),R.client_source).limit(top_limit))]
        for row in clients:
            normalize(row);row['name']=row.pop('client_source') or 'unknown'
        clients.sort(key=lambda r: (-(r['total_tokens'] or 0),r['name']))
        key_rows=[dict(r._mapping) for r in s.execute(select(R.key_id,*fields).where(*filters).group_by(R.key_id).order_by(func.sum(total_tokens).desc().nulls_last(),R.key_id).limit(top_limit))]
        key_names=dict(s.execute(select(ApiKey.id,ApiKey.name).where(ApiKey.id.in_([r['key_id'] for r in key_rows if r['key_id']]))).all())
        for row in key_rows:
            normalize(row);row['name']=key_names.get(row['key_id'],'已归档密钥' if row['key_id'] else '未记录密钥')
        key_rows.sort(key=lambda r: (-(r['total_tokens'] or 0),r['key_id'] or ''))
        windows=[]
        for hours in (1,6,24):
            row=dict(s.execute(select(*fields).where(*filters,stamp>=end-hours*3600)).one()._mapping)
            normalize(row)
            windows.append(dict(hours=hours,reported_requests=row['cache_reported_requests'],missing_requests=row['cache_missing_requests'],input_tokens=row['cache_input_tokens'],cached_tokens=row['cached_tokens'],ratio=row['cache_ratio']))
        return dict(activity=activity,cache_windows=windows,client_distribution=clients,key_distribution=key_rows,start=start,end=end,basis=basis,all_time=all_time,rank_by=rank_by,top_limit=top_limit,as_of=store.clock(),summary=summary,groups=groups,trend=sorted(trend,key=lambda r:r['start']),top_accounts=tops,platform_distribution=platforms,model_distribution=models,inflight_before_end=s.scalar(select(func.count()).select_from(R).where(R.status=='inflight',R.started_at<end)),end_window_excludes_inflight=basis=='end')


def sources(store,kind,start,end,basis='start',platform=None,model=None,account_id=None,key_id=None,client_source=None,page=1,page_size=25,sort='total_tokens'):
    if kind not in ('keys','clients') or end<=start or basis not in ('start','end') or sort not in ('total_tokens','total'):
        raise DomainError('invalid_report_query',422)
    stamp=R.started_at if basis=='start' else R.ended_at
    conditions=[stamp>=start,stamp<end]
    for column,value in ((R.platform,platform),(R.model,model),(R.account_id,account_id),(R.key_id,key_id),(R.client_source,client_source)):
        if value:conditions.append(column.is_(None) if value=="unknown" and column.key in ("key_id","client_source") else column==value)
    dimension=R.key_id if kind=='keys' else R.client_source
    cached=and_(R.cached_tokens.is_not(None),R.input_tokens.is_not(None),R.cached_tokens>=0,R.cached_tokens<=R.input_tokens)
    tokens=func.coalesce(R.total_tokens,R.input_tokens+R.output_tokens)
    fields=[dimension.label('source'),func.count().label('total'),func.max(stamp).label('last_used_at'),func.sum(tokens).label('total_tokens')]
    fields += [func.sum(case((R.status==status,1),else_=0)).label(status) for status in ('succeeded','failed','cancelled','inflight')]
    fields += [func.sum(getattr(R,k)).label(k) for k in ('input_tokens','output_tokens')]
    fields += [func.sum(case((cached,1),else_=0)).label('cache_reported_requests'),func.sum(case((cached,R.cached_tokens),else_=None)).label('cached_tokens'),func.sum(case((cached,R.input_tokens),else_=None)).label('cache_input_tokens')]
    grouped=select(*fields).where(*conditions).group_by(dimension).subquery()
    with store.read() as (session,_):
        total=session.scalar(select(func.count()).select_from(grouped))
        items=[dict(row._mapping) for row in session.execute(select(grouped).order_by(grouped.c[sort].desc().nulls_last(),grouped.c.source).offset((page-1)*page_size).limit(page_size))]
        names=dict(session.execute(select(ApiKey.id,ApiKey.name).where(ApiKey.id.in_([r['source'] for r in items if r['source']]))).all()) if kind=='keys' else {}
        for row in items:
            source=row.pop('source')
            row['cache_reported_requests']=row['cache_reported_requests'] or 0
            row['cache_missing_requests']=row['total']-row['cache_reported_requests']
            row['key_id' if kind=='keys' else 'client_source']=source
            row['name']=names.get(source,'已归档密钥' if source else '未记录密钥') if kind=='keys' else source or 'unknown'
        return dict(items=items,total=total,page=page,page_size=page_size)
