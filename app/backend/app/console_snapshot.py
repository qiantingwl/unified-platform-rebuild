"""Light console bootstrap; list pages fetch their own bounded directories."""
from sqlalchemy import select,func,cast,Float
from .models import Job,Resource
from .store import PRIORITY,PLATFORMS,day_at
from .account_directory import directory_query
from .registration_queries import field


def snapshot(store,capacity):
    now=store.clock();day=day_at(now)
    with store.read() as (s,state):
        source=directory_query(store,s,now,include_balance=False).subquery()
        reasons=dict(s.execute(select(source.c.directory_status,func.count()).group_by(source.c.directory_status)).all())
        total=sum(reasons.values())
        today={k:{} for k in PRIORITY}
        for kind,status,count in s.execute(select(Job.kind,Job.status,func.count()).where(Job.business_day==day).group_by(Job.kind,Job.status)):
            if kind in today:today[kind][status]=count
        jobs=dict(s.execute(select(Job.status,func.count()).group_by(Job.status)).all())
        maintenance=s.scalar(select(func.count()).select_from(Job).where(Job.status=='running',Job.lease_until>now))
        registration=s.scalar(select(func.count()).select_from(Resource).where(Resource.kind=='execution_lease',cast(field(s,'expires_at'),Float)>now))
        registration_status=field(s,'status')
        counts=dict(s.execute(select(registration_status,func.count()).where(Resource.kind=='registration').group_by(registration_status)).all())
        after=dict(s.execute(select(Job.status,func.count()).where(Job.business_day==day,Job.kind=='balance',Job.dedupe_key.contains(':balance:after_checkin:',autoescape=True)).group_by(Job.status)).all())
        return dict(revision=state.revision,as_of=now,business_day=day,platforms=PLATFORMS,accounts=[],keys=[],events=[],requests=[],request_attempts=[],jobs=[],totals=dict(total=total,ready=reasons.get('ready',0),busy=reasons.get('busy',0),unavailable=total-reasons.get('ready',0)-reasons.get('busy',0)),reasons=reasons,job_counts=jobs,execution=dict(maintenance_active=maintenance,registration_active=registration,total_active=maintenance+registration,capacity=store.execution_capacity(s,capacity)),registration_counts=counts,today=today,after_checkin_balance=after,capabilities={p:dict(real_adapter=bool(store.real_capabilities.get(p)),live_verified=False) for p in PLATFORMS})

