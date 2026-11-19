"""Platform-owned model mapping and account groups; never cross-platform fallback."""
import json

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, String, Text, Integer, Float, delete, UniqueConstraint, literal, cast, or_
from sqlalchemy.orm import Mapped, mapped_column

from .models import Account, Resource, Base
from .store import DomainError, PLATFORMS, uid


class CatalogEntry(Base):
    __tablename__ = 'forward_model_catalog'
    __table_args__ = (UniqueConstraint('platform', 'model'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(100), index=True)
    display_name: Mapped[str] = mapped_column(String(200), default='')
    details: Mapped[str] = mapped_column(Text)
    catalog_version: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


def refresh_index(store, session, platform=None):
    query = select(Resource).where(Resource.kind == 'model_catalog')
    if platform:
        query = query.where(Resource.name == platform)
    for row in session.scalars(query):
        indexed = session.scalar(select(func.max(CatalogEntry.catalog_version)).where(CatalogEntry.platform == row.name))
        if indexed == row.version:
            continue
        # Lock the provider resource, never the full account directory. Recheck
        # after the lock so two concurrent refreshes cannot duplicate rows.
        session.refresh(row, with_for_update=True)
        indexed = session.scalar(select(func.max(CatalogEntry.catalog_version)).where(CatalogEntry.platform == row.name))
        if indexed == row.version:
            continue
        data = json.loads(row.data)
        details = {d['id']: d for d in data.get('model_details', [])}
        session.execute(delete(CatalogEntry).where(CatalogEntry.platform == row.name))
        for identity in dict.fromkeys(data.get('models', [])):
            detail = details.get(identity, {'id': identity, 'capabilities': {}})
            session.add(CatalogEntry(id=uid(), platform=row.name, model=identity,
                                    display_name=detail.get('display_name', ''), details=json.dumps(detail),
                                    catalog_version=row.version, observed_at=data.get('observed_at')))
    session.flush()


class Group(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field(min_length=1, max_length=100)
    account_ids: list[str] = Field(default_factory=list, max_length=50000)


class Mapping(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = Field(min_length=1, max_length=100, pattern=r'^\S+$')
    upstream_model: str = Field(min_length=1, max_length=100, pattern=r'^\S+$')
    group_id: str | None = None
    enabled: bool = True


class RoutesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)
    groups: list[Group] = Field(default_factory=list, max_length=200)
    mappings: list[Mapping] = Field(default_factory=list, max_length=2000)


def read(session, platform):
    row = session.scalar(select(Resource).where(Resource.kind == 'model_routes', Resource.name == platform))
    return {'platform': platform, 'version': row.version if row else 0,
            **(json.loads(row.data) if row else {'groups': [], 'mappings': []})}


def route_scope(session, platform, key_id, model, resolved_model):
    config = read(session, platform)
    mapping = next((m for m in config['mappings'] if m['model'] == resolved_model), None)
    if mapping is None:
        return {'upstream_model': resolved_model, 'account_ids': None, 'group_id': None, 'version': config['version']}
    if not mapping['enabled']:
        raise DomainError('model_not_found', 404)
    group = next((g for g in config['groups'] if g['id'] == mapping['group_id']), None)
    if mapping['group_id'] is not None and group is None:
        raise DomainError('invalid_model_route', 503)
    return {'upstream_model': mapping['upstream_model'], 'account_ids': group['account_ids'] if group else None,
            'group_id': mapping['group_id'], 'version': config['version']}


def save(store, platform, body):
    if platform not in PLATFORMS:
        raise DomainError('unsupported_platform', 422)
    groups = [g.model_dump() for g in body.groups]
    mappings = [m.model_dump() for m in body.mappings]
    ids = {g['id'] for g in groups}
    if len(ids) != len(groups) or len({m['model'] for m in mappings}) != len(mappings):
        raise DomainError('duplicate_model_route', 422)
    if any(m['group_id'] is not None and m['group_id'] not in ids for m in mappings):
        raise DomainError('model_route_group_not_found', 422)
    account_ids = {a for g in groups for a in g['account_ids']}
    with store.tx() as (session, state):
        # Resolve every member on the server; a caller cannot route into another
        # platform by smuggling an otherwise valid account identity.
        found = set()
        ordered = sorted(account_ids)
        for start in range(0, len(ordered), 500):
            found.update(session.scalars(select(Account.id).where(Account.platform == platform, Account.id.in_(ordered[start:start+500]))))
        if found != account_ids:
            raise DomainError('model_route_account_platform_mismatch', 422)
        row = session.scalar(select(Resource).where(Resource.kind == 'model_routes', Resource.name == platform))
        if body.expected_version != (row.version if row else 0):
            raise DomainError('resource_version_conflict', 409)
        if row is None:
            row = Resource(id=uid(), kind='model_routes', name=platform, version=0, data='{}', updated_at=store.clock())
            session.add(row)
        for group in groups:
            group['account_ids'] = list(dict.fromkeys(group['account_ids']))
        row.data = json.dumps({'groups': groups, 'mappings': mappings}, ensure_ascii=False)
        row.version += 1
        row.updated_at = store.clock()
        store.emit(session, state, 'model_routes', 'configuration_saved')
        return {'id': row.id, 'version': row.version}


def mount_model_routes(app, store):
    CatalogEntry.__table__.create(store.engine, checkfirst=True)
    @app.get('/api/model-routes/{platform}')
    def get_routes(platform: str):
        if platform not in PLATFORMS:
            raise DomainError('unsupported_platform', 422)
        with store.read() as (session, _):
            return read(session, platform)

    @app.put('/api/model-routes/{platform}')
    def put_routes(platform: str, body: RoutesInput):
        return save(store, platform, body)

    @app.get('/api/model-routes/{platform}/groups/{group_id}/accounts')
    def group_accounts(platform: str, group_id: str, q: str = Query(default='', max_length=100),
                       page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=200)):
        if platform not in PLATFORMS:
            raise DomainError('unsupported_platform', 422)
        from .account_directory import identity
        with store.read() as (session, _):
            config = read(session, platform)
            group = next((g for g in config['groups'] if g['id'] == group_id), None)
            if group is None:
                raise DomainError('model_route_group_not_found', 404)
            # One bound JSON array keeps even 50k members below driver bind
            # limits. Only the requested account page is materialized in Python.
            encoded = literal(json.dumps(group['account_ids']))
            if session.bind.dialect.name == 'postgresql':
                from sqlalchemy.dialects.postgresql import JSONB
                members = func.jsonb_array_elements_text(cast(encoded, JSONB)).column_valued('member_id')
                member_ids = select(members)
            else:
                members = func.json_each(encoded).table_valued('value')
                member_ids = select(members.c.value)
            clauses = [Account.platform == platform, Account.id.in_(member_ids)]
            if q:
                clauses.append(or_(*(field.icontains(q, autoescape=True) for field in
                                     (Account.id, Account.label, Account.original_label, Account.external_id))))
            total = session.scalar(select(func.count()).select_from(Account).where(*clauses))
            rows = session.scalars(select(Account).where(*clauses).order_by(Account.created_at, Account.id)
                                   .offset((page-1)*page_size).limit(page_size))
            items = []
            for account in rows:
                safe = identity(account)
                items.append({'id': account.id, 'platform': account.platform,
                              'display_name': safe['display_name'], 'identity_hint': safe['identity_hint']})
            return {'items': items, 'total': total, 'page': page, 'page_size': page_size}

    @app.get('/api/model-directory')
    def directory(platform: str | None = None, q: str = Query(default='', max_length=100),
                  page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200)):
        if platform is not None and platform not in PLATFORMS:
            raise DomainError('unsupported_platform', 422)
        with store.tx() as (session, _):
            refresh_index(store, session, platform)
            clauses = []
            if platform:
                clauses.append(CatalogEntry.platform == platform)
            if q:
                escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                clauses.append(CatalogEntry.model.ilike('%'+escaped+'%', escape='\\') |
                               CatalogEntry.display_name.ilike('%'+escaped+'%', escape='\\'))
            total = session.scalar(select(func.count()).select_from(CatalogEntry).where(*clauses))
            rows = session.scalars(select(CatalogEntry).where(*clauses).order_by(CatalogEntry.platform, CatalogEntry.model)
                                   .offset((page-1)*page_size).limit(page_size))
            items = [{'platform': r.platform, **json.loads(r.details), 'observed_at': r.observed_at,
                      'catalog_version': r.catalog_version} for r in rows]
            return {'items': items, 'total': total,
                    'page': page, 'page_size': page_size}
