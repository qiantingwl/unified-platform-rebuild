"""CodeArts official PKCE + DPoP login, retaining renewal context."""
import base64
import hashlib
import secrets
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .adapters import encode, required
from .upstream import UpstreamError, payload
from .vendor.mirai.codearts import signer

TOKEN_URL = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
IDENTITY_URL = "https://sts.cn-north-4.myhuaweicloud.com/v5/caller-identity"


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def proof(context):
    pair = context["dpop_key_pair"]
    private, public = pair["private_key_jwk"], pair["public_key_jwk"]
    scalar = int.from_bytes(base64.urlsafe_b64decode(private["d"] + "=" * (-len(private["d"]) % 4)), "big")
    key = ec.derive_private_key(scalar, ec.SECP256R1())
    head = {"alg": "ES256", "typ": "dpop+jwt", "jwk": public}
    claims = {"htm": "POST", "htu": TOKEN_URL, "iat": int(time.time()), "jti": uuid4().hex}
    message = b64(encode(head)) + "." + b64(encode(claims))
    r, s = decode_dss_signature(key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
    return message + "." + b64(r.to_bytes(32,"big") + s.to_bytes(32,"big"))


async def identity(transport, credential):
    headers = {"Content-Type": "application/json", "X-Security-Token": required(credential, "security_token")}
    headers = signer.sign("GET", IDENTITY_URL, headers, b"", required(credential, "access_key", "access_key_id"), required(credential, "secret_key", "secret_access_key"))
    observed = payload(await transport.json("GET", IDENTITY_URL, headers))
    required(observed, "account_id")
    required(observed, "principal_id")
    return {"account_id": observed["account_id"], "principal_id": observed["principal_id"]}


class CodeArtsLogin:
    def __init__(self, transport):
        self.t = transport

    async def start(self, material, mode):
        key = ec.generate_private_key(ec.SECP256R1()).private_numbers()
        public = {"kty": "EC", "crv": "P-256", "x": b64(key.public_numbers.x.to_bytes(32,"big")), "y": b64(key.public_numbers.y.to_bytes(32,"big"))}
        verifier, state = secrets.token_urlsafe(48), secrets.token_hex(24)
        context = {"pkce_pair": {"code_verifier": verifier, "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "SHA-256"}, "dpop_key_pair": {"public_key_jwk": public, "private_key_jwk": {**public,"d": b64(key.private_value.to_bytes(32,"big"))}}}
        callback = "http://127.0.0.1:4180/oauth/callback"
        params = {"theme": "2", "locale": "zh-cn", "uri_scheme": "vscode-codebot", "client_id": "vscode-codebot", "port": "4180", "code_challenge": context["pkce_pair"]["code_challenge"], "code_challenge_method": "SHA-256", "ticket_id": state, "auth_callback_url": callback, "plugin-name": "CodeArts", "plugin-version": "1.0.0"}
        authorization_url="https://codearts.huaweicloud.com/portal/authorize?" + urlencode(params)
        result={"waiting": "waiting_authorization", "state": state, "oauth_context": context, "callback_url": callback, "authorization_url":authorization_url}
        if mode=="register":
            result.update(codearts_oauth_url=authorization_url,authorization_url="https://reg.huaweicloud.com/registerui/cn/register.html",codearts_registration_pending=True)
        return result

    async def verify(self, material, mode, code):
        credential = material.get("issued_credential")
        if credential is None:
            url = urlsplit(code or "")
            expected = urlsplit(material["callback_url"])
            if (url.scheme,url.netloc,url.path) != (expected.scheme,expected.netloc,expected.path):
                raise UpstreamError("invalid_oauth_callback",422)
            query = parse_qs(url.query)
            state = query.get("state", query.get("ticket_id"))
            if state is not None and state != [material["state"]]:
                raise UpstreamError("oauth_state_mismatch",422)
            if query.get("secret") and query.get("redirect") and not query.get("code"):
                redirect = query["redirect"][0]
                parsed = urlsplit(redirect)
                if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".huaweicloud.com") or parsed.username:
                    raise UpstreamError("invalid_oauth_callback",422)
                # Continue the official authorization in the same browser. The
                # ticket-only fallback cannot renew, so do not label it complete.
                return {"waiting":"waiting_authorization", "authorization_url": redirect}
            if len(query.get("code", [])) != 1:
                raise UpstreamError("invalid_oauth_callback",422)
            context = material["oauth_context"]
            form = {"client_id":"vscode-codebot", "code":query["code"][0], "code_verifier":context["pkce_pair"]["code_verifier"], "grant_type":"authorization_code", "redirect_uri":material["callback_url"]}
            data = payload(await self.t.json("POST", TOKEN_URL, {"Content-Type":"application/x-www-form-urlencoded", "DPoP":proof(context)}, urlencode(form).encode()))
            issued = data.get("credentials") or {}
            credential = {"access_key":required(issued,"access_key_id"), "secret_key":required(issued,"secret_access_key"), "security_token":required(issued,"security_token"), "refresh_token":required(data,"refresh_token"), "oauth_context":context, "expires_at":datetime.fromisoformat(required(issued,"expiration").replace("Z","+00:00")).timestamp()}
            await self.checkpoint({"issued_credential":credential})
        verified = await identity(self.t, credential)
        return {"identity":str(verified["account_id"]) + ":" + str(verified["principal_id"]), "credential":{**credential,"verified_identity":verified}}
