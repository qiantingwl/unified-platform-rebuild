"""Transactional accounts/jobs with row-scoped forwarding admission.

Business writes flush before the short revision increment. Upstream calls never
run inside a database transaction. SQLite retains its native single-writer lock.
"""
import json
import hashlib
import math
import re
import secrets
import time
from collections import Counter
from contextlib import contextmanager
from itertools import chain
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, delete, event, func, select, text, inspect, update
from sqlalchemy.orm import Session

from .models import (
    Account, ApiKey, Attempt, Base, Credential, Event, Job, Lease, ModelState,
    Observation, Receipt, RequestLog, State, RequestAttempt, Affinity, Resource,
)

PLATFORMS = ["Loomy", "小浣熊", "WPS 灵犀", "WorkBuddy 国内", "WorkBuddy 国际", "CodeArts", "Marvis"]
PRIORITY = {"checkin": 10, "balance": 20, "onboarding": 25, "benefits": 26, "renew": 30, "relogin": 40, "verify": 50, "invitation": 60}
ACCOUNT_FAILURES = {"no_sms", "invalid_credentials", "login_rejected"}
REQUEST_LEASE_SECONDS = 180
TEMPORARY_MODEL_BLOCKS = frozenset({"network", "rate_limit", "unknown_403"})
ERROR_CODES = ACCOUNT_FAILURES | {"permission", "rate_limit", "network", "unknown_403", "executor_lost", "missing_material", "adapter_unavailable", "cancelled", "internal_error", "network_execution_disabled", "quota", "identity", "upstream_5xx", "upstream_timeout", "response_read_failed", "invalid_response", "invalid_balance", "unconfirmed_checkin", "iam_refresh_material_required", "identity_mismatch", "upstream_rejected"}
ERROR_CODES |= {"identity_verifier_unavailable", "invalid_credential_format", "response_too_large", "frame_too_large", "invalid_stream_frame", "truncated_stream", "startup_timeout", "stream_idle", "empty_response", "unsupported_image_input", "unsupported_protocol_field", "prompt_too_large", "model_not_found", "unexpected_redirect"}
ERROR_CODES.add("upstream_queue_wait")
ERROR_CODES |= {"checkin_readback_unavailable", "checkin_inactive", "request_rejected", "content_filter"}
ERROR_CODES |= {"no_effective_output", "proxy_unavailable", "proxy_pool_unavailable", "proxy_pool_missing", "proxy_route_invalid", "proxy_retired", "missing_proxy", "proxy_target_unverified", "maintenance_proxy_route_invalid"}
# 上游明确说「这个账号已被停用」：不是凭证过期、也不是额度问题，重登也不会好转。
ERROR_CODES.add("account_disabled")
ERROR_CODES |= {"onboarding_uncertain", "onboarding_incomplete", "onboarding_ledger_required", "unconfirmed_onboarding"}
ERROR_CODES |= {'benefits_uncertain','material_required','capability_unavailable','benefits_unavailable','agreement_required'}
ERROR_CODES |= {'benefits_material_required','benefits_incomplete'}
ERROR_CODES |= {'benefits_business_day_changed','previous_operation_requires_review','independent_session_required'}
RETRYABLE_BALANCE_ERRORS = {"network", "upstream_timeout", "upstream_5xx", "response_read_failed", "rate_limit", "executor_lost", "proxy_unavailable", "proxy_pool_unavailable"}


class DomainError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


def uid():
    return str(uuid4())


def day_at(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).date().isoformat()


