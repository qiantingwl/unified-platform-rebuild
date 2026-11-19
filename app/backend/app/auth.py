"""Shared persistent admin sessions; raw bearer/session material never stored."""
import hashlib
import hmac
import json
import secrets

from sqlalchemy import delete, select

from .models import Resource
from .store import DomainError, uid


class AdminAuth:
    def __init__(self, store, admin_token, username="", password=""):
        self.store = store
        # Rotating admin credential also invalidates all previously issued sessions.
        self.username = username
        self.password_digest = hashlib.scrypt(password.encode(), salt=b"unified-admin-password-v1", n=16384, r=8, p=1) if username and password else None
        self.password_salt=b"unified-admin-password-v1"
        self.environment_identity=(self.username,self.password_digest,self.password_salt)
        self.mode = "password" if self.password_digest else "token"
        self.epoch = hashlib.sha256((admin_token + "\0" + username).encode() + (self.password_digest or b"")).hexdigest()
        self.admin_token = admin_token

    def refresh_identity(self):
        with self.store.read() as (s,_):
            row=s.scalar(select(Resource).where(Resource.kind=='admin_identity',Resource.name=='primary'))
            if row and row.secret:
                value=json.loads(self.store.cipher.decrypt(row.secret.encode()))
                username,digest,salt=value['username'],bytes.fromhex(value['digest']),bytes.fromhex(value['salt'])
            else:
                username,digest,salt=self.environment_identity
        self.username,self.password_digest,self.password_salt=username,digest,salt
        self.mode='password' if digest else 'token'
        self.epoch=hashlib.sha256((self.admin_token+'\0'+username).encode()+(digest or b'')).hexdigest()
        return dict(username=username,digest=digest,salt=salt,epoch=hashlib.sha256((self.admin_token+'\0'+username).encode()+(digest or b'')).hexdigest(),mode='password' if digest else 'token')

    def configure(self,username,password):
        if not username.strip():raise DomainError("invalid_admin_username",422)
        salt=secrets.token_bytes(32)
        digest=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1)
        encrypted=self.store.cipher.encrypt(json.dumps(dict(username=username,digest=digest.hex(),salt=salt.hex())).encode()).decode()
        with self.store.tx() as (s,state):
            row=s.scalar(select(Resource).where(Resource.kind=='admin_identity',Resource.name=='primary'))
            if row is None:
                row=Resource(id=uid(),kind='admin_identity',name='primary',data='{}',updated_at=self.store.clock());s.add(row)
            row.secret=encrypted;row.version=(row.version or 0)+1;row.updated_at=self.store.clock()
            s.execute(delete(Resource).where(Resource.kind=='admin_session'))
            self.store.emit(s,state,'admin','identity_configured')
        self.refresh_identity()

    def login(self, supplied, client_id, username="", password=""):
        identity=self.refresh_identity()
        if identity["mode"] == "password":
            candidate = hashlib.scrypt(password.encode(), salt=identity["salt"], n=16384, r=8, p=1)
            accepted = hmac.compare_digest(username.encode(), identity["username"].encode()) & hmac.compare_digest(candidate, identity["digest"])
        else:
            accepted = bool(self.admin_token) and hmac.compare_digest(supplied.encode(), self.admin_token.encode())
        now = self.store.clock()
        bucket = hashlib.sha256(client_id.encode()).hexdigest()
        rejected = False
        with self.store.tx() as (s, state):
            row = s.scalar(select(Resource).where(Resource.kind == "login_throttle", Resource.name == bucket))
            data = json.loads(row.data) if row else {"window": now, "attempts": 0}
            if now - data["window"] >= 60:
                data = {"window": now, "attempts": 0}
            if data["attempts"] >= 10:
                raise DomainError("login_rate_limited", 429)
            if self.refresh_identity()["epoch"] != identity["epoch"]:accepted=False
            if not accepted:
                data["attempts"] += 1
                if not row:
                    row = Resource(id=uid(), kind="login_throttle", name=bucket, updated_at=now, data="{}")
                    s.add(row)
                row.data, row.updated_at = json.dumps(data), now
                rejected = True
            else:
                if row:
                    s.delete(row)
                token = secrets.token_urlsafe(32)
                digest = hashlib.sha256(token.encode()).hexdigest()
                s.add(Resource(id=uid(), kind="admin_session", name=digest, data=json.dumps({"epoch": identity["epoch"], "expires_at": now + 8*3600}), updated_at=now))
                self.store.emit(s, state, "admin", "session_created")
            s.execute(delete(Resource).where(Resource.kind.in_(["admin_session", "login_throttle"]), Resource.updated_at < now - 86400))
        if rejected:
            raise DomainError("invalid_admin_credentials" if self.mode == "password" else "invalid_admin_token", 401)
        return token

    def valid(self, token):
        identity=self.refresh_identity()
        if not token:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.read() as (s, _):
            row = s.scalar(select(Resource).where(Resource.kind == "admin_session", Resource.name == digest))
            if not row:
                return False
            data = json.loads(row.data)
            return data["expires_at"] > self.store.clock() and hmac.compare_digest(data["epoch"], identity["epoch"])

    def logout(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.tx() as (s, state):
            s.execute(delete(Resource).where(Resource.kind == "admin_session", Resource.name == digest))
            self.store.emit(s, state, "admin", "session_revoked")
