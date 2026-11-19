import json

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.adapters import Registry
from app.main import create_app
from app.store import Store


def test_new_console_routes_are_authenticated_and_preflight_is_read_only(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'console.db'}", Fernet.generate_key())
    app = create_app(store=store, registry=Registry(enabled=True), admin_token='synthetic-admin-token', role='api')
    client = TestClient(app)
    headers = {'X-Console-Client': 'unified-local'}
    try:
        assert client.get('/api/dispatch-policies').status_code == 401
        assert client.post('/api/registration/preflight',headers=headers,json={'platform':'Loomy','mode':'register'}).status_code == 401
        assert client.post('/api/session',headers=headers,json={'token':'synthetic-admin-token'}).status_code == 200
        revision = store.revision()
        response = client.post('/api/registration/preflight',headers=headers,json={'platform':'Loomy','mode':'register'})
        assert response.status_code == 200, response.text
        assert response.json()['read_only'] and not response.json()['registration_verified']
        assert store.revision() == revision
        assert client.put('/api/dispatch-policies/Loomy',headers=headers,json={'expected_version':0,'strategy':'balance_low'}).status_code == 200
        result=client.get('/api/dispatch-policies').json()
        assert next(p for p in result['policies'] if p['platform']=='Loomy')['strategy']=='balance_low'
        aid=store.create_account('Loomy','synthetic-account','synthetic')
        store.operation_ledger(aid,'onboarding',{'settled':False,'tasks':[{'key':'test','title':'synthetic','status':'uncertain','secret':'must-not-leak'}]})
        result=client.get(f'/api/accounts/{aid}/onboarding')
        assert result.status_code==200, result.text
        assert result.json()['readback_supported']
        assert 'must-not-leak' not in result.text
        assert result.json()['tasks'][0]['status']=='uncertain'
    finally:
        client.close()
        store.engine.dispose()