class Store:
    def __init__(self, url, key, clock=time.time):
        self.clock = clock
        self.real_capabilities = {}
        self.cipher = Fernet(key)
        if url.startswith("sqlite"):
            self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15}, pool_pre_ping=True)
        else:
            # Request threads, maintenance executors and registration all hold a
            # connection while queued on the state row; the default 5+10 pool starves.
            self.engine = create_engine(url, pool_pre_ping=True, pool_size=20, max_overflow=40, pool_recycle=1800)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_setup(conn, _):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=NORMAL")
        # API and tasks start independently, often at the same instant. PostgreSQL
        # checkfirst alone does not serialize DDL or the initial state-row insert.
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(7283041205)"))
            from . import model_routes  # register dedicated catalog table before serialized DDL
            Base.metadata.create_all(connection)
            for table, column, sqltype in [('accounts','original_label','VARCHAR(100)'),('accounts','priority','INTEGER NOT NULL DEFAULT 0'),('accounts','balance_interval_seconds','INTEGER'),('accounts','daily_checkin_enabled','BOOLEAN NOT NULL DEFAULT TRUE'),('accounts','daily_checkin_minute','INTEGER'),('request_attempts','selection_data','TEXT'),('request_attempts','upstream_response_at','FLOAT'),('request_attempts','first_frame_at','FLOAT'),('events','request_id','VARCHAR(36)')]:
                if column not in {c['name'] for c in inspect(connection).get_columns(table)}:
                    connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {sqltype}'))
            if 'revoked_at' not in {c['name'] for c in inspect(connection).get_columns('api_keys')}:
                connection.execute(text('ALTER TABLE api_keys ADD COLUMN revoked_at FLOAT'))
            # Additive migration for existing installations, serialized by the
            # same PostgreSQL startup lock. Never recreate or empty the tables.
            columns = {c['name'] for c in inspect(connection).get_columns('request_logs')}
            for name, sqltype in {'output_tokens':'INTEGER','total_tokens':'INTEGER','reasoning_tokens':'INTEGER','key_id':'VARCHAR(36)','route_version':'INTEGER','authorized_model':'VARCHAR(100)','client_source':'VARCHAR(40)','retry_expires_at':'FLOAT'}.items():
                if name not in columns:
                    connection.execute(text(f'ALTER TABLE request_logs ADD COLUMN {name} {sqltype}'))
            if 'finished_revision' not in {c['name'] for c in inspect(connection).get_columns('jobs')}:
                connection.execute(text('ALTER TABLE jobs ADD COLUMN finished_revision INTEGER'))
            if 'attempts' not in {c['name'] for c in inspect(connection).get_columns('account_model_states')}:
                connection.execute(text('ALTER TABLE account_model_states ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0'))
            for table in Base.metadata.sorted_tables:
                for index in table.indexes:
                    index.create(connection, checkfirst=True)
            from .registration_queries import ensure_registration_indexes
            ensure_registration_indexes(connection)
            with Session(bind=connection) as s, s.begin():
                state = s.get(State, 1)
                if state is None:
                    s.add(State(id=1, schema_version=2, revision=0))
                elif state.schema_version == 1:
                    state.schema_version = 2
                elif state.schema_version != 2:
                    raise RuntimeError("Unsupported schema version")

    @contextmanager
    def tx(self, scope='maintenance'):
        with Session(self.engine, expire_on_commit=False) as s:
            try:
                if self.engine.dialect.name == "sqlite":
                    s.connection().exec_driver_sql("BEGIN IMMEDIATE")
                elif scope is not None:
                    # Maintenance/config transitions share a short domain guard;
                    # forwarding uses key/request/account rows independently.
                    lock_id = int.from_bytes(hashlib.sha256(str(scope).encode()).digest()[:8], 'big', signed=True)
                    s.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_id})
                s.info['mutation'] = True
                initial = s.scalar(select(State.revision).where(State.id == 1))
                state = SimpleNamespace(revision=initial, events=[])
                yield s, state
                # Flush all business writes before touching the revision row.
                # No transaction waits on another domain/account after this point.
                finished = [(row, row.finished_revision) for row in s.identity_map.values()
                            if isinstance(row, Job) and row.finished_revision is not None and row.finished_revision > initial]
                s.flush()
                if state.events:
                    end = s.scalar(update(State).where(State.id == 1).values(revision=State.revision + len(state.events)).returning(State.revision))
                    base = end - len(state.events)
                    for number, row in enumerate(state.events, 1): row.revision = base + number
                    for row, provisional in finished: row.finished_revision = base + min(max(provisional-initial, 1), len(state.events))
                s.commit()
            except BaseException:
                s.rollback()
                raise

    @contextmanager
    def read(self):
        """Snapshot read without the state-row lock; never mutate through it."""
        with Session(self.engine, expire_on_commit=False, autoflush=False) as s:
            try:
                if self.engine.dialect.name == "postgresql":
                    s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                    s.execute(text("SET TRANSACTION READ ONLY"))
                else:
                    s.connection().exec_driver_sql("BEGIN")
                yield s, s.get(State, 1)
            finally:
                s.rollback()

    def has_synthetic_accounts(self):
        with self.read() as (s, _):
            return s.scalar(select(Account.id).where(Account.sandbox.is_(True)).limit(1)) is not None

    def revision(self):
        with self.read() as (_, state):
            return state.revision

    @staticmethod
    def block_active(block, now):
        """限制是有期限的冷却，不是永久判决。

        旧网关（3065/3066）对「额度不足 / 权限 / 限流」给的都是 cool_until，
        到点自动放回候选池；只有「账号已被停用」才是永久结论，而且那条走的是
        credential_status=disabled，不在这里。我们早期把迁入的 cool_kind 原样
        写成 review_at 已过期的通配符 ModelState，而调度只看 verified_at 是不是
        空，于是 2024 个 Loomy 账户被永久拒派——同一批号在旧网关近 6 小时还有
        3638 次成功（WPS 561 个号 12.8 万次）。

        规则：
        - 未到期一律拦；
        - 到期后，限流、网络抖动和没有明确业务码的403和通配符限制（没有可探测的
          模型目标）不再拦；
        - 具体模型的 quota/permission/identity 仍留到「原模型真答一次」为止
          （r6/r9/r10 的口径不变）。
        放回不等于宣布可用：下一次真实请求若仍被拒，fail_attempt 会按退避重新 block。
        """
        if block.verified_at is not None:
            return False
        if block.review_at > now:
            return True
        return not (block.model == "*" or block.reason in TEMPORARY_MODEL_BLOCKS)

    @staticmethod
    def routing_index(leases, blocks, now):
        """Per-account lookups so selection is not accounts x restrictions."""
        busy, blocked = Counter(), {}
        for lease in leases:
            if lease.expires_at > now:
                busy[lease.account_id] += 1
        for block in blocks:
            if Store.block_active(block, now):
                blocked.setdefault(block.account_id, []).append(block)
        return busy, blocked

    def expire_model_blocks(self):
        """把到期的通配符/瞬时限制标记解除，别让页面永远挂着过期封锁。

        具体模型的限制仍交给 verify 去证明恢复，不能在这里悄悄放出去。
        """
        now = self.clock()
        with self.tx() as (s, state):
            rows = [m for m in s.scalars(select(ModelState).where(
                ModelState.verified_at.is_(None), ModelState.review_at <= now))
                if not self.block_active(m, now)]
            for m in rows:
                m.verified_at, m.attempts = now, 0
            if rows:
                self.emit(s, state, "model", "blocks_expired", None)
            return len(rows)

    def observe_auth_failure(self, s, state, a):
        """Observe -> review -> confirm. The first 401 only pauses the account and queues one
        credential review; a second failure on the same version confirms it and allows relogin."""
        if a.credential_status == "suspect" or "balance" not in self.real_capabilities.get(a.platform, set()) and not a.sandbox:
            a.credential_status = "invalid"
            self.emit(s, state, "account", "credential_confirmed_invalid", a.id)
            return
        if a.credential_status == "invalid":
            return
        a.credential_status = "suspect"
        # A later failure on the same credential is a new observation cycle.
        # Never reopen a successful query: its immutable balance receipt is
        # unique per job and must remain part of the history.
        review = self.enqueue(s, a, "balance", f"auth_review:{a.credential_version}:{uid()}")
        review.priority = 5
        if review.status in {"failed", "superseded", "uncertain"}:
            review.status, review.finished_at, review.error_code, review.available_at = "queued", None, None, self.clock()
            review.result_code = None
        self.emit(s, state, "account", "credential_review_scheduled", a.id, review.id)

    def mark_upstream_disabled(self, s, state, a):
        """上游明确说「账号已被停用」：停止调度，保留余额与历史，等人工重新检测。

        和 invalid（凭证失效、可以重登）区别开：这类号重登也不会好转，
        放进重登队列只会白烧短信。也不改 enabled —— 那是人的开关，
        程序不替人做停用决定，只把自己的可调度视图收起来。
        """
        if a.credential_status == "disabled":
            return
        a.credential_status, a.updated_at = "disabled", self.clock()
        self.emit(s, state, "account", "upstream_disabled", a.id)

    def emit(self, s, state, kind, code, account_id=None, job_id=None, request_id=None):
        state.revision += 1
        row = Event(revision=state.revision, kind=kind, code=code, account_id=account_id, job_id=job_id, request_id=request_id, created_at=self.clock())
        state.events.append(row)
        s.add(row)

    def account(self, s, account_id):
        query = select(Account).where(Account.id == account_id)
        if s.info.get('mutation'): query = query.with_for_update().execution_options(populate_existing=True)
        a = s.scalar(query)
        if not a:
            raise DomainError("account_not_found", 404)
        return a

    def create_account(self, platform, external_id, label, sandbox=False):
        if platform not in PLATFORMS:
            raise DomainError("unsupported_platform", 422)
        with self.tx() as (s, state):
            existing = s.scalar(select(Account).where(Account.platform == platform, Account.external_id == external_id))
            if existing:
                return existing.id
            a = Account(id=uid(), platform=platform, external_id=external_id, label=label, sandbox=sandbox, created_at=self.clock(), updated_at=self.clock())
            s.add(a)
            self.emit(s, state, "account", "created", a.id)
            return a.id

    def put_credential(self, account_id, secret, expected_version):
        with self.tx() as (s, state):
            a = self.account(s, account_id)
            if a.credential_version != expected_version:
                raise DomainError("credential_version_conflict")
            a.credential_version += 1
            a.credential_status = "pending"
            a.updated_at = self.clock()
            s.add(Credential(id=uid(), account_id=a.id, version=a.credential_version, ciphertext=self.cipher.encrypt(secret.encode()).decode(), created_at=self.clock()))
            for j in s.scalars(select(Job).where(Job.account_id == a.id, Job.status == "queued")):
                j.status, j.result_code, j.finished_at = "superseded", "credential_replaced", self.clock()
            self.after_login(s,state,a,f"credential:{a.credential_version}")
            self.emit(s, state, "credential", "updated_pending_validation", a.id)
            return a.credential_version

    def enqueue(self, s, a, kind, suffix=None):
        day = day_at(self.clock())
        key = f"{a.id}:{kind}:{suffix or day}"
        existing = s.scalar(select(Job).where(Job.dedupe_key == key))
        if existing:
            return existing
        j = self.new_job(s, a, kind, suffix)
        s.flush()
        return j

    def new_job(self, s, a, kind, suffix=None):
        """Append after deduplication under tx's lock, allowing bulk scheduling.

        Callers must have established that the key is absent in the same
        transaction. Normal individual callers continue to use enqueue().
        """
        day = day_at(self.clock())
        j = Job(id=uid(), dedupe_key=f"{a.id}:{kind}:{suffix or day}", account_id=a.id, kind=kind, business_day=day, credential_version=a.credential_version, status="queued", attempt=0, priority=PRIORITY[kind], available_at=self.clock(), created_at=self.clock())
        s.add(j)
        return j

    def plan(self, account_id, kind, model=None):
        if kind not in PRIORITY:
            raise DomainError("unsupported_job", 422)
        with self.tx() as (s, state):
            a = self.account(s, account_id)
            if kind == 'renew' and not a.sandbox:
                from .native_lifecycle import control
                lifecycle=control(s,a)
                if lifecycle['state']!='independent': raise DomainError('independent_session_required',409)
            if kind == 'benefits':
                if a.platform != 'WorkBuddy 国内': raise DomainError('capability_unavailable',422)
                row = s.scalar(select(Credential).where(Credential.account_id==a.id,Credential.version==a.credential_version))
                data = json.loads(self.cipher.decrypt(row.ciphertext.encode())) if row else {}
                profile = data.get('account') or {}
                if profile.get('enterpriseId') or profile.get('enterprise_id'): raise DomainError('capability_unavailable',422)
            if not a.sandbox and kind not in self.real_capabilities.get(a.platform, set()):
                raise DomainError("adapter_unavailable", 501)
            if kind == "verify" and not a.sandbox and (not model or len(model) > 100):
                raise DomainError("model_required", 422)
            day = day_at(self.clock())
            if kind == "checkin" and a.checkin_day == day:
                raise DomainError("already_checked_in")
            if kind == "relogin":
                if a.relogin_failures >= 3:
                    raise DomainError("relogin_attempts_exhausted")
                if a.relogin_day == day:
                    raise DomainError("relogin_already_scheduled_today")
                unresolved = s.scalar(select(Job).where(Job.account_id == a.id, Job.kind == "relogin", Job.status.in_(["queued", "running", "uncertain"])))
                if unresolved:
                    raise DomainError("relogin_result_unresolved")
                a.relogin_day = day
            suffix = None
            if kind in {"balance", "verify", "renew", "onboarding", "benefits"}:
                if kind == 'benefits':
                    uncertain=s.scalar(select(Job).where(Job.account_id==a.id,Job.kind==kind,Job.credential_version==a.credential_version,Job.status=='uncertain').order_by(Job.created_at.desc()).limit(1))
                    if uncertain:
                        target=s.scalar(select(Resource).where(Resource.kind=='job_target',Resource.name==uncertain.id))
                        if target is None:
                            target=Resource(id=uid(),kind='job_target',name=uncertain.id,data='{}',updated_at=self.clock());s.add(target)
                        target.data=json.dumps({'reconcile_benefits':True})
                        uncertain.status,uncertain.available_at,uncertain.finished_at='queued',self.clock(),None
                        self.emit(s,state,'job','benefits_readback_scheduled',a.id,uncertain.id)
                        return uncertain.id
                for pending in s.scalars(select(Job).where(Job.account_id == a.id, Job.kind == kind, Job.credential_version == a.credential_version, Job.status.in_(["queued", "running"]))):
                    target = s.scalar(select(Resource).where(Resource.kind == "job_target", Resource.name == pending.id))
                    if kind != "verify" or (json.loads(target.data)["model"] if target else "sandbox-echo") == (model or "sandbox-echo"):
                        return pending.id
                if kind == "onboarding" and s.scalar(select(Job.id).where(Job.account_id==a.id,Job.kind==kind,Job.status=="uncertain")):
                    raise DomainError("previous_operation_requires_review")
                if kind == "renew":
                    previous = s.scalar(select(Job).where(Job.account_id == a.id, Job.kind == kind, Job.credential_version == a.credential_version, Job.status.in_(["failed", "uncertain"])).order_by(Job.created_at.desc()))
                    if previous:
                        candidate = s.scalar(select(Resource).where(Resource.kind == "renewal_candidate", Resource.name == previous.id))
                        if candidate:
                            previous.status, previous.error_code, previous.finished_at = "queued", None, None
                            previous.available_at = self.clock()
                            self.emit(s, state, "renewal", "validation_resumed", a.id, previous.id)
                            return previous.id
                        if previous.status == "uncertain":
                            raise DomainError("previous_operation_requires_review")
                # Queries deduplicate while pending, not for the entire business day.
                suffix = f"manual:{uid()}"
            j = self.enqueue(s, a, kind, suffix)
            if kind == "checkin" and j.status in {"failed", "uncertain", "superseded"} and j.attempt:
                # A manual request must actually progress the old record, but
                # must not repeat a claim whose external result was lost.
                target = s.scalar(select(Resource).where(Resource.kind == "job_target", Resource.name == j.id))
                if target is None:
                    target = Resource(id=uid(), kind="job_target", name=j.id, data="{}", updated_at=self.clock())
                    s.add(target)
                target.data = json.dumps({"reconcile_checkin": True})
                j.status, j.credential_version, j.available_at = "queued", a.credential_version, self.clock()
                j.finished_at, j.error_code, j.result_code = None, None, None
            if kind == "verify":
                s.add(Resource(id=uid(), kind="job_target", name=j.id, data=json.dumps({"model": model or "sandbox-echo"}), updated_at=self.clock()))
            if j.status == "superseded":
                if j.attempt:
                    raise DomainError("previous_operation_requires_review")
                j.status, j.credential_version = "queued", a.credential_version
                j.finished_at, j.result_code = None, None
            self.emit(s, state, "job", "scheduled", a.id, j.id)
            return j.id

    def schedule_today(self, max_attempts=3, retry_interval=1800, *, automatic=False, daily_checkin_minute=0):
        planned = 0
        with self.tx() as (s, state):
            day = day_at(self.clock())
            local = datetime.fromtimestamp(self.clock(), ZoneInfo("Asia/Shanghai"))
            minute = local.hour * 60 + local.minute
            today_jobs = {j.account_id: j for j in s.scalars(select(Job).where(Job.kind == "checkin", Job.business_day == day))}
            for a in s.scalars(select(Account)):
                if automatic:
                    scheduled_minute = a.daily_checkin_minute if a.daily_checkin_minute is not None else daily_checkin_minute
                    if not a.daily_checkin_enabled or minute < scheduled_minute:
                        continue
                if not a.sandbox and "checkin" not in self.real_capabilities.get(a.platform, set()):
                    continue
                if a.checkin_day != day and a.credential_version > 0 and a.credential_status not in {"invalid", "disabled"}:
                    before = today_jobs.get(a.id)
                    # A definitive failure (not an uncertain one) is retried later the same day.
                    retry = before is not None and before.status == "failed" and before.attempt < max_attempts and (before.finished_at or 0) + retry_interval <= self.clock() and before.error_code not in {"invalid_credentials", "checkin_inactive", "adapter_unavailable"}
                    if before is None or retry or (before.status == "superseded" and before.attempt == 0):
                        j = before if before is not None else self.new_job(s, a, "checkin")
                        j.available_at, j.error_code = self.clock(), None
                        j.status, j.credential_version = "queued", a.credential_version
                        j.finished_at, j.result_code = None, None
                        self.emit(s, state, "job", "scheduled", a.id, j.id)
                        planned += 1
        return planned

    def reconcile_maintenance(self):
        """Finish already admitted credential validation even when periodic work is off.

        A failed sign-in or an inconclusive 401 review must not strand pending/
        suspect credentials. Only read-only balance queries are retried; SMS,
        model probes and uncertain sign-ins are never repeated here.
        """
        now = self.clock()
        with self.tx() as (s, state):
            accounts = list(s.scalars(select(Account).where(
                Account.credential_status.in_(["pending", "suspect"]), Account.credential_version > 0)))
            if not accounts:
                return 0
            needs_validation = select(Account.id).where(
                Account.credential_status.in_(["pending", "suspect"]), Account.credential_version > 0)
            active = set(s.scalars(select(Job.account_id).join(Account, Account.id == Job.account_id).where(
                Job.account_id.in_(needs_validation), Job.credential_version == Account.credential_version,
                Job.kind.in_(["checkin", "balance"]), Job.status.in_(["queued", "running"])).distinct()))
            ranked = select(Job.id, func.row_number().over(partition_by=Job.account_id, order_by=(
                func.coalesce(Job.finished_at, Job.created_at).desc(), Job.finished_revision.desc().nullslast(), Job.created_at.desc(), Job.id.desc()
            )).label("rank")).join(Account, Account.id == Job.account_id).where(
                Job.account_id.in_(needs_validation), Job.credential_version == Account.credential_version, Job.kind == "balance").subquery()
            latest = {j.account_id: j for j in s.scalars(select(Job).join(ranked, ranked.c.id == Job.id).where(ranked.c.rank == 1))}
            planned = 0
            for account in accounts:
                if account.id in active or not account.sandbox and "balance" not in self.real_capabilities.get(account.platform, set()):
                    continue
                previous = latest.get(account.id)
                if previous and previous.status == "failed":
                    if previous.error_code in RETRYABLE_BALANCE_ERRORS:
                        delay = min(300 * 2 ** min(max(previous.attempt - 1, 0), 4), 3600)
                    else:
                        # 业务码拒绝（上游停用、未知 code）以前一旦失败就再也不重排：
                        # 账户停在 pending，没有任务、没有结论，人也不知道该干什么。
                        # 给 6 小时一次的长周期复核，三次仍不通过就停手等人工。
                        if previous.attempt >= 3:
                            continue
                        delay = 21600
                    if now < (previous.finished_at or previous.created_at) + delay:
                        continue
                    job = previous
                elif previous and previous.status == "uncertain":
                    job = previous  # A balance read has no external side effect.
                else:
                    suffix = f"auth_review:{account.credential_version}:{uid()}" if account.credential_status == "suspect" else f"validation:{account.credential_version}:{uid()}"
                    job = self.new_job(s, account, "balance", suffix)
                job.status, job.available_at = "queued", now
                job.error_code, job.result_code, job.finished_at = None, None, None
                job.priority = 5 if account.credential_status == "suspect" else PRIORITY["balance"]
                self.emit(s, state, "job", "credential_validation_resumed", account.id, job.id)
                planned += 1
            return planned

    def recover_expired(self, s, state):
        now = self.clock()
        for j in s.scalars(select(Job).where(Job.status == "running", Job.lease_until < now)):
            # Sending a code or claiming a check-in may have happened before a crash.
            # Never infer an external failure or blindly repeat a non-idempotent action.
            j.status = "queued" if j.kind in {"balance", "verify"} else "uncertain"
            j.owner, j.lease_until, j.error_code = None, None, "executor_lost"
            s.execute(delete(Lease).where(Lease.request_id == j.id))
            for a in s.scalars(select(Attempt).where(Attempt.job_id == j.id, Attempt.outcome == "running")):
                a.outcome, a.finished_at = "executor_lost", now
            if j.kind=="onboarding":
                account=self.account(s,j.account_id)
                self.onboarding_followup(s,state,account,j,"uncertain")
            self.emit(s, state, "job", "lease_expired", j.account_id, j.id)

    def claim(self, owner, capacity=8, lease_seconds=30):
        with self.tx() as (s, state):
            capacity=self.execution_capacity(s,capacity)
            self.recover_expired(s, state)
            running = list(s.scalars(select(Job).where(Job.status == "running")))
            external = self.execution_leases(s)
            if len(running) + len(external) >= capacity:
                return None
            busy = {j.account_id for j in running} | {x.get("account_id") for x in external}
            # Already busy accounts cannot claim another maintenance job.
            # Filter before taking any row lock so a blocked queue head does
            # not lock accounts that are still serving forwarding requests.
            ready = select(Job).where(Job.status == "queued", Job.available_at <= self.clock())
            if busy:
                ready = ready.where(Job.account_id.not_in(busy))
            candidates = s.scalars(ready.order_by(Job.priority, Job.created_at, Job.id).execution_options(yield_per=64))
            for j in candidates:
                a = self.account(s, j.account_id)
                if j.kind=='renew' and not a.sandbox:
                    from .native_lifecycle import control
                    if control(s,a)['state']!='independent':
                        j.status,j.result_code,j.finished_at='superseded','independent_session_required',self.clock()
                        self.emit(s,state,'job','independent_session_required',a.id,j.id)
                        continue
                if not a.sandbox and j.kind not in self.real_capabilities.get(a.platform, set()):
                    continue
                if j.credential_version != a.credential_version:
                    j.status, j.result_code, j.finished_at = "superseded", "credential_replaced", self.clock()
                    self.emit(s, state, "job", "stale_job_skipped", a.id, j.id)
                    continue
                target = s.scalar(select(Resource).where(Resource.kind == "job_target", Resource.name == j.id))
                target_data=json.loads(target.data) if target else {}
                model = target_data.get("model","sandbox-echo")
                if j.kind == "verify" and target:
                    # Preserve ordering when timestamp resolution ties a prior
                    # refusal with this claim. New refusals at that same instant
                    # still invalidate this probe, unlike using a strict > test.
                    target_data["prior_rejection_ids"] = list(s.scalars(select(RequestAttempt.id).where(
                        RequestAttempt.account_id == a.id,
                        RequestAttempt.credential_version == j.credential_version,
                        RequestAttempt.ended_at == self.clock())))
                    target.data = json.dumps(target_data)
                if j.kind == "verify" and not a.sandbox:
                    leased = list(s.scalars(select(Lease).where(Lease.account_id == a.id, Lease.expires_at > self.clock())))
                    if len(leased) >= a.concurrency:
                        continue
                    # Model recovery test occupies the same native account slot.
                    s.add(Lease(id=uid(), account_id=a.id, request_id=j.id, expires_at=self.clock() + lease_seconds))
                j.status, j.owner = "running", owner
                j.lease_until, j.attempt = self.clock() + lease_seconds, j.attempt + 1
                s.add(Attempt(id=uid(), job_id=j.id, number=j.attempt, owner=owner, started_at=self.clock()))
                self.emit(s, state, "job", "claimed", a.id, j.id)
                return {"id": j.id, "account_id": a.id, "kind": j.kind, "owner": owner, "attempt": j.attempt, "version": j.credential_version, "sandbox": a.sandbox, "platform": a.platform, "day": j.business_day, "model": model,"reconcile_checkin":target_data.get("reconcile_checkin",False),"reconcile_onboarding":target_data.get("reconcile_onboarding",False),"reconcile_benefits":target_data.get("reconcile_benefits",False),"benefits_read_only":target_data.get("benefits_read_only",False)}
            return None

    def execution_leases(self, s):
        """Registration network steps share the maintenance pool across processes."""
        return [json.loads(r.data) for r in s.scalars(select(Resource).where(Resource.kind == "execution_lease")) if json.loads(r.data).get("expires_at", 0) > self.clock()]

    def execution_capacity(self,s,fallback):
        config=s.scalar(select(Resource).where(Resource.kind=="settings",Resource.name=="runtime"))
        return int(json.loads(config.data).get("maintenance_capacity",fallback)) if config else fallback

    def acquire_execution(self, session_id, capacity, prioritize_maintenance=False):
        with self.tx() as (s, state):
            capacity=self.execution_capacity(s,capacity)
            self.recover_expired(s, state)
            active = self.execution_leases(s)
            if any(x["session_id"] == session_id for x in active):
                raise DomainError("registration_execution_busy")
            registration = s.get(Resource, session_id)
            if not registration or registration.kind != "registration":
                raise DomainError("registration_not_found", 404)
            account_id = json.loads(registration.data).get("account_id")
            jobs = list(s.scalars(select(Job).where(Job.status == "running")))
            if account_id and (any(j.account_id == account_id for j in jobs) or any(x.get("account_id") == account_id for x in active)):
                raise DomainError("account_maintenance_busy")
            if len(jobs) + len(active) >= capacity:
                raise DomainError("maintenance_capacity_busy")
            if prioritize_maintenance:
                busy = {j.account_id for j in jobs} | {x.get("account_id") for x in active}
                for job in s.scalars(select(Job).where(Job.status == "queued", Job.kind.in_(["checkin", "balance"]), Job.available_at <= self.clock())):
                    a = self.account(s, job.account_id)
                    if a.id not in busy and job.credential_version == a.credential_version and (a.sandbox or job.kind in self.real_capabilities.get(a.platform, set())):
                        raise DomainError("maintenance_priority_pending")
            row = s.scalar(select(Resource).where(Resource.kind == "execution_lease", Resource.name == session_id))
            if row is None:
                row = Resource(id=uid(), kind="execution_lease", name=session_id, data="{}", updated_at=self.clock())
                s.add(row)
            token = secrets.token_hex(16)
            row.data = json.dumps({"session_id": session_id, "account_id": account_id, "owner": token, "expires_at": self.clock() + 120})
            row.updated_at = self.clock()
            self.emit(s,state,"registration","execution_acquired",account_id)
            return token

    def heartbeat_execution(self, session_id, token):
        with self.tx() as (s, _):
            row = s.scalar(select(Resource).where(Resource.kind == "execution_lease", Resource.name == session_id))
            data = json.loads(row.data) if row else {}
            if data.get("owner") != token or data.get("expires_at", 0) <= self.clock():
                raise DomainError("registration_execution_lost")
            data["expires_at"] = self.clock() + 120
            row.data, row.updated_at = json.dumps(data), self.clock()

    def release_execution(self, session_id, token):
        with self.tx() as (s, state):
            row = s.scalar(select(Resource).where(Resource.kind == "execution_lease", Resource.name == session_id))
            if row and json.loads(row.data).get("owner") == token:
                self.emit(s,state,"registration","execution_released",json.loads(row.data).get("account_id"))
                s.delete(row)

    def supersede_balance_queries(self, s, state, jobs):
        """Keep queue history when a later confirmed observation covers an old query."""
        count = 0
        for pending in jobs:
            pending.status, pending.result_code = "superseded", "balance_already_verified"
            pending.finished_at, pending.finished_revision = self.clock(), state.revision + 1
            pending.error_code, pending.owner, pending.lease_until = None, None, None
            self.emit(s, state, "job", "balance_already_verified", pending.account_id, pending.id)
            count += 1
        return count

    def reconcile_migration_balances(self, apply=False, limit=1000):
        """Retire covered migration queries only; never cancel auth review or live work.

        Defaults to a preview. Each apply is one bounded atomic transaction;
        callers may repeat until no eligible migration queries remain.
        """
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise DomainError("invalid_reconcile_limit", 422)
        with (self.tx() if apply else self.read()) as (s, state):
            latest = select(Observation.account_id, Observation.credential_version,
                            func.max(Observation.created_at).label("observed_at")).group_by(
                                Observation.account_id, Observation.credential_version).subquery()
            covered = list(s.scalars(select(Job).join(Account, Account.id == Job.account_id).join(
                latest, (latest.c.account_id == Job.account_id) & (latest.c.credential_version == Job.credential_version)
            ).where(
                Job.kind == "balance", Job.status == "queued", Job.dedupe_key.like("%:balance:migration:%"),
                Account.credential_status == "valid", Job.credential_version == Account.credential_version,
                Account.balance_version == Account.credential_version, Account.balance.is_not(None), Account.balance >= 0,
                Account.balance_at > Job.created_at, Account.balance_at <= self.clock(),
                latest.c.observed_at > Job.created_at, latest.c.observed_at >= Job.available_at,
            ).order_by(Job.created_at, Job.id).limit(limit)))
            count = len(covered)
            changed = self.supersede_balance_queries(s, state, covered) if apply else 0
            return {"eligible": count, "superseded": changed, "scope": "covered_queued_migration_balances_only", "limit": limit}

    def queue_registration_onboarding(self, s, state, account):
        """Only a new-registration marker opts into automatic once-only rewards."""
        if "onboarding" not in self.real_capabilities.get(account.platform,set()):
            return
        marker=s.scalar(select(Resource).where(Resource.kind=="onboarding_pending",Resource.name==account.id))
        if not marker:
            return
        data=json.loads(marker.data)
        if data.get("source")!="registration" or data.get("status") not in {"pending","queued"}:
            return
        previous=s.get(Job,data["job_id"]) if data.get("job_id") else None
        if previous and previous.status!="superseded":
            return
        job=self.enqueue(s,account,"onboarding",f"registration:{marker.id}:{account.credential_version}")
        data.update(status="queued",job_id=job.id)
        marker.data,marker.updated_at=json.dumps(data),self.clock()
        self.emit(s,state,"job","onboarding_after_balance",account.id,job.id)

    def onboarding_followup(self,s,state,account,job,status):
        marker=s.scalar(select(Resource).where(Resource.kind=="onboarding_pending",Resource.name==account.id))
        if marker:
            data=json.loads(marker.data)
            data.update(status="pending" if status=="superseded" else status,job_id=job.id)
            marker.data,marker.updated_at=json.dumps(data),self.clock()
        followup=self.enqueue(s,account,"balance",f"after_onboarding:{job.id}:{job.attempt}")
        followup.priority=5
        self.emit(s,state,"job","balance_after_onboarding",account.id,followup.id)

    def finish(self, job_id, owner, attempt, success, error=None, balance=None, effective_answer=False):
        if error and error not in ERROR_CODES:
            error = "internal_error"
        with self.tx() as (s, state):
            j = s.get(Job, job_id)
            if not j:
                raise DomainError("job_not_found", 404)
            if j.status != "running" or j.owner != owner or j.attempt != attempt or j.lease_until <= self.clock():
                raise DomainError("stale_job_receipt")
            a = self.account(s, j.account_id)
            stale = j.credential_version != a.credential_version
            # A confirmed check-in is a fact about account/day, not credential
            # validity. Keep that receipt and query balance using the new version.
            if stale and success and j.kind in {"checkin","onboarding"}:
                stale = False
            if success and j.kind == "balance" and (balance is None or not math.isfinite(balance) or balance < 0):
                raise DomainError("invalid_balance", 422)
            if success and j.kind == "verify" and not effective_answer:
                success, error = False, "permission"
            # Registration/relogin adapters must return newly issued credentials.
            # The local adapter deliberately does not implement these capabilities.
            if success and j.kind in {"renew", "relogin"}:
                raise DomainError("verified_credential_receipt_required", 422)
            j.status = "superseded" if stale else "succeeded" if success else "failed"
            if not stale and not success and j.kind=="onboarding" and error=="onboarding_uncertain":
                j.status="uncertain"
            if not stale and not success and j.kind=='benefits' and error=='benefits_uncertain':
                j.status='uncertain'
            if not stale and not success and j.kind=="onboarding" and error!="onboarding_incomplete":
                target=s.scalar(select(Resource).where(Resource.kind=="job_target",Resource.name==j.id))
                if target and json.loads(target.data).get("reconcile_onboarding"):
                    # A failed read is not proof that an earlier reward was unpaid.
                    j.status="uncertain"
            j.finished_at, j.lease_until = self.clock(), None
            j.finished_revision = state.revision + 1
            j.result_code = "stale_credential_result" if stale else "confirmed" if success else None
            j.error_code = None if stale or success else error or "internal_error"
            s.execute(delete(Lease).where(Lease.request_id == j.id))
            active_attempt = s.scalar(select(Attempt).where(Attempt.job_id == j.id, Attempt.number == attempt))
            active_attempt.outcome, active_attempt.finished_at = j.status, self.clock()
            if not stale:
                a.updated_at = self.clock()
                if success and j.kind == "checkin":
                    receipt = s.scalar(select(Receipt).where(Receipt.account_id == a.id, Receipt.business_day == j.business_day))
                    if receipt is None:
                        s.add(Receipt(id=uid(), account_id=a.id, business_day=j.business_day, job_id=j.id, created_at=self.clock()))
                    a.checkin_day = max(a.checkin_day or "", j.business_day)
                    followup = self.enqueue(s, a, "balance", f"after_checkin:{j.business_day}")
                    # Complete this account's check-in chain before routine jobs;
                    # the remaining shared slots continue taking new check-ins.
                    followup.priority = 5
                    if followup.status in {"superseded","failed"} and followup.credential_version!=a.credential_version:
                        followup.status,followup.credential_version,followup.finished_at,followup.error_code="queued",a.credential_version,None,None
                    self.emit(s, state, "job", "balance_after_checkin", a.id, followup.id)
                elif success and j.kind == "balance":
                    a.balance, a.balance_at, a.balance_version = balance, self.clock(), j.credential_version
                    # Sandbox query confirms the sandbox credential only. A model
                    # permission block remains until an effective-answer check passes.
                    # 上游判过「停用」的账户不因一次余额可读就复活：停用的是账号而不是
                    # 凭证，只有人工重新检测（recheck 把状态放回 pending）才重走核验。
                    if a.credential_status != "disabled":
                        a.credential_status = "valid"
                    s.add(Observation(id=uid(), account_id=a.id, job_id=j.id, credential_version=j.credential_version, amount=balance, created_at=self.clock()))
                    covered = list(s.scalars(select(Job).where(
                        Job.account_id == a.id, Job.kind == "balance", Job.status == "queued",
                        Job.credential_version == j.credential_version, Job.created_at < j.created_at,
                        Job.available_at <= active_attempt.started_at,
                        ~Job.dedupe_key.like("%:balance:auth_review:%"),
                    )))
                    self.supersede_balance_queries(s, state, covered)
                    self.queue_registration_onboarding(s,state,a)
                elif success and j.kind == "verify":
                    target = s.scalar(select(Resource).where(Resource.kind == "job_target", Resource.name == j.id))
                    target_data = json.loads(target.data) if target else {}
                    model = target_data.get("model", "sandbox-echo")
                    for m in s.scalars(select(ModelState).where(ModelState.account_id == a.id, ModelState.model.in_([model, "*"]))):
                        newer_rejection = select(RequestAttempt.id).join(RequestLog, RequestLog.id == RequestAttempt.request_id).where(
                            RequestAttempt.account_id == a.id,
                            RequestAttempt.credential_version == j.credential_version,
                            RequestAttempt.code == m.reason,
                            RequestAttempt.ended_at >= active_attempt.started_at,
                        )
                        if target_data.get("prior_rejection_ids"):
                            newer_rejection = newer_rejection.where(RequestAttempt.id.not_in(target_data["prior_rejection_ids"]))
                        if m.model != "*":
                            newer_rejection = newer_rejection.where(RequestLog.model == m.model)
                        if s.scalar(newer_rejection.limit(1)):
                            # A probe begun before a newer rejection cannot
                            # prove recovery from that rejection.
                            continue
                        m.credential_version, m.verified_at, m.attempts = j.credential_version, self.clock(), 0
                elif not success:
                    if error == "account_disabled":
                        self.mark_upstream_disabled(s, state, a)
                    elif error == "invalid_credentials":
                        self.observe_auth_failure(s, state, a)
                    if j.kind == "verify":
                        # 复核失败：累计该账号尚未解除的限制的次数。
                        # 后续由 operations.schedule 按 attempts 做指数退避，
                        # 避免上游持续拒绝时每 15 分钟固定空转一次。
                        target = s.scalar(select(Resource).where(Resource.kind == "job_target", Resource.name == j.id))
                        model = json.loads(target.data)["model"] if target else "sandbox-echo"
                        for m in s.scalars(select(ModelState).where(ModelState.account_id == a.id, ModelState.model.in_([model, "*"]), ModelState.verified_at.is_(None))):
                            m.attempts = int(m.attempts or 0) + 1
                    # P1-3: a failed post-sign-in balance is retried soon, not at the next cycle.
                    if j.kind == "balance" and any(prefix in j.dedupe_key for prefix in (":balance:after_checkin:", ":balance:after_invitation:", ":balance:after_onboarding:")) and error != "invalid_credentials" and j.attempt < 6:
                        j.status, j.available_at, j.finished_at = "queued", self.clock() + 300, None
                    if j.kind == "relogin" and error in ACCOUNT_FAILURES:
                        # Only "no SMS received" counts toward the stop; other errors are kept on the job.
                        if error == "no_sms":
                            a.relogin_failures += 1
            if not stale and j.kind=='benefits':
                ledger_name=hashlib.sha256(f'{a.id}:benefits'.encode()).hexdigest()
                ledger=s.scalar(select(Resource).where(Resource.kind=='operation_ledger',Resource.name==ledger_name))
                receipt=json.loads(ledger.data) if ledger else {}
                if receipt.get('job_id')==j.id and receipt.get('credential_version')==j.credential_version and receipt.get('needs_balance'):
                    followup=self.enqueue(s,a,'balance',f'after_benefits:{j.id}')
                    followup.priority=5
                    self.emit(s,state,'job','balance_after_benefits',a.id,followup.id)
            if j.kind=="onboarding":
                self.onboarding_followup(s,state,a,j,j.status)
            self.emit(s, state, "job", j.result_code or j.error_code or j.status, a.id, j.id)

    def retry(self, job_id):
        with self.tx() as (s, state):
            j = s.get(Job, job_id)
            if not j or j.kind not in {"balance", "verify", "renew", "benefits"} or j.status not in {"failed", "uncertain"}:
                raise DomainError("job_not_safely_retryable")
            a = self.account(s, j.account_id)
            if j.kind=='benefits':
                target=s.scalar(select(Resource).where(Resource.kind=='job_target',Resource.name==j.id))
                if target is None:
                    target=Resource(id=uid(),kind='job_target',name=j.id,data='{}',updated_at=self.clock());s.add(target)
                target.data=json.dumps({'reconcile_benefits':True})
            if j.kind == "renew":
                candidate = s.scalar(select(Resource).where(Resource.kind == "renewal_candidate", Resource.name == j.id))
                if not candidate or not candidate.secret or j.credential_version != a.credential_version:
                    raise DomainError("job_not_safely_retryable")
            j.status, j.error_code, j.finished_at = "queued", None, None
            j.credential_version, j.available_at = a.credential_version, self.clock()
            self.emit(s, state, "job", "safe_retry", a.id, j.id)

    def recheck(self, account_id):
        """人工确认后，把「上游停用」的账户重新放回核验队列。

        只把可调度资格放回 pending，不伪造可用性：一次只读余额查询成功才会回到
        候选池；上游仍然拒，新的 job 会带着真实业务码留下证据。
        """
        with self.tx() as (s, state):
            a = self.account(s, account_id)
            if a.credential_status != "disabled":
                raise DomainError("account_not_upstream_disabled")
            a.credential_status, a.updated_at = "pending", self.clock()
            self.emit(s, state, "account", "recheck_requested", a.id)
        return self.plan(account_id, "balance")

    def reconcile_onboarding(self,job_id):
        with self.tx() as (s,state):
            job=s.get(Job,job_id)
            if not job or job.kind!="onboarding" or job.status not in {"uncertain","failed"}:
                raise DomainError("job_not_safely_retryable")
            account=self.account(s,job.account_id)
            if account.platform!="Loomy":
                raise DomainError("job_not_safely_retryable")
            target=s.scalar(select(Resource).where(Resource.kind=="job_target",Resource.name==job.id))
            if target is None:
                target=Resource(id=uid(),kind="job_target",name=job.id,data="{}",updated_at=self.clock())
                s.add(target)
            data=json.loads(target.data)
            data["reconcile_onboarding"]=True
            target.data,target.updated_at=json.dumps(data),self.clock()
            job.status,job.credential_version,job.available_at="queued",account.credential_version,self.clock()
            job.finished_at,job.error_code,job.result_code=None,None,None
            self.emit(s,state,"job","onboarding_readback_queued",account.id,job.id)
            return job.id

    def reconcile_checkin(self,job_id):
        with self.tx() as (s,state):
            job=s.get(Job,job_id)
            if not job or job.kind!="checkin" or job.status not in {"uncertain","failed","superseded"}:
                raise DomainError("job_not_safely_retryable")
            account=self.account(s,job.account_id)
            target=s.scalar(select(Resource).where(Resource.kind=="job_target",Resource.name==job.id))
            if target is None:
                target=Resource(id=uid(),kind="job_target",name=job.id,data="{}",updated_at=self.clock())
                s.add(target)
            target.data=json.dumps({"reconcile_checkin":True})
            job.status,job.credential_version,job.available_at="queued",account.credential_version,self.clock()
            job.finished_at,job.error_code=None,None
            self.emit(s,state,"job","readback_queued",account.id,job.id)

    def finish_login(self, job_id, owner, attempt, secret, verified_identity):
        """Internal adapter receipt, deliberately not exposed as a public API.

        Only an adapter which has verified the returned platform identity may
        start a new failure cycle. Previous attempts remain immutable history.
        """
        if not verified_identity or not secret:
            raise DomainError("verified_credential_receipt_required", 422)
        with self.tx() as (s, state):
            j = s.get(Job, job_id)
            if not j or j.kind not in {"relogin", "renew"} or j.status != "running" or j.owner != owner or j.attempt != attempt or j.lease_until <= self.clock():
                raise DomainError("stale_job_receipt")
            a = self.account(s, j.account_id)
            if j.credential_version != a.credential_version:
                raise DomainError("credential_version_conflict")
            from .native_lifecycle import control
            previous_control=control(s,a)
            a.credential_version += 1
            a.credential_status, a.updated_at = "pending", self.clock()
            if j.kind == "relogin":
                a.relogin_failures = 0
            s.add(Credential(id=uid(), account_id=a.id, version=a.credential_version, ciphertext=self.cipher.encrypt(secret.encode()).decode(), created_at=self.clock()))
            from .native_identity import capture_identity
            try:
                identity_material=json.loads(secret)
            except (ValueError,TypeError):
                identity_material={}
            if isinstance(identity_material,dict):
                capture_identity(a,identity_material)
            lifecycle=s.scalar(select(Resource).where(Resource.kind=='lifecycle_control',Resource.name==a.id))
            if lifecycle and previous_control.get('state')=='independent':
                lifecycle.version+=1
                lifecycle.data=json.dumps({**previous_control,'credential_version':a.credential_version,'version':lifecycle.version,'last_verified_renewal_at':self.clock()})
                lifecycle.updated_at=self.clock()
            j.status, j.result_code, j.finished_at, j.lease_until = "succeeded", "identity_verified", self.clock(), None
            j.finished_revision = state.revision + 1
            record = s.scalar(select(Attempt).where(Attempt.job_id == j.id, Attempt.number == attempt))
            record.outcome, record.finished_at = "succeeded", self.clock()
            candidate = s.scalar(select(Resource).where(Resource.kind == "renewal_candidate", Resource.name == j.id))
            if candidate:
                candidate.data = json.dumps({"account_id": a.id, "source_version": j.credential_version, "status": "activated", "activated_version": a.credential_version})
                candidate.secret, candidate.updated_at = None, self.clock()
            self.after_login(s, state, a, f"credential:{a.credential_version}")
            self.emit(s, state, "credential", "identity_verified", a.id, j.id)

    def after_login(self, s, state, account, suffix):
        """Never repeat an uncertain claim; keep it visible and refresh balance."""
        checkin = "checkin" in self.real_capabilities.get(account.platform, set()) and account.checkin_day != day_at(self.clock())
        if checkin:
            job = self.enqueue(s, account, "checkin")
            if job.status == "queued" or job.status in {"superseded", "failed"} and (job.attempt == 0 or job.error_code == "invalid_credentials"):
                job.status, job.credential_version = "queued", account.credential_version
                job.finished_at, job.error_code, job.result_code = None, None, None
                return job
            if job.status == "running":
                return job
            if job.status in {"failed","uncertain","superseded"}:
                target=s.scalar(select(Resource).where(Resource.kind=="job_target",Resource.name==job.id))
                if target is None:
                    target=Resource(id=uid(),kind="job_target",name=job.id,data="{}",updated_at=self.clock())
                    s.add(target)
                target.data=json.dumps({"reconcile_checkin":True})
                job.status,job.credential_version,job.available_at="queued",account.credential_version,self.clock()
                job.finished_at,job.error_code,job.result_code=None,None,None
                self.emit(s,state,"job","readback_queued",account.id,job.id)
                # Balance is still queued even when the receipt cannot be read.
            self.emit(s, state, "job", "checkin_requires_reconciliation", account.id, job.id)
        return self.enqueue(s, account, "balance", suffix) if "balance" in self.real_capabilities.get(account.platform, set()) or account.sandbox else None

    def block_model(self, account_id, model, reason, credential_version, review_at):
        if reason not in {"permission", "quota", "rate_limit", "needs_verification", "identity", "unknown_403", "network"}:
            raise DomainError("unsupported_block_reason", 422)
        with self.tx() as (s, state):
            a = self.account(s, account_id)
            if credential_version != a.credential_version:
                raise DomainError("credential_version_conflict")
            m = s.scalar(select(ModelState).where(ModelState.account_id == a.id, ModelState.model == model))
            if m is None:
                m = ModelState(id=uid(), account_id=a.id, model=model)
                s.add(m)
            m.reason, m.credential_version, m.review_at, m.verified_at = reason, credential_version, review_at, None
            m.attempts = 0
            self.emit(s, state, "model", reason, a.id)

    def release_model_block(self, account_id, model=None):
        """人工解除账户的模型/账户级限制（唯一能绕过自动复核的出口）。

        自动复核要求「原报错模型真实取得正文」，当下游上游持续拒绝时这个条件
        永远无法满足，账户会被永久锁死。这个方法只做一件事：把人判断为
        「可以再试一次」的限制放回候选池，**不伪造可用性**——
        下一次真实请求若仍被拒，`fail_attempt` 会立刻重新 block。
        """
        with self.tx() as (s, state):
            a = self.account(s, account_id)
            now = self.clock()
            rows = list(s.scalars(select(ModelState).where(
                ModelState.account_id == a.id, ModelState.verified_at.is_(None))))
            if model:
                rows = [m for m in rows if m.model in (model, "*")]
            released = []
            for m in rows:
                released.append({"model": m.model, "reason": m.reason})
                m.verified_at, m.review_at, m.attempts = now, now, 0
                self.emit(s, state, "model", "manually_released", a.id)
            return released

    def reason(self, a, busy, blocked, model="sandbox-echo"):
        if not a.enabled:
            return "manually_disabled"
        if not a.sandbox and "chat" not in self.real_capabilities.get(a.platform, set()):
            return "adapter_unavailable"
        if a.credential_status == "missing":
            return "missing_credential"
        if a.credential_status == "invalid":
            return "needs_relogin"
        if a.credential_status == "disabled":
            # 上游说账号被停用：重登没用，要人工确认后走 recheck。
            return "upstream_disabled"
        if a.credential_status == "suspect":
            return "credential_review"
        if a.credential_status != "valid":
            return "credential_pending"
        for m in sorted((b for b in blocked.get(a.id, ()) if b.reason == 'quota' or b.model in {model,"*"}),key=lambda b: (b.reason != 'quota', b.reason in TEMPORARY_MODEL_BLOCKS)):
            if self.block_active(m, self.clock()):
                return m.reason
        # Only "never observed for this credential" blocks. Age alone does not: a stalled
        # maintenance queue must not take every account out of service within an hour.
        if a.balance is None or a.balance_version != a.credential_version or a.balance_at is None:
            return "balance_pending"
        if a.balance <= 0:
            if not a.sandbox and "checkin" not in self.real_capabilities.get(a.platform,set()):
                return "no_balance"
            return "balance_exhausted" if a.checkin_day == day_at(self.clock()) else "checkin_pending"
        if busy[a.id] >= a.concurrency:
            return "busy"
        return "ready"

    def create_key(self, name, platform, model, models=None):
        if platform not in PLATFORMS:
            raise DomainError("unsupported_platform", 422)
        token = "up_" + secrets.token_urlsafe(32)
        with self.tx() as (s, state):
            k = ApiKey(id=uid(), name=name, platform=platform, model=model, digest=hashlib.sha256(token.encode()).hexdigest(), prefix=token[:10], created_at=self.clock())
            s.add(k)
            if models:
                from .key_policy import DEFAULT, validate
                policy={**DEFAULT,'models':list(dict.fromkeys([model]+models))}
                validate(policy)
                s.add(Resource(id=uid(),kind='key_policy',name=k.id,data=json.dumps(policy),updated_at=self.clock()))
            self.emit(s, state, "api_key", "created")
        return {"id": k.id, "token": token}

    def revoke_key(self, key_id):
        with self.tx() as (s, state):
            k = s.get(ApiKey, key_id)
            if not k:
                raise DomainError("key_not_found", 404)
            k.enabled = False
            k.revoked_at = self.clock()
            self.emit(s, state, "api_key", "revoked")

    def key_models(self, token):
        with self.read() as (s, _):
            key = s.scalar(select(ApiKey).where(ApiKey.digest == hashlib.sha256(token.encode()).hexdigest(), ApiKey.enabled.is_(True)))
            if not key:
                raise DomainError("invalid_api_key", 401)
            from .key_policy import read
            policy=read(s,key)
            catalog=s.scalar(select(Resource).where(Resource.kind=='model_catalog',Resource.name==key.platform))
            observations=json.loads(catalog.data) if catalog else {}
            details={row['id']:row for row in observations.get('model_details',[]) if isinstance(row,dict) and 'id' in row}
            from .dispatch_policy import route_scope
            result=[]
            for name in dict.fromkeys(policy['models']+list(policy['aliases'])):
                resolved=policy['aliases'].get(name,name)
                try:upstream=route_scope(s,key.platform,key.id,name,resolved)['upstream_model']
                except DomainError:continue
                observed=details.get(upstream,{})
                item={"id":name,"object":"model","created":int(key.created_at),"owned_by":key.platform}
                for field in ('display_name','capabilities','capability_sources','context_window','max_output_tokens','reasoning','reasoning_source','only_reasoning'):
                    if field in observed:item[field]=observed[field]
                item['metadata_observed_at']=observations.get('observed_at') if observed else None
                result.append(item)
            return result

    def acquire(self, token, model, excluded=(), request_id=None, conversation=None, client_ip=None, distinguish_busy=False, client_source=None):
        self.expire_request_leases()
        with self.tx(scope=None) as (s, state):
            key = s.scalar(select(ApiKey).where(ApiKey.digest == hashlib.sha256(token.encode()).hexdigest(), ApiKey.enabled.is_(True)).with_for_update())
            if not key:
                raise DomainError("invalid_api_key", 401)
            now = self.clock()
            from .key_policy import read, admit
            policy=read(s,key)
            resolved_model=admit(s,key,policy,model,now,client_ip,request_id)
            authorized_model = resolved_model
            if request_id:
                previous = s.scalar(select(RequestLog).where(RequestLog.id == request_id).with_for_update())
                if previous and (previous.status != 'inflight' or previous.first_content_at is not None or previous.key_id != key.id or previous.platform != key.platform):
                    raise DomainError('request_not_retryable')
            from .dispatch_policy import route_scope, selection_explanation
            scope = route_scope(s, key.platform, key.id, model, resolved_model)
            if request_id and previous and previous.route_version != scope['version']:
                raise DomainError('request_route_changed',409)
            resolved_model = scope['upstream_model']
            platform_accounts = select(Account.id).where(Account.platform == key.platform)
            busy = Counter(dict(s.execute(select(Lease.account_id, func.count()).where(Lease.expires_at > now, Lease.account_id.in_(platform_accounts)).group_by(Lease.account_id)).all()))
            _, blocked = self.routing_index((), s.scalars(select(ModelState).where(ModelState.account_id.in_(platform_accounts), (ModelState.model.in_([resolved_model, '*']) | (ModelState.reason == 'quota')), ModelState.verified_at.is_(None))), now)
            from .dispatch_policy import read as dispatch_settings
            dispatch = dispatch_settings(s, key.platform)
            affinity_ttl = dispatch['affinity_ttl_seconds']
            affinity_id = hashlib.sha256(f"{key.id}:{model}:{conversation}".encode()).hexdigest() if conversation and affinity_ttl > 0 else None
            sticky = s.get(Affinity, affinity_id) if affinity_id else None
            criteria = [Account.platform == key.platform, Account.sandbox.is_(model == "sandbox-echo"), Account.enabled.is_(True), Account.credential_status == 'valid', Account.balance > 0, Account.balance_version == Account.credential_version, Account.balance_at.is_not(None)]
            if scope['account_ids'] is not None:
                criteria.append(Account.id.in_(scope['account_ids']))
            if excluded:
                criteria.append(Account.id.not_in(excluded))
            preferred = s.scalar(select(Account).where(*criteria, Account.id == sticky.account_id)) if sticky and sticky.expires_at > now else None
            ordinary = select(Account).where(*criteria)
            if preferred:
                ordinary = ordinary.where(Account.id != preferred.id)
            from .dispatch_policy import ordering
            candidates = chain([preferred] if preferred else (), s.scalars(ordinary.order_by(*ordering(s, key.platform)).execution_options(yield_per=64)))
            from .proxy_pool import held_forwarding_accounts
            held_exits = held_forwarding_accounts(s, key.platform, now) if model != "sandbox-echo" else set()
            temporarily_busy = fixed_exit_unavailable = False
            for a in candidates:
                if a.id in excluded:
                    continue
                if a.id in held_exits:
                    # Skip before locking an account or recording an attempt. The
                    # gateway still resolves the route at send time for changes
                    # or failures that occur after this admission snapshot.
                    fixed_exit_unavailable |= self.reason(a, busy, blocked, resolved_model) in {"ready", "busy"}
                    continue
                # Different API keys can target the same account. Count after
                # locking its current row, never use the pre-lock pool snapshot.
                a = s.scalar(select(Account).where(Account.id == a.id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
                if a is None:
                    temporarily_busy = True
                    continue
                busy[a.id] = s.scalar(select(func.count()).select_from(Lease).where(Lease.account_id == a.id, Lease.expires_at > self.clock()))
                _, current_blocks = self.routing_index((), s.scalars(select(ModelState).where(ModelState.account_id == a.id, ModelState.verified_at.is_(None))), self.clock())
                blocked[a.id] = current_blocks.get(a.id, ())
                reason = self.reason(a, busy, blocked, resolved_model)
                temporarily_busy |= reason == 'busy'
                if reason != "ready":
                    continue
                request_id, lease_id = request_id or uid(), uid()
                s.add(Lease(id=lease_id, account_id=a.id, request_id=request_id, expires_at=now + REQUEST_LEASE_SECONDS))
                log = s.get(RequestLog, request_id)
                if log:
                    if log.status != "inflight" or log.first_content_at is not None or log.platform != key.platform or log.key_id != key.id or log.model != resolved_model:
                        raise DomainError("request_not_retryable")
                    log.account_id, log.retry_expires_at = a.id, None
                else:
                    s.add(RequestLog(id=request_id, client_source=client_source, platform=a.platform, model=resolved_model, authorized_model=authorized_model, account_id=a.id, key_id=key.id, route_version=scope['version'], started_at=now))
                attempt = RequestAttempt(id=uid(), request_id=request_id, account_id=a.id, credential_version=a.credential_version, started_at=now)
                selection = selection_explanation(a, dispatch, scope, affinity=bool(preferred and a.id == preferred.id), busy=busy[a.id])
                attempt.selection_data = json.dumps(selection)
                s.add(attempt)
                if affinity_id:
                    if not sticky:
                        sticky = Affinity(id=affinity_id, account_id=a.id, expires_at=now + affinity_ttl)
                        s.add(sticky)
                    elif sticky.account_id != a.id or sticky.expires_at <= now:
                        sticky.account_id, sticky.expires_at = a.id, now + affinity_ttl
                    else:
                        # Shortening TTL applies to existing bindings too;
                        # ordinary reuse does not extend the default lifetime.
                        sticky.expires_at = min(sticky.expires_at, now + affinity_ttl)
                a.last_selected = now
                self.emit(s, state, "request", "acquired", a.id, request_id=request_id)
                return {"id": request_id, "account_id": a.id, "lease_id": lease_id, "platform": a.platform, "version": a.credential_version, "sandbox": a.sandbox, "attempt_id": attempt.id, "upstream_model":resolved_model,"client_ip":client_ip,"session": hashlib.sha256(f"{a.id}:{a.credential_version}:{affinity_id or request_id}".encode()).hexdigest(), "queue_wait_seconds": dispatch['queue_wait_seconds']}
            if distinguish_busy and temporarily_busy:
                failure = DomainError('account_concurrency_busy', 503)
                failure.queue_wait_seconds = dispatch['queue_wait_seconds']
                raise failure
            if fixed_exit_unavailable and not request_id:
                raise DomainError("proxy_unavailable", 503)
            raise DomainError("no_available_account", 503)

    def expire_request_leases(self, limit=100):
        with self.tx(scope=None) as (s,state):
            ids=select(Lease.request_id).where(Lease.expires_at<=self.clock())
            records=list(s.scalars(select(RequestLog).where(RequestLog.id.in_(ids)).order_by(RequestLog.id).limit(limit).with_for_update(skip_locked=True)))
            for log in records:
                lease=s.scalar(select(Lease).where(Lease.request_id==log.id).with_for_update().execution_options(populate_existing=True))
                if not lease or lease.expires_at>self.clock(): continue
                if log.status=='inflight':
                    log.status,log.error_code,log.ended_at='failed','lease_expired',self.clock()
                    log.retry_expires_at = None
                    self.emit(s,state,'request','lease_expired',log.account_id,request_id=log.id)
                s.delete(lease)

            # A failed attempt releases its account slot before admission of
            # the next one. The API key still owns the inflight request. If
            # the process died between those steps, no account lease remains
            # to expire it. Check the durable gap deadline under the same lock.
            gaps = list(s.scalars(select(RequestLog).where(
                RequestLog.status == 'inflight', RequestLog.retry_expires_at.is_not(None),
                RequestLog.retry_expires_at <= self.clock(),
            ).order_by(RequestLog.retry_expires_at, RequestLog.id).limit(limit)
              .with_for_update(skip_locked=True).execution_options(populate_existing=True)))
            for log in gaps:
                if s.scalar(select(Lease.id).where(Lease.request_id == log.id)):
                    # Only a gap can expire here; active attempts are handled
                    # by their own lease above (including mixed image revisions).
                    log.retry_expires_at = None
                    continue
                log.status, log.error_code, log.ended_at = 'failed', 'lease_expired', self.clock()
                log.retry_expires_at = None
                self.emit(s, state, 'request', 'lease_expired', log.account_id, request_id=log.id)

    def mark_output(self, request_id, placeholder=False):
        with self.tx(scope=None) as (s, state):
            s.scalar(select(RequestLog).where(RequestLog.id == request_id).with_for_update())
            r = s.get(RequestLog, request_id)
            if not r or r.status != "inflight":
                raise DomainError("request_not_running")
            field = "placeholder_at" if placeholder else "first_content_at"
            if getattr(r, field) is None:
                setattr(r, field, self.clock())
                self.emit(s, state, "request", field, r.account_id, request_id=r.id)

    def end_request(self, request_id, status, error=None):
        with self.tx(scope=None) as (s, state):
            s.scalar(select(RequestLog).where(RequestLog.id == request_id).with_for_update())
            r = s.get(RequestLog, request_id)
            if not r or r.status != "inflight":
                return
            if status == "succeeded" and r.first_content_at is None:
                status, error = "failed", "no_effective_output"
            r.status, r.ended_at, r.retry_expires_at = status, self.clock(), None
            from .gateway import ERROR_CODES as GATEWAY_ERRORS
            r.error_code = error if error in GATEWAY_ERRORS else "internal_error" if error else None
            for attempt in s.scalars(select(RequestAttempt).where(RequestAttempt.request_id == request_id, RequestAttempt.ended_at.is_(None))):
                attempt.ended_at, attempt.code = self.clock(), r.error_code or status
            s.execute(delete(Lease).where(Lease.request_id == r.id))
            self.emit(s, state, "request", error or status, r.account_id, request_id=r.id)

    def credential(self, account_id, version):
        with self.read() as (s, _):
            a = self.account(s, account_id)
            if a.credential_version != version:
                raise DomainError("credential_version_conflict")
            c = s.scalar(select(Credential).where(Credential.account_id == account_id, Credential.version == version))
            if not c:
                raise DomainError("missing_credential", 422)
            try:
                secret = json.loads(self.cipher.decrypt(c.ciphertext.encode()))
            except (ValueError, TypeError):
                raise DomainError("invalid_credential_format", 422) from None
            if not isinstance(secret, dict):
                raise DomainError("invalid_credential_format", 422)
            return secret

    def account_proxy(self, account_id):
        from .proxy_pool import resolve_account_proxy
        return resolve_account_proxy(self, account_id)

    def resources(self, kind):
        with self.read() as (s, _):
            return [{"id": r.id, "name": r.name, "version": r.version, "data": json.loads(r.data), "has_secret": r.secret is not None, "updated_at": r.updated_at} for r in s.scalars(select(Resource).where(Resource.kind == kind).order_by(Resource.updated_at.desc()))]

    def resource_put(self, kind, name, data, secret=None, resource_id=None, expected_version=None):
        with self.tx() as (s, state):
            resource = s.get(Resource, resource_id) if resource_id else None
            if resource_id and (not resource or resource.kind != kind):
                raise DomainError("resource_not_found", 404)
            if resource and resource.version != expected_version:
                raise DomainError("resource_version_conflict")
            if resource is None:
                resource = Resource(id=uid(), kind=kind, name=name, version=1, data="{}", updated_at=self.clock())
                s.add(resource)
            else:
                resource.version += 1
            resource.name, resource.data, resource.updated_at = name, json.dumps(data, ensure_ascii=False), self.clock()
            if secret is not None:
                resource.secret = self.cipher.encrypt(secret.encode()).decode()
            self.emit(s, state, kind, "configuration_saved")
            return resource.id

    def prune(self, event_days=30, batch=5000):
        """Bounded cleanup of growth-only tables. Request, job and receipt history is kept."""
        now = self.clock()
        with self.tx() as (s, _):
            old = select(Event.id).where(Event.created_at < now - event_days * 86400).order_by(Event.id).limit(batch)
            events = s.execute(delete(Event).where(Event.id.in_(old))).rowcount
            stale = select(Affinity.id).where(Affinity.expires_at < now - 86400).limit(batch)
            affinity = s.execute(delete(Affinity).where(Affinity.id.in_(stale))).rowcount
        return {"events": events, "affinity": affinity}

    @staticmethod
    def phone_key(platform, phone):
        """Number ledger key. Per platform: one number may register Loomy and WPS, never Loomy twice."""
        return hashlib.sha256(f"{platform}|{phone}".encode()).hexdigest()

    def record_phone(self, s, platform, phone, source, account_id=None):
        """Index a number already tied to an account (registered, relogged or imported) so a
        newly bought number that matches it is released instead of being sent a code."""
        if not phone:
            return
        key = self.phone_key(platform, str(phone))
        if s.scalar(select(Resource.id).where(Resource.kind == "phone_history", Resource.name == key)) is None:
            s.add(Resource(id=uid(), kind="phone_history", name=key, data=json.dumps({"platform": platform, "source": source, "account_id": account_id, "first_seen": self.clock()}), updated_at=self.clock()))

    def mark_phone_rejected(self, s, platform, phone, reason):
        """Remember a number the platform refused to send a code to.

        Buying the same number back is a certain waste: the platform已明确拒绝这个号码，
        再发一次要么继续被拒，要么干脆不落码。下一次取到同一个号时直接释放，
        把一次会话留给别的号码。
        """
        phone = str(phone or "").strip()
        if not re.fullmatch(r"1\d{10}", phone):
            return
        key = self.phone_key(platform, phone)
        now = self.clock()
        row = s.scalar(select(Resource).where(Resource.kind == "phone_rejected", Resource.name == key))
        if row is None:
            s.add(Resource(id=uid(), kind="phone_rejected", name=key, data=json.dumps({"platform": platform, "reason": reason, "count": 1, "first_seen": now, "last_seen": now}), updated_at=now))
        else:
            data = json.loads(row.data)
            data.update(reason=reason, count=int(data.get("count") or 0) + 1, last_seen=now)
            row.data, row.updated_at = json.dumps(data), now

    def phone_rejected(self, s, platform, phone):
        """True when this exact number was already refused by the platform for this platform."""
        phone = str(phone or "").strip()
        if not phone:
            return False
        return s.scalar(select(Resource.id).where(Resource.kind == "phone_rejected", Resource.name == self.phone_key(platform, phone))) is not None

    def service_heartbeat(self, name, data):
        """Liveness of a separately deployed service; no revision bump, so it does not wake every console."""
        with self.tx() as (s, _):
            row = s.scalar(select(Resource).where(Resource.kind == "service_status", Resource.name == name))
            if row is None:
                row = Resource(id=uid(), kind="service_status", name=name, data="{}", updated_at=self.clock())
                s.add(row)
            row.data, row.updated_at = json.dumps(data, ensure_ascii=False), self.clock()

    def service_status(self, name, stale_after=45):
        with self.read() as (s, _):
            row = s.scalar(select(Resource).where(Resource.kind == "service_status", Resource.name == name))
            if row is None:
                return {"online": False, "seen_at": None}
            return {**json.loads(row.data), "online": self.clock() - row.updated_at <= stale_after, "seen_at": row.updated_at}

    def operation_ledger(self, account_id, key, value=None, fence=None, job=None):
        fence = fence or job
        name = hashlib.sha256(f"{account_id}:{key}".encode()).hexdigest()
        with self.tx() as (s, state):
            self.account(s, account_id)
            record = s.scalar(select(Resource).where(Resource.kind == "operation_ledger", Resource.name == name))
            if fence is not None:
                job = s.get(Job, fence['id'])
                account = self.account(s, account_id)
                if (not job or job.account_id != account_id or job.status != 'running'
                    or job.owner != fence['owner'] or job.attempt != fence['attempt']
                    or (job.lease_until or 0) <= self.clock()
                    or job.credential_version != fence['version']
                    or account.credential_version != fence['version']):
                    raise DomainError('stale_job_receipt')
            if value is None:
                return json.loads(record.data) if record else None
            if record is None:
                record = Resource(id=uid(), kind="operation_ledger", name=name, data="{}", updated_at=self.clock())
                s.add(record)
            record.data, record.updated_at = json.dumps(value), self.clock()
            self.emit(s, state, "operation_ledger", "receipt_saved", account_id)
            return value

    def renewal_candidate(self, job, credential=None):
        with self.tx() as (s, state):
            j = s.scalar(select(Job).where(Job.id == job["id"]).with_for_update())
            if (not j or j.kind != 'renew' or j.status != "running" or j.owner != job["owner"]
                or j.attempt != job["attempt"] or not j.lease_until or j.lease_until <= self.clock()
                or j.account_id != job["account_id"] or j.credential_version != job["version"]):
                raise DomainError("stale_job_receipt")
            account=self.account(s,j.account_id)
            if account.credential_version != j.credential_version:
                raise DomainError('credential_version_conflict')
            row = s.scalar(select(Resource).where(Resource.kind == "renewal_candidate", Resource.name == job["id"]))
            if row:
                data=json.loads(row.data)
                if data.get('account_id') != j.account_id or data.get('source_version') != j.credential_version:
                    raise DomainError('credential_version_conflict')
            if credential is None:
                return json.loads(self.cipher.decrypt(row.secret.encode())) if row and row.secret else None
            if row is None:
                row = Resource(id=uid(), kind="renewal_candidate", name=job["id"], data=json.dumps({"account_id": j.account_id, "source_version": j.credential_version, "status": "pending_validation"}), updated_at=self.clock())
                s.add(row)
            row.secret, row.updated_at = self.cipher.encrypt(json.dumps(credential).encode()).decode(), self.clock()
            self.emit(s, state, "renewal", "candidate_saved", j.account_id, j.id)
            return credential

    def bind_proxy(self, account_id, proxy_id):
        with self.tx() as (s, state):
            self.account(s, account_id)
            if proxy_id:
                node = s.get(Resource, proxy_id)
                if not node or node.kind != "proxy":
                    raise DomainError("proxy_not_found", 404)
            current = s.scalar(select(Resource).where(Resource.kind == "proxy_binding", Resource.name == account_id))
            if current:
                s.delete(current)
            if proxy_id:
                s.add(Resource(id=uid(), kind="proxy_binding", name=account_id, data=json.dumps({"proxy_id": proxy_id}), updated_at=self.clock()))
            self.emit(s, state, "proxy", "binding_updated", account_id)

    def heartbeat_job(self, job):
        with self.tx() as (s, _):
            j = s.get(Job, job["id"])
            if not j or j.status != "running" or j.owner != job["owner"] or j.attempt != job["attempt"] or j.lease_until <= self.clock():
                raise DomainError("stale_job_receipt")
            j.lease_until = self.clock() + 60
            lease = s.scalar(select(Lease).where(Lease.request_id == j.id))
            if lease:
                lease.expires_at = j.lease_until

    def mark_upstream_progress(self, current, stage):
        if stage not in ('upstream_response_at','first_frame_at'):
            raise DomainError('invalid_progress_stage',422)
        with self.tx(scope=None) as (s,state):
            log=s.scalar(select(RequestLog).where(RequestLog.id==current['id']).with_for_update())
            attempt=s.get(RequestAttempt,current['attempt_id'])
            lease=s.get(Lease,current['lease_id'])
            if (not log or log.status!='inflight' or not attempt or attempt.request_id!=log.id
                or attempt.account_id!=current['account_id'] or attempt.credential_version!=current['version']
                or attempt.ended_at is not None or not lease or lease.request_id!=log.id
                or lease.account_id!=attempt.account_id or lease.expires_at<=self.clock()):
                raise DomainError('stale_request_receipt')
            if getattr(attempt,stage) is None:
                setattr(attempt,stage,self.clock())
                self.emit(s,state,'request',stage,attempt.account_id,request_id=log.id)

    def heartbeat_request(self, request_id):
        with self.tx(scope=None) as (s, _):
            s.scalar(select(RequestLog).where(RequestLog.id == request_id).with_for_update())
            lease = s.scalar(select(Lease).where(Lease.request_id == request_id))
            if not lease or lease.expires_at <= self.clock():
                raise DomainError("request_lease_lost")
            lease.expires_at = self.clock() + REQUEST_LEASE_SECONDS

    def record_usage(self, request_id, usage):
        with self.tx(scope=None) as (s, state):
            s.scalar(select(RequestLog).where(RequestLog.id == request_id).with_for_update())
            log = s.get(RequestLog, request_id)
            if not log:
                return
            prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
            details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            cache = details.get("cached_tokens", usage.get("cache_read_input_tokens"))
            if isinstance(prompt, int) and prompt >= 0:
                log.input_tokens = prompt
            if isinstance(cache, int) and cache >= 0:
                log.cached_tokens = cache
            for field,value in {'output_tokens':usage.get('completion_tokens',usage.get('output_tokens')),'total_tokens':usage.get('total_tokens'),'reasoning_tokens':(usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}).get('reasoning_tokens')}.items():
                if isinstance(value,int) and not isinstance(value,bool) and value>=0:
                    setattr(log,field,value)
            self.emit(s, state, "request", "usage_received", log.account_id, request_id=log.id)

    def fail_attempt(self, current, error):
        with self.tx(scope=None) as (s, state):
            log = s.scalar(select(RequestLog).where(RequestLog.id == current["id"]).with_for_update())
            attempt = s.get(RequestAttempt, current["attempt_id"])
            if not log or log.status != 'inflight' or not attempt or attempt.ended_at is not None:
                return
            attempt.ended_at, attempt.code = self.clock(), error.code
            log.retry_expires_at = attempt.ended_at + REQUEST_LEASE_SECONDS
            s.execute(delete(Lease).where(Lease.request_id == current["id"]))
            a = self.account(s, current["account_id"])
            if a.credential_version == current["version"]:
                if error.code == "account_disabled":
                    # 上游在对话里也回 100003：立刻从调度里摘掉，别让下一个请求再撞它。
                    # 不进 model block —— 不是「这个模型没权限」，是整个账号停了。
                    self.mark_upstream_disabled(s, state, a)
                elif error.code == "invalid_credentials":
                    self.observe_auth_failure(s, state, a)
                # Quota pauses this account across models, per user policy.
                # Keep the last actual balance and the rejected model as the
                # recovery target; another model's answer cannot clear it.
                reason = error.code if error.code in {"identity", "permission", "quota", "unknown_403", "rate_limit"} else "network" if error.code in {"network","upstream_5xx","upstream_timeout","response_read_failed","truncated_stream","no_effective_output","empty_response"} and error.retryable else None
                if reason:
                    model = "*" if reason == "identity" else s.get(RequestLog, current["id"]).model
                    m = s.scalar(select(ModelState).where(ModelState.account_id == a.id, ModelState.model == model))
                    if m is None:
                        m = ModelState(id=uid(), account_id=a.id, model=model)
                        s.add(m)
                    # Concurrent requests were admitted before the pause. A
                    # later network/rate/permission result must not downgrade
                    # an unresolved quota into a timed or model-only block.
                    if not (m.reason == "quota" and m.verified_at is None and reason != "quota"):
                        m.reason, m.credential_version, m.verified_at = reason, a.credential_version, None
                        # 新的拒绝事实：退避重新从第一档开始（后续由 schedule 递增）。
                        m.attempts = 0
                        # 「账户对这个模型不可用」不是瞬时抖动，30 秒后重排只会让
                        # 复核任务立刻空转一次。给一个不低于 recovery_interval 的起点。
                        delay = error.retry_after or 30
                        if reason in {"quota", "permission", "identity", "unknown_403"}:
                            delay = max(delay, 900)
                        m.review_at = self.clock() + delay
            self.emit(s, state, "request_attempt", error.code, a.id, request_id=current["id"])

    def fail_delivery(self, request_id, code):
        """Protocol serialization can fail after an upstream finishes successfully."""
        allowed = {"invalid_tool_arguments", "invalid_stream_frame", "response_too_large", "unsupported_protocol_field",'response_context_too_large','response_history_unavailable','response_history_conflict'}
        with self.tx(scope=None) as (s, state):
            s.scalar(select(RequestLog).where(RequestLog.id == request_id).with_for_update())
            log = s.get(RequestLog, request_id)
            if not log:
                return
            log.status, log.error_code, log.ended_at = "failed", code if code in allowed else "internal_error", self.clock()
            log.retry_expires_at = None
            s.execute(delete(Lease).where(Lease.request_id == request_id))
            self.emit(s, state, "request", log.error_code, log.account_id, request_id=log.id)

    def snapshot(self,capacity=64):
        with self.read() as (s, state):
            now, day = self.clock(), day_at(self.clock())
            busy, blocked = self.routing_index(s.scalars(select(Lease)), s.scalars(select(ModelState).where(ModelState.verified_at.is_(None))), now)
            accounts = []
            # A failed query newer than the stored observation must not look like a fresh balance (or a zero).
            balance_errors = {}
            # A midnight boundary must not erase the last failed query. Rank in
            # SQL so a long history does not need to be loaded into the panel.
            latest_balance = select(Job.id, func.row_number().over(partition_by=Job.account_id, order_by=(Job.finished_at.desc(), Job.finished_revision.desc().nulls_last(), Job.created_at.desc(), Job.id.desc())).label("rank")).where(Job.kind == "balance", Job.status.in_(["succeeded", "failed"])).subquery()
            for account_id, status, error_code in s.execute(select(Job.account_id, Job.status, Job.error_code).join(latest_balance, Job.id == latest_balance.c.id).where(latest_balance.c.rank == 1)):
                balance_errors[account_id] = error_code if status == "failed" else None
            from .key_policy import decode
            all_keys = list(s.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())))
            raw_policies = dict(s.execute(select(Resource.name, Resource.data).where(Resource.kind == "key_policy")).all())
            policies = {key.id: decode(key, raw_policies.get(key.id)) for key in all_keys}
            served = {}
            for k in all_keys:
                if k.enabled and k.model != "sandbox-echo":
                    served.setdefault(k.platform, set()).update(policies[k.id]['models'])
            for a in s.scalars(select(Account).order_by(Account.created_at, Account.external_id, Account.id)):
                models = ["sandbox-echo"] if a.sandbox else sorted(served.get(a.platform, set()))
                readiness = {model: self.reason(a, busy, blocked, model) for model in models}
                status = "ready" if "ready" in readiness.values() else "busy" if "busy" in readiness.values() else next(iter(readiness.values()), self.reason(a, busy, blocked))
                if not a.sandbox and not models and status == "ready":
                    status = "no_route"
                accounts.append({"id": a.id, "platform": a.platform, "label": a.label, "sandbox": a.sandbox, "enabled": a.enabled,
                    "status": status, "model_readiness": readiness, "model_blocks": [{"model": b.model, "scope": "account" if b.reason == "quota" or b.model == "*" else "model", "reason": b.reason, "review_at": b.review_at, "verified_at": b.verified_at} for b in blocked.get(a.id, ())], "credential_version": a.credential_version, "credential_status": a.credential_status,
                    "balance": a.balance, "balance_at": a.balance_at, "balance_version": a.balance_version,
                    "balance_error": balance_errors.get(a.id), "checkin_day": a.checkin_day, "relogin_day": a.relogin_day,
                    "relogin_failures": a.relogin_failures, "concurrency": a.concurrency,
                    "busy": busy[a.id], "updated_at": a.updated_at})
            reasons = dict(Counter(a["status"] for a in accounts))
            # Aggregate the full business day in SQL; only the recent 200 rows
            # below need ORM objects. Counts remain independent of pagination.
            today_counts = {kind: {} for kind in PRIORITY}
            for kind, status, count in s.execute(select(Job.kind, Job.status, func.count()).where(Job.business_day == day).group_by(Job.kind, Job.status)):
                if kind in today_counts:
                    today_counts[kind][status] = count
            after_checkin = dict(s.execute(select(Job.status, func.count()).where(
                Job.business_day == day, Job.kind == "balance",
                Job.dedupe_key.contains(":balance:after_checkin:", autoescape=True),
            ).group_by(Job.status)).all())
            jobs = list(s.scalars(select(Job).order_by(Job.created_at.desc()).limit(200)))
            all_job_counts = {status: count for status, count in s.execute(select(Job.status, func.count()).group_by(Job.status))}
            active_jobs=s.scalar(select(func.count()).select_from(Job).where(Job.status=="running",Job.lease_until>now))
            active_registration=len(self.execution_leases(s))
            registration_counts=dict(Counter(json.loads(r.data)["status"] for r in s.scalars(select(Resource).where(Resource.kind=="registration"))))
            job_data = [{k: getattr(j, k) for k in ("id", "account_id", "kind", "business_day", "credential_version", "status", "created_at", "available_at", "finished_at", "attempt", "lease_until", "result_code", "error_code")} for j in jobs]
            candidates = {r.name for r in s.scalars(select(Resource).where(Resource.kind == "renewal_candidate")) if r.secret}
            for job in job_data:
                job["has_renewal_candidate"] = job["id"] in candidates
            keys = [{k: getattr(a, k) for k in ("id", "name", "platform", "model", "prefix", "enabled", "created_at")} for a in all_keys]
            for key in keys:
                key['policy'] = policies[key['id']]
            events = [{k: getattr(e, k) for k in ("id", "revision", "kind", "code", "account_id", "job_id", "created_at")} for e in s.scalars(select(Event).order_by(Event.id.desc()).limit(100))]
            requests = [{k: getattr(r, k) for k in ("id", "platform", "model", "account_id", "started_at", "ended_at", "placeholder_at", "first_content_at", "status", "error_code", "input_tokens", "cached_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "key_id")} for r in s.scalars(select(RequestLog).order_by(RequestLog.started_at.desc()).limit(200))]
            request_ids = [r["id"] for r in requests]
            attempts = [{k: getattr(r, k) for k in ("id", "request_id", "account_id", "credential_version", "started_at", "ended_at", "code")} for r in s.scalars(select(RequestAttempt).where(RequestAttempt.request_id.in_(request_ids)).order_by(RequestAttempt.started_at))] if request_ids else []
            return {"revision": state.revision, "as_of": now, "business_day": day, "platforms": PLATFORMS, "accounts": accounts,
                "totals": {"total": len(accounts), "ready": reasons.get("ready", 0), "busy": reasons.get("busy", 0), "unavailable": len(accounts)-reasons.get("ready", 0)-reasons.get("busy", 0)},
                "reasons": reasons, "jobs": job_data, "job_counts": all_job_counts,
                "execution":{"maintenance_active":active_jobs,"registration_active":active_registration,"total_active":active_jobs+active_registration,"capacity":self.execution_capacity(s,capacity)},"registration_counts":registration_counts,
                "today": today_counts,
                "after_checkin_balance": after_checkin,
                "keys": keys, "events": events, "requests": requests, "request_attempts": attempts, "request_window": "最近 200 条请求（按开始时间）",
                "capabilities": {p: {"real_adapter": bool(self.real_capabilities.get(p)), "live_verified": False} for p in PLATFORMS}}
