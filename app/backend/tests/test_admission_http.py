import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, func

from app.dispatch_policy import PolicyInput, save
from app.main import create_app
from app.models import Lease, RequestLog
from app.store import Store
from test_gateway import FakeRegistry, delta, real_accounts


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'http-wait.db'}", Fernet.generate_key())
    token = real_accounts(store, 1)
    save(store, 'Loomy', PolicyInput(expected_version=0, strategy='lru', queue_wait_seconds=1))
    held = store.acquire(token, 'model')
    registry = FakeRegistry([[delta('answer'), {'_done':True}]])
    app = create_app(store=store, registry=registry, start_worker=False, admin_token='synthetic-admin-token', role='api')
    yield store, token, held, registry, app
    store.engine.dispose()


def test_http_entry_wait_cancellation_does_not_dispatch_or_take_slot(env):
    store, token, held, registry, app = env

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            request = asyncio.create_task(client.post('/v1/chat/completions',
                headers={'Authorization':'Bearer '+token},
                json={'model':'model','messages':[{'role':'user','content':'synthetic'}],'stream':True}))
            await asyncio.sleep(.05)
            assert not request.done()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

    asyncio.run(run())
    assert registry.calls == []
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Lease)) == 1
        assert s.scalar(select(func.count()).select_from(RequestLog)) == 1
        assert s.get(RequestLog, held['id']).status == 'inflight'


@pytest.mark.parametrize('placeholder', [True, False])
def test_http_entry_queue_keeps_configured_placeholder_and_effective_answer(env, placeholder):
    store, token, held, registry, app = env
    app.state.gateway.placeholder_enabled = placeholder

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            request = asyncio.create_task(client.post('/v1/chat/completions',
                headers={'Authorization':'Bearer '+token},
                json={'model':'model','messages':[{'role':'user','content':'synthetic'}],'stream':True}))
            await asyncio.sleep(.05)
            assert not request.done()
            await asyncio.to_thread(store.end_request, held['id'], 'cancelled', 'cancelled')
            app.state.gateway.slots_changed()
            response = await request
            assert response.status_code == 200
            assert 'content-encoding' not in response.headers
            frames = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith('data: ') and line != 'data: [DONE]']
            content = [choice.get('delta',{}).get('content') for frame in frames for choice in frame.get('choices',[])]
            assert content[0] == (' ' if placeholder else 'answer')
            assert 'answer' in content and response.text.endswith('data: [DONE]\n\n')

    asyncio.run(run())
    with store.read() as (s, _):
        assert s.scalar(select(func.count()).select_from(Lease)) == 0
        completed = s.scalar(select(RequestLog).where(RequestLog.id != held['id']))
        assert completed.status == 'succeeded' and completed.first_content_at is not None
        assert (completed.placeholder_at is not None) is placeholder
