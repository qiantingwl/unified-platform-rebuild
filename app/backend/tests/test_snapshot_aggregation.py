from collections import Counter
from cryptography.fernet import Fernet
from sqlalchemy import insert

from app.models import Job
from app.store import Store, day_at
from test_routing_large_population import measured


def test_full_day_counts_do_not_materialize_full_job_history(tmp_path):
    now = 1794920000.0
    store = Store(f"sqlite:///{tmp_path/'aggregation.db'}", Fernet.generate_key(), clock=lambda: now)
    try:
        aid = store.create_account('Loomy', 'synthetic', 'synthetic')
        day = day_at(now)
        rows = []
        for i in range(5000):
            kind = 'balance' if i % 2 == 0 else 'checkin'
            rows.append({'id': f'job-{i}', 'dedupe_key': f'{i}:balance:after_checkin:x' if i % 4 == 0 else f'job-{i}',
                'account_id': aid, 'kind': kind, 'business_day': day if i < 4000 else '2000-01-01',
                'credential_version': 0, 'status': ['succeeded','failed','queued'][i%3],
                'priority': 10, 'created_at': now-i, 'available_at': now,
                'finished_at': now-i if i%3!=2 else None, 'error_code': 'network' if i%3==1 else None})
        with store.tx() as (s, _): s.execute(insert(Job), rows)
        for i in range(120): store.create_key(f'synthetic-{i}', 'Loomy', 'model')
        with measured(store, 'snapshot-history-aggregate') as cost:
            snapshot = store.snapshot()
        for kind in ('balance','checkin'):
            assert snapshot['today'][kind] == dict(Counter(r['status'] for r in rows if r['business_day']==day and r['kind']==kind))
        assert snapshot['after_checkin_balance'] == dict(Counter(r['status'] for r in rows if r['business_day']==day and ':balance:after_checkin:' in r['dedupe_key']))
        assert snapshot['job_counts'] == dict(Counter(r['status'] for r in rows))
        assert len(snapshot['jobs']) == 200
        assert cost['Job_loaded'] <= 200
        assert cost['sql_calls'] < 30  # independent of 120 keys and 5000 jobs
        assert len(snapshot['keys']) == 120
        assert all(key['policy']['models'] == ['model'] for key in snapshot['keys'])
    finally:
        store.engine.dispose()
