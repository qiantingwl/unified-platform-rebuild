from sqlalchemy import Boolean, Float, Index, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class State(Base):
    __tablename__ = "schema_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    schema_version: Mapped[int] = mapped_column(default=1)
    revision: Mapped[int] = mapped_column(default=0)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "external_id"), Index('ix_accounts_route_order','platform','sandbox','last_selected','id'),Index('ix_accounts_directory','platform','created_at','id'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(100))
    original_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    credential_version: Mapped[int] = mapped_column(default=0)
    credential_status: Mapped[str] = mapped_column(String(30), default="missing")
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_version: Mapped[int] = mapped_column(default=0)
    checkin_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    relogin_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    relogin_failures: Mapped[int] = mapped_column(default=0)
    concurrency: Mapped[int] = mapped_column(default=1)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    balance_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_checkin_enabled: Mapped[bool] = mapped_column(Boolean,default=True,server_default="true")
    daily_checkin_minute: Mapped[int | None] = mapped_column(Integer,nullable=True)
    last_selected: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class Credential(Base):
    __tablename__ = "credential_versions"
    __table_args__ = (UniqueConstraint("account_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index('ix_jobs_claim_order','status','priority','created_at','id'),Index('ix_jobs_expired','status','lease_until'),Index('ix_jobs_account_kind_finished','account_id','kind','finished_at','created_at'),Index('ix_jobs_directory','created_at','id'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(150), unique=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    business_day: Mapped[str] = mapped_column(String(10), index=True)
    credential_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempt: Mapped[int] = mapped_column(default=0)
    result_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)


class Attempt(Base):
    __tablename__ = "job_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    number: Mapped[int] = mapped_column(Integer)
    owner: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome: Mapped[str] = mapped_column(String(40), default="running")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(60))
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float)


class Receipt(Base):
    __tablename__ = "checkin_receipts"
    __table_args__ = (UniqueConstraint("account_id", "business_day"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"))
    business_day: Mapped[str] = mapped_column(String(10))
    job_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float)


class Observation(Base):
    __tablename__ = "balance_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"))
    job_id: Mapped[str] = mapped_column(String(36), unique=True)
    credential_version: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float)


class ModelState(Base):
    __tablename__ = "account_model_states"
    __table_args__ = (UniqueConstraint("account_id", "model"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), index=True)
    model: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(40))
    credential_version: Mapped[int] = mapped_column(Integer)
    review_at: Mapped[float] = mapped_column(Float)
    verified_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 复核尝试次数。用于指数退避：上游持续拒绝时不再每 15 分钟空转一次。
    #: 复核成功（verified_at 落值）或重新 block 时归零。
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Lease(Base):
    __tablename__ = "runtime_leases"
    __table_args__ = (Index('ix_leases_account_active','account_id','expires_at'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(100))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(12))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class RequestLog(Base):
    __tablename__ = "request_logs"
    __table_args__ = (
        Index('ix_request_logs_platform_time','platform','started_at','id'),
        Index('ix_request_logs_end_time','ended_at','id'),
        Index('ix_request_logs_account_time','account_id','started_at','id'),
        Index('ix_request_logs_retry_expiry', 'retry_expires_at', 'id',
              postgresql_where=text("retry_expires_at IS NOT NULL"),
              sqlite_where=text("retry_expires_at IS NOT NULL")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(100))
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"))
    started_at: Mapped[float] = mapped_column(Float, index=True)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    retry_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    placeholder_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_content_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="inflight")
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    route_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authorized_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_source: Mapped[str | None] = mapped_column(String(40), nullable=True,index=True)


class RequestAttempt(Base):
    __tablename__ = "request_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    account_id: Mapped[str] = mapped_column(String(36))
    credential_version: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[float] = mapped_column(Float)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    selection_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_response_at: Mapped[float | None] = mapped_column(Float,nullable=True)
    first_frame_at: Mapped[float | None] = mapped_column(Float,nullable=True)


class Affinity(Base):
    __tablename__ = "conversation_affinity"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[float] = mapped_column(Float)


class Resource(Base):
    """Versioned nonsecret configuration; private payload separately encrypted."""
    __tablename__ = "resources"
    __table_args__ = (Index("ix_resources_kind_name", "kind", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[str] = mapped_column(Text, default="{}")
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float)
