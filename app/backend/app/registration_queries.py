"""Database filtered registration queries; never decrypt historical materials."""
import json
from sqlalchemy import JSON, Float, String, Index, case, cast, func, or_, select, literal_column
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.elements import Grouping
from .models import Resource
from sqlalchemy.orm import aliased

TERMINAL = ("succeeded", "failed", "cancelled")


def field(session, key):
    if key not in {"account_id", "status", "result_unresolved", "expires_at", "batch_id", "platform", "mode", "created_at", "finished_at"}:
        raise ValueError("unsupported registration query field")
    # SQLite CAST(text AS JSON) is numeric affinity, not JSON parsing.
    if session.get_bind().dialect.name == "sqlite":
        return case((func.json_valid(Resource.data),
            func.json_extract(Resource.data, literal_column("'$." + key + "'"))))
    # Whitelisted keys are SQL literals so independently built expressions
    # remain identical in PostgreSQL SELECT/GROUP BY and expression indexes.
    registration_kinds = Resource.kind.in_([literal_column("'registration'"), literal_column("'registration_batch'")])
    # CASE also fences casts during sequential scans: the optimizer may reorder
    # WHERE predicates, while unrelated resources can contain damaged JSON.
    # PostgreSQL index keys require parentheses around CASE expressions.
    return Grouping(case((registration_kinds,
        cast(Resource.data, JSON)[literal_column("'" + key + "'")].as_string())))


def ensure_registration_indexes(connection):
    """Called by the shared startup migration, with its existing connection."""
    class Bound:
        def get_bind(self):
            return connection
    bound = Bound()
    specifications = {
        "ix_resources_registration_account": [Resource.kind, field(bound, "account_id"), Resource.updated_at, Resource.id],
        "ix_resources_registration_status": [Resource.kind, field(bound, "status"), Resource.updated_at, Resource.id],
        "ix_resources_registration_expiry": [Resource.kind, field(bound, "status"), cast(field(bound, "expires_at"), Float)],
        "ix_resources_registration_batch": [Resource.kind, field(bound, "batch_id"), field(bound, "status"), Resource.id],
    }
    for name, expressions in specifications.items():
        # v1 indexed every Resource and parsed unrelated data as registration
        # JSON. Replace it explicitly; IF NOT EXISTS would preserve that bug.
        connection.exec_driver_sql("DROP INDEX IF EXISTS " + name)
        registration_only = Resource.kind == literal_column("'registration'")
        index = Index(name + "_v2", *expressions,
            sqlite_where=registration_only, postgresql_where=registration_only)
        try:
            connection.execute(CreateIndex(index, if_not_exists=True))
        finally:
            # Dialect-specific expressions must not leak into a later engine's
            # metadata.create_all (including SQLite/Postgres validation runs).
            Resource.__table__.indexes.discard(index)


def registrations(session, account_id=None, pending=False):
    query = select(Resource).where(Resource.kind == "registration")
    if account_id is not None:
        query = query.where(field(session, "account_id") == account_id)
    if pending:
        query = query.where(or_(field(session, "status").not_in(TERMINAL),
                                cast(field(session, "result_unresolved"), String).in_(["1", "true"])))
    return query


def public(row):
    data = json.loads(row.data)
    for key in ("operation", "execution_owner"):
        data.pop(key, None)
    return {"id": row.id, "name": row.name, "kind": row.kind,
            "data": data, "version": row.version, "updated_at": row.updated_at}


def page(store, page=1, page_size=25, platform=None, status=None, account_id=None, q=None):
    with store.read() as (s, _):
        query = registrations(s, account_id)
        if platform:
            query = query.where(field(s, "platform") == platform)
        if status:
            query = query.where(field(s, "status") == status)
        if q:
            query = query.where(or_(Resource.name.contains(q, autoescape=True), Resource.id == q))
        total = s.scalar(select(func.count()).select_from(query.subquery()))
        rows = s.scalars(query.order_by(Resource.updated_at.desc(), Resource.id.desc())
                         .offset((page-1)*page_size).limit(page_size))
        return {"sessions": [public(row) for row in rows], "total": total,
                "page": page, "page_size": page_size}


def recovery_candidates(session, now):
    status = field(session, "status")
    return registrations(session).where(or_(status.in_(["sending", "verifying"]),
        status.like("waiting_%") & (cast(field(session, "expires_at"), Float) <= now)))


def runnable(store):
    """Open registrations plus terminal orders still requiring release.

    Provider order status is durable; a restart does not rescan completed history.
    """
    with store.read() as (s, _):
        lease = aliased(Resource)
        if s.get_bind().dialect.name == "sqlite":
            lease_status = func.json_extract(lease.data, literal_column("'$.status'"))
        else:
            lease_status = cast(lease.data, JSON)[literal_column("'status'")].as_string()
        release = select(lease.id).where(lease.kind == "sms_lease", lease.name == Resource.id,
            lease_status.not_in(["retired", "provider_auto_release"])).exists()
        query = registrations(s).where(or_(field(s, "status").not_in(TERMINAL), release))
        return [public(row) for row in s.scalars(query.order_by(Resource.updated_at, Resource.id))]
