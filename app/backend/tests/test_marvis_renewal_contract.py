"""Documented official renewal shapes only. No network or real credentials."""
import asyncio
import json

import pytest

from app.adapters import Marvis
from app.upstream import UpstreamError


class StaticTransport:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def json(self, method, url, headers, body):
        self.requests.append((method, json.loads(body)))
        return self.response


def renew(response, monkeypatch, **changes):
    credential = {"openid": "fixture-subject", "access_token": "fixture-access",
                  "refresh_token": "fixture-refresh", "guid": "fixture-guid",
                  "login_type": "2", "expires_at": 1, **changes}
    transport = StaticTransport(response)
    monkeypatch.setattr("app.adapters.time.time", lambda: 1700000000)
    result = asyncio.run(Marvis(transport, credential, "fixture-local-id").renew())
    return result, transport.requests


def test_success_omitted_openid_keeps_authenticated_subject_and_string_seconds(monkeypatch):
    result, requests = renew({"code": 0, "user_info": {"access_token": "fixture-access", "refresh_token": "", "expires_in": "7200"}}, monkeypatch)
    assert result["openid"] == "fixture-subject"
    assert result["refresh_token"] == "fixture-refresh"
    assert result["expires_in"] == 7200
    assert result["expires_at"] == 1700007200
    assert requests == [("POST", {"userInfo": {"openId": "fixture-subject", "accessToken": "fixture-access", "refreshToken": "fixture-refresh", "loginType": "Marvis"}})]


def test_camelcase_success_contract(monkeypatch):
    result, _ = renew({"code": "0", "userInfo": {"accessToken": "fixture-new-access", "refreshToken": "fixture-new-refresh", "openId": "fixture-subject", "expiresIn": 3600}}, monkeypatch)
    assert result["access_token"] == "fixture-new-access"
    assert result["refresh_token"] == "fixture-new-refresh"
    assert result["expires_at"] == 1700003600


@pytest.mark.parametrize("identity", [{"openid": "another-subject"}, {"openId": "another-subject"}, {"openid": "fixture-subject", "openId": "another-subject"}])
def test_nonempty_different_identity_remains_rejected(monkeypatch, identity):
    with pytest.raises(UpstreamError, match="identity_mismatch"):
        renew({"code": 0, "user_info": {"access_token": "fixture-new-access", **identity}}, monkeypatch)


def test_missing_identity_without_explicit_success_is_not_trusted(monkeypatch):
    with pytest.raises(UpstreamError, match="identity_verifier_unavailable"):
        renew({"user_info": {"access_token": "fixture-new-access"}}, monkeypatch)


@pytest.mark.parametrize("field,value,seconds", [("expires_in", "7200", 7200), ("expiresIn", 120, 120), ("access_token_expire_time", "900", 900), ("expires_in", 1700007200, 0), ("expires_in", "invalid", 0), ("expires_in", True, 0), ("expires_in", "Infinity", 0)])
def test_expiry_units_do_not_turn_absolute_timestamp_into_duration(monkeypatch, field, value, seconds):
    result, _ = renew({"code": 0, "user_info": {"access_token": "fixture-access", field: value}}, monkeypatch)
    assert result["expires_in"] == seconds
    assert result["expires_at"] == (1700000000 + seconds if seconds else 0)


@pytest.mark.parametrize("missing", ["openid", "access_token", "refresh_token"])
def test_no_authenticated_refresh_without_original_material(monkeypatch, missing):
    with pytest.raises(UpstreamError, match="missing_material"):
        renew({"code": 0, "user_info": {"access_token": "fixture-access"}}, monkeypatch, **{missing: ""})


@pytest.mark.parametrize("code,expected,code_status", [
    (-107, "invalid_credentials", 401), (-101, "invalid_credentials", 401),
    ("-114", "invalid_credentials", 401), (-109, "request_rejected", 400),
    (-7, "request_rejected", 400), (-5, "request_rejected", 400),
])
def test_refresh_error_code_keeps_auth_failure_and_request_error_separate(monkeypatch, code, expected, code_status):
    with pytest.raises(UpstreamError, match=expected) as error:
        renew({"code": code, "user_info": {"access_token": "fixture-access"}}, monkeypatch)
    assert error.value.status == code_status
    assert not error.value.retryable
