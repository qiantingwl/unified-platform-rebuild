import asyncio
import pytest
from cryptography.fernet import Fernet
from app.store import Store
from app.worker import MaintenanceWorker
from test_gateway import real_accounts,FakeRegistry,delta


@pytest.mark.parametrize('flow,code',[
    ([delta('answer')],'truncated_stream'),
    ([{'_done':True}],'no_effective_output'),
    ([delta('answer'), {'choices':[{'finish_reason':'stop'}]}, {'error':{'type':'server_error'}}], 'upstream_5xx'),
    ([delta('answer'), {'_done':True,'error':{'code':'ModelPermissionDenied'}}], 'permission'),
    ([{'choices':[{'delta':{'content':'partial'},'finish_reason':'content_filter'}]}], 'content_filter'),
])
def test_model_review_requires_completed_text_and_no_stream_error(tmp_path,flow,code):
    store=Store(f'sqlite:///{tmp_path/"worker.db"}',Fernet.generate_key())
    try:
        real_accounts(store,1)
        store.real_capabilities['Loomy'].add('verify')
        aid=store.snapshot()['accounts'][0]['id']
        # Clear prior credential admission jobs for this focused verification.
        from app.models import Job
        from sqlalchemy import delete
        with store.tx() as (db,_):db.execute(delete(Job))
        jid=store.plan(aid,'verify','model')
        job=store.claim('test-owner',1)
        assert job['id']==jid
        job.pop('sandbox')  # Missing flag must never produce synthetic success.
        registry=FakeRegistry([flow])
        asyncio.run(MaintenanceWorker(store,registry=registry).execute(job))
        observed=next(j for j in store.snapshot()['jobs'] if j['id']==jid)
        assert observed['status']=='failed' and observed['error_code']==code
        assert registry.calls==[aid]
    finally:store.engine.dispose()
