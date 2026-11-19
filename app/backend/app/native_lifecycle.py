"""Native credential lifecycle evidence for the account and its current version."""
import json
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from .models import Resource
from .store import uid


def control(session,account):
    row=session.scalar(select(Resource).where(Resource.kind=='lifecycle_control',Resource.name==account.id))
    if row:
        value=json.loads(row.data)
        if value.get('credential_version')!=account.credential_version:
            return {**value,'state':'verification_required','reason':'credential_version_changed'}
        return value
    return dict(state='verification_required', credential_version=account.credential_version,
                session_origin='unknown', reason='session_evidence_required', version=0,
                material_completeness=None, independence_evidence_kind=None,
                independence_evidence_at=None)


def record_native_login(store,session,account,mode,credential,previous_version=None):
    """Record a verified newly issued session, never an imported token.

    Exact modes come from login_connectors.SUPPORTED. Identity verification is
    completed by registration before this hook. Equal session/refresh material
    is evidence of reuse, not proof of an independent session.
    """
    from .models import Credential
    from .login_material import requirements
    modes = {
        "Loomy": {"sms", "password", "register"},
        "WorkBuddy 国内": {"sms", "official"},
        "WorkBuddy 国际": {"official"},
        "WPS 灵犀": {"browser"},
        "小浣熊": {"official"},
        "CodeArts": {"official", "register"},
        "Marvis": {"oauth_qq", "oauth_wechat"},
    }
    independent = mode in modes.get(account.platform, set())
    def session_keys(value):
        auth = value.get("auth") if isinstance(value.get("auth"), dict) else {}
        keys = ("session", "cookie", "refresh_token", "refreshToken", "access_token",
                "accessToken", "token", "security_token", "securityToken")
        result = set()
        for source in (value, auth):
            for key in keys:
                raw = source.get(key)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                raw = raw.strip()
                if key == "cookie" and account.platform == "WPS 灵犀":
                    # The surrounding cookies are unrelated to WPS SSO.
                    result.update(part.strip().partition("=")[2] for part in raw.split(";")
                                  if part.strip().startswith("wps_sid=") and part.strip()[8:])
                elif key in {"access_token", "accessToken", "token"} and raw.lower().startswith("bearer "):
                    if raw[7:].strip():
                        result.add(raw[7:].strip())
                else:
                    result.add(raw)
        return result

    reason = None if independent else "independent_session_required"
    current_keys = session_keys(credential) if independent else set()
    if independent and not current_keys:
        independent = False
        reason = "session_evidence_required"
    if independent and previous_version:
        old = session.scalar(select(Credential).where(Credential.account_id==account.id,Credential.version==previous_version))
        try:
            previous = json.loads(store.cipher.decrypt(old.ciphertext.encode())) if old else None
        except (InvalidToken, ValueError, TypeError):
            previous = None
        previous_keys = session_keys(previous) if isinstance(previous, dict) else set()
        if not previous_keys:
            independent = False
            reason = "previous_session_evidence_unavailable"
        elif previous_keys & current_keys:
            # A changed access token or unrelated cookie cannot prove independence.
            independent = False
            reason = "official_login_reused_session_material"
    material = requirements(account.platform,credential,credential)
    row=session.scalar(select(Resource).where(Resource.kind=='lifecycle_control',Resource.name==account.id))
    if row is None:
        row=Resource(id=uid(),kind='lifecycle_control',name=account.id,data='{}',version=0,updated_at=store.clock())
        session.add(row)
    row.version+=1
    row.updated_at=store.clock()
    value=dict(state='independent' if independent else 'verification_required',credential_version=account.credential_version,
        session_origin='official_login' if mode in modes.get(account.platform,set()) else 'imported',
        independence_evidence_kind='new_verified_official_session' if independent else None,
        independence_evidence_at=store.clock() if independent else None,
        material_completeness={k:material[k] for k in ('can_refresh','missing_material','material_guidance')},
        reason=reason,version=row.version)
    row.data=json.dumps(value)
    return value
