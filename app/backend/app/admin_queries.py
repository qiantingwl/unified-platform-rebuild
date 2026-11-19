"""Bounded dashboard and system event queries."""
from fastapi import Query
from sqlalchemy import select,func,case,or_
from .models import Event,Account
from .account_directory import directory_query,identity
from .store import day_at


def mount_admin_queries(app,store):
    @app.get('/api/overview')
    def overview():
        now=store.clock()
        with store.read() as (s,_):
            source=directory_query(store,s,now).subquery()
            columns=[source.c.platform,func.count().label('total')]
            columns += [func.sum(case((source.c.directory_status==k,1),else_=0)).label(k) for k in ('ready','busy')]
            columns += [func.sum(case((source.c.balance_state==k,1),else_=0)).label(k) for k in ('verified','stale','unknown','failed')]
            columns += [func.count(source.c.balance).label('known'),func.sum(source.c.balance).label('knownSum'),func.sum(case((source.c.balance_state=='verified',source.c.balance),else_=None)).label('sum')]
            rows=[]
            for r in s.execute(select(*columns).group_by(source.c.platform)):
                row=dict(r._mapping);row['unavailable']=row['total']-row['ready']-row['busy'];rows.append(row)
            return dict(platform_summaries=rows,as_of=now,business_day=day_at(now))

    @app.get('/api/system-events')
    def events(page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=200),q:str='',component:str='',level:str='',account_id:str='',start:float|None=None,end:float|None=None):
        with store.read() as (s,_):
            filters=[]
            if q:filters.append(or_(Event.code.icontains(q,autoescape=True),Event.kind.icontains(q,autoescape=True)))
            if component:filters.append(Event.kind==component)
            if account_id:filters.append(Event.account_id==account_id)
            if start is not None:filters.append(Event.created_at>=start)
            if end is not None:filters.append(Event.created_at<end)
            # The event model does not record a severity; do not invent it.
            if level and level!='all':filters.append(Event.id<0)
            total=s.scalar(select(func.count()).select_from(Event).where(*filters))
            records=list(s.scalars(select(Event).where(*filters).order_by(Event.id.desc()).offset((page-1)*page_size).limit(page_size)))
            accounts={a.id:a for a in s.scalars(select(Account).where(Account.id.in_([r.account_id for r in records if r.account_id])))}
            items=[]
            for r in records:
                row={k:getattr(r,k) for k in ('id','kind','code','created_at','revision','account_id','job_id','request_id')}
                row.update(component=r.kind,level=None,account_display_name=identity(accounts[r.account_id])['display_name'] if r.account_id in accounts else None);items.append(row)
            return dict(items=items,total=total,page=page,page_size=page_size,as_of=store.clock(),severity_available=False)
