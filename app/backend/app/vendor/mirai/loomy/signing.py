from __future__ import annotations

import base64

import hashlib

import hmac

import json

import logging

import os

import urllib.error

import urllib.request

import uuid

from datetime import datetime, timezone

from urllib.parse import quote

logger = logging.getLogger("admin.platform.loomy.xfyun")

ACCOUNT_BASE = os.getenv("LOOMY_XFYUN_BASE_URL", "https://account.xfinfr.com")

ACCESS_KEY_ID = os.getenv("LOOMY_XFYUN_ACCESS_KEY_ID", "2thryby66wxi53sk")

ACCESS_KEY_SECRET = os.getenv("LOOMY_XFYUN_ACCESS_KEY_SECRET",
                              "zsak6eadrbawz683wf5r3m2snrwj868r")

APP_ID = os.getenv("LOOMY_XFYUN_APP_ID", "GM3LOOMY")

DEFAULT_DEVID = "web"

DEVICE_UA = "Loomy|Desktop|Electron|macOS"

MODEL_ID = "Web"

CLIENT_VERSION = "1.0.0"

SESSION_EXPIRE_SECONDS = 14 * 24 * 3600

BIZ_OK = "000000"

AUTH_ERROR_CODES = frozenset({"020002", "100002"})

def _escape(value: str) -> str:
    return (quote(str(value), safe="~")
            .replace("!", "%21").replace("'", "%27")
            .replace("(", "%28").replace(")", "%29").replace("*", "%2A"))

def _escaped_path(path: str) -> str:
    clean = path if path.startswith("/") else "/" + path
    if len(clean) > 1 and clean.endswith("/"):
        clean = clean[:-1]
    return "/".join(_escape(seg) if seg else "" for seg in clean.split("/"))

def sign_headers(method: str, path: str, body: str,
                 content_type: str = "application/json") -> dict:
    """生成一次请求的签名头（Date/Nonce 每次都要新）。"""
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    nonce = str(uuid.uuid4())
    md5 = (base64.b64encode(hashlib.md5(body.encode()).digest()).decode()
           if body else "")
    parts = [method.upper(), _escaped_path(path), "", md5, content_type,
             date, nonce, "", ""]
    digest = hmac.new(ACCESS_KEY_SECRET.encode(),
                      "\n".join(parts).encode(), hashlib.sha1).digest()
    headers = {
        "Authorization": "account %s:%s" % (
            ACCESS_KEY_ID, base64.b64encode(digest).decode()),
        "Date": date,
        "Nonce": nonce,
        "Content-Type": content_type,
    }
    if md5:
        headers["Content-MD5"] = md5
    return headers

def _base(devid: str = "") -> dict:
    return {
        "appid": APP_ID,
        "modelid": MODEL_ID,
        "version": CLIENT_VERSION,
        "devid": devid or DEFAULT_DEVID,
        "ua": DEVICE_UA,
        "traceid": uuid.uuid4().hex,
    }

def der_to_numbers(der: bytes):
    """从 DER SubjectPublicKeyInfo 取 `(n, e)`。"""
    tag, pos, _ = _der_read(der, 0)
    if tag != 0x30:
        raise ValueError("not a DER SEQUENCE")
    tag, pos, alg_len = _der_read(der, pos)
    if tag != 0x30:
        raise ValueError("missing AlgorithmIdentifier")
    pos += alg_len
    tag, body, _length = _der_read(der, pos)
    if tag != 0x03:
        raise ValueError("missing BIT STRING")
    if der[body] != 0x00:
        raise ValueError("unexpected unused bits")
    pos = body + 1
    tag, pos, _ = _der_read(der, pos)
    if tag != 0x30:
        raise ValueError("missing RSAPublicKey")
    values = []
    for _ in range(2):
        tag, body, length = _der_read(der, pos)
        if tag != 0x02:
            raise ValueError("expected INTEGER")
        values.append(int.from_bytes(der[body:body + length], "big"))
        pos = body + length
    return values[0], values[1]

def rsa_encrypt(pubkey_b64: str, plaintext: str) -> str:
    """RSA/PKCS#1 v1.5 加密（客户端就是这么传密码的）。"""
    der = base64.b64decode(pubkey_b64 + "=" * (-len(pubkey_b64) % 4))
    n, e = der_to_numbers(der)
    k = (n.bit_length() + 7) // 8
    message = plaintext.encode("utf-8")
    if len(message) > k - 11:
        raise ValueError("密码过长，超出 RSA 分组上限")
    ps_len = k - len(message) - 3
    while True:
        padding = os.urandom(ps_len)
        if 0 not in padding:
            break
    block = b"\x00\x02" + padding + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), e, n)
    return base64.b64encode(cipher.to_bytes(k, "big")).decode()

def _der_read(buf: bytes, pos: int):
    tag = buf[pos]
    pos += 1
    length = buf[pos]
    pos += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4:
            raise ValueError("unsupported DER length")
        length = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    return tag, pos, length
