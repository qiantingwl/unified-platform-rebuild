"""Explain platform-specific material completeness without exposing secrets."""
import base64
import json


def requirements(platform, credential, material):
    def mapping(value):
        return value if isinstance(value, dict) else {}
    refresh = bool(credential.get("refresh_token") or credential.get("refreshToken") or
                   mapping(credential.get("auth")).get("refreshToken"))
    missing = []
    reason = None
    if platform == "CodeArts":
        context = mapping(credential.get("oauth_context"))
        verifier = credential.get("pkce_code_verifier") or mapping(context.get("pkce_pair")).get("code_verifier")
        private = credential.get("dpop_private_key") or mapping(context.get("dpop_key_pair")).get("private_key_jwk")
        for key, present in (("refresh_token", refresh), ("pkce_code_verifier", verifier), ("dpop_private_key", private)):
            if not present:
                missing.append(key)
        refresh = not missing
        reason = "official_authorization_recreates_pkce_dpop" if missing else None
    elif platform == "小浣熊":
        token = credential.get("access_token") or credential.get("accessToken") or credential.get("token") or ""
        try:
            part = str(token).removeprefix("Bearer ").split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part+"="*(-len(part)%4)))
            iam = str(claims.get("iss", "")).endswith("/iam")
        except (ValueError, IndexError, TypeError, AttributeError):
            iam = False
        if iam:
            refresh = False
            reason = "iam_requires_official_authorization"
        elif not refresh:
            missing.append("refresh_token")
    elif platform == "WPS 灵犀":
        refresh = False
        reason = "official_sso_required_balance_is_not_renewal"
    elif platform == "Loomy":
        if not material.get("phone"):
            missing.append("original_phone")
        if not material.get("password") and not material.get("sms_channel_id"):
            missing.append("password_or_sms_channel")
    elif platform.startswith("WorkBuddy") and not refresh:
        missing.append("refresh_token")
        reason = "official_authorization_or_original_number" if platform.endswith("国内") else "official_international_authorization"
    return {"can_refresh": refresh, "missing_material": missing, "material_guidance": reason,
            "original_number_required": platform in {"Loomy", "WorkBuddy 国内"},
            "independent_session_verified": False}
