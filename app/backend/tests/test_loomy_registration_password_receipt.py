"""r18: port 7866's register-save-password-login sequence without losing accounts."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.models import Resource
from app.registration import LoomyLogin, RegistrationService
from app.store import Store
from app.upstream import UpstreamError


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr('app.registration.signing.rsa_encrypt', lambda *_: 'synthetic-encrypted')
    adapter = LoomyLogin(None)
    saved, calls = {}, []
    async def checkpoint(patch):
        saved.update(json.loads(json.dumps(patch)))
    async def explained(path, params, device):
        calls.append(path)
        if path.endswith('/checkCode'):
            raise UpstreamError('loomy_not_registered', 400)
        return {'userid': 'synthetic-id', 'session': 'registered-session'}
    async def call(path, params, device):
        calls.append(path)
        assert saved['issued_credential']['session'] == 'registered-session'
        assert saved['issued_credential']['password_verified'] is False
        if path.endswith('/getPuKey'):
            return {'pukey': 'synthetic', 'rcode': 'synthetic'}
        return {'userid': 'synthetic-id', 'session': 'password-session'}
    adapter.checkpoint, adapter.call_explained, adapter.call = checkpoint, explained, call
    material = {'phone': '13800000001', 'challenge': 'synthetic', 'password': 'synthetic-password', 'device': 'synthetic-device'}
    return adapter, saved, calls, material


def test_password_is_verified_only_after_registration_receipt_saved(adapter):
    login, saved, calls, material = adapter
    result = asyncio.run(login.verify(material, 'register', '123456'))
    assert result['credential']['password_verified'] is True
    assert result['credential']['session'] == 'password-session'
    assert result['credential']['password_verified_at'] > 0
    assert saved['issued_credential'] == result['credential']
    assert calls == ['/login/phone/checkCode', '/register/phone/submit', '/login/account/getPuKey', '/login/account/byPwd']


def test_password_readback_failure_preserves_issued_registration(adapter):
    login, saved, _, material = adapter
    async def unavailable(*args):
        raise UpstreamError('network', 502)
    login.call = unavailable
    result = asyncio.run(login.verify(material, 'register', '123456'))
    assert result['credential']['session'] == 'registered-session'
    assert result['credential']['password_verified'] is False
    assert result['credential']['password_verification_error'] == 'network'
    assert saved['issued_credential'] == result['credential']


def test_existing_number_does_not_get_generated_password_or_password_probe(adapter):
    login, _, calls, material = adapter
    async def existing(*args):
        return {'userid': 'existing-id', 'session': 'existing-session'}
    login.call_explained = existing
    result = asyncio.run(login.verify(material, 'register', '123456'))
    assert 'password' not in result['credential']
    assert 'password_verified' not in result['credential']
    assert calls == []


def test_cancelled_password_probe_resumes_without_replaying_sms_or_registration(adapter):
    login, saved, calls, material = adapter
    good_call = login.call
    async def cancelled(*args):
        raise asyncio.CancelledError()
    login.call = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(login.verify(material, 'register', '123456'))
    assert saved['issued_credential']['session'] == 'registered-session'
    login.call = good_call
    result = asyncio.run(login.verify({**material, **saved}, 'register', None))
    assert result['credential']['password_verified']
    assert calls.count('/register/phone/submit') == 1
    assert calls.count('/login/phone/checkCode') == 1


def test_identity_mismatch_stays_unresolved_then_resumes_in_registration_service(adapter, tmp_path):
    login, _, calls, material = adapter
    async def mismatched(path, params, device):
        if path.endswith('/getPuKey'):
            return {'pukey': 'synthetic', 'rcode': 'synthetic'}
        return {'userid': 'other-id', 'session': 'other-session'}
    login.call = mismatched
    class Transport:
        async def close(self):
            pass
    store = Store(f"sqlite:///{tmp_path/'receipt.db'}", Fernet.generate_key())
    service = RegistrationService(store, SimpleNamespace(enabled=True, transport_factory=lambda **_: Transport()), factory=lambda _: login)
    sid = service.create('Loomy', 'synthetic', 'register', material)
    with store.tx() as (s, _):
        row = s.get(Resource, sid)
        data = json.loads(row.data)
        data['status'] = 'waiting_code'
        data['expires_at'] = store.clock() + 300
        row.data = json.dumps(data)
    try:
        result = asyncio.run(service.run(sid, code='123456'))
        assert result['status'] == 'failed' and result['error'] == 'identity_mismatch'
        assert result['result_unresolved'] is True
        private = service.details(sid)[1]
        assert private['issued_credential']['session'] == 'registered-session'
        assert private['issued_credential']['password_verified'] is False
        async def correct(path, params, device):
            if path.endswith('/getPuKey'):
                return {'pukey': 'synthetic', 'rcode': 'synthetic'}
            return {'userid': 'synthetic-id', 'session': 'password-session'}
        login.call = correct
        final = asyncio.run(service.run(sid, continuation=True))
        assert final['status'] == 'succeeded'
        assert final['result_unresolved'] is False
        assert calls.count('/register/phone/submit') == 1
        assert calls.count('/login/phone/checkCode') == 1
        markers = store.resources('onboarding_pending')
        assert len(markers) == 1
        assert markers[0]['name'] == final['account_id']
        assert markers[0]['data'] == {'status': 'pending', 'source': 'registration', 'session_id': sid}
    finally:
        store.engine.dispose()


@pytest.mark.parametrize('mode,relogin', [('verified_import', False), ('official', True)])
def test_import_or_relogin_does_not_schedule_new_user_rewards(tmp_path, mode, relogin):
    store = Store(f"sqlite:///{tmp_path/'onboarding.db'}", Fernet.generate_key())
    service = RegistrationService(store, SimpleNamespace(enabled=True))
    aid = store.create_account('小浣熊', 'synthetic-id', 'synthetic') if relogin else None
    sid = service.create('小浣熊', 'synthetic', mode, {'credential': {'access_token': 'synthetic'}}, account_id=aid)
    token = store.acquire_execution(sid, 64)
    try:
        data, _ = service.transition(sid, 'prepared', 'sending', token)
        result = service.finish(sid, data['operation'], result={'identity': 'synthetic-id', 'credential': {'access_token': 'synthetic'}})
        assert result['status'] == 'succeeded'
        assert store.resources('onboarding_pending') == []
    finally:
        store.release_execution(sid, token)
        store.engine.dispose()
