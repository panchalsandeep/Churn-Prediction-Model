"""Persistence (SQLAlchemy Core). SQLite by default; set DATABASE_URL for Postgres.

Everything is keyed by workspace_id, so accounts never see each other's data.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from . import config

metadata = sa.MetaData()

settings_t = sa.Table(
    'workspace_settings', metadata,
    sa.Column('workspace_id', sa.String(80), primary_key=True),
    sa.Column('settings', sa.Text, nullable=False),
    sa.Column('updated_at', sa.Float, nullable=False),
)

models_t = sa.Table(
    'models', metadata,
    sa.Column('id', sa.String(32), primary_key=True),
    sa.Column('workspace_id', sa.String(80), nullable=False, index=True),
    sa.Column('created_at', sa.Float, nullable=False),
    sa.Column('algorithm', sa.String(40)),
    sa.Column('status', sa.String(16), nullable=False),          # champion | challenger | archived
    sa.Column('pr_auc', sa.Float),
    sa.Column('roc_auc', sa.Float),
    sa.Column('schema_signature', sa.String(24)),
    sa.Column('artifact_path', sa.Text),
    sa.Column('data_path', sa.Text),
    sa.Column('meta', sa.Text, nullable=False),                   # JSON: evaluation, comparison, cohorts...
)

customers_t = sa.Table(
    'customers', metadata,
    sa.Column('workspace_id', sa.String(80), primary_key=True),
    sa.Column('customer_id', sa.String(120), primary_key=True),
    sa.Column('model_id', sa.String(32), nullable=False),
    sa.Column('prob', sa.Float, nullable=False, index=True),
    sa.Column('prev_prob', sa.Float),
    sa.Column('tier', sa.String(8), nullable=False),
    sa.Column('revenue', sa.Float),
    sa.Column('tenure', sa.Float),
    sa.Column('activity', sa.Float),
    sa.Column('expected_loss', sa.Float, index=True),
    sa.Column('actual', sa.Integer),
    sa.Column('top_driver', sa.String(120)),
)
# Wide (encrypted) per-customer blobs live apart from the hot table; read by key only.
details_t = sa.Table(
    'customer_details', metadata,
    sa.Column('workspace_id', sa.String(80), primary_key=True),
    sa.Column('customer_id', sa.String(120), primary_key=True),
    sa.Column('reasons', sa.Text),
    sa.Column('attrs', sa.Text),
)
sa.Index('ix_customers_ws_prob', customers_t.c.workspace_id, customers_t.c.prob)
sa.Index('ix_customers_ws_tier_prob', customers_t.c.workspace_id, customers_t.c.tier, customers_t.c.prob)
sa.Index('ix_customers_ws_loss', customers_t.c.workspace_id, customers_t.c.expected_loss)

alerts_t = sa.Table(
    'alerts', metadata,
    sa.Column('id', sa.String(32), primary_key=True),
    sa.Column('workspace_id', sa.String(80), nullable=False, index=True),
    sa.Column('customer_id', sa.String(120), nullable=False),
    sa.Column('rule', sa.String(40), nullable=False),
    sa.Column('severity', sa.String(8), nullable=False),
    sa.Column('title', sa.String(200), nullable=False),
    sa.Column('details', sa.Text),
    sa.Column('signals', sa.Text),
    sa.Column('status', sa.String(16), nullable=False),           # Open | Acknowledged | Resolved
    sa.Column('assignee', sa.String(120)),
    sa.Column('note', sa.Text),
    sa.Column('expected_loss', sa.Float),
    sa.Column('created_at', sa.Float, nullable=False),
    sa.Column('updated_at', sa.Float, nullable=False),
)

interventions_t = sa.Table(
    'interventions', metadata,
    sa.Column('id', sa.String(32), primary_key=True),
    sa.Column('workspace_id', sa.String(80), nullable=False, index=True),
    sa.Column('customer_id', sa.String(120), nullable=False, index=True),
    sa.Column('kind', sa.String(40), nullable=False),
    sa.Column('notes', sa.Text),
    sa.Column('actor', sa.String(120)),
    sa.Column('risk_at_action', sa.Float),
    sa.Column('outcome', sa.String(16), nullable=False),          # pending | saved | churned | no_effect
    sa.Column('created_at', sa.Float, nullable=False),
    sa.Column('updated_at', sa.Float, nullable=False),
)

jobs_t = sa.Table(
    'jobs', metadata,
    sa.Column('id', sa.String(32), primary_key=True),
    sa.Column('workspace_id', sa.String(80), nullable=False, index=True),
    sa.Column('kind', sa.String(24), nullable=False),
    sa.Column('status', sa.String(12), nullable=False),           # queued | running | done | error
    sa.Column('stage', sa.String(80)),
    sa.Column('progress', sa.Integer, nullable=False),
    sa.Column('error', sa.Text),
    sa.Column('result', sa.Text),
    sa.Column('created_at', sa.Float, nullable=False),
    sa.Column('finished_at', sa.Float),
    # durable-queue columns (migration 0005)
    sa.Column('params', sa.Text),                                  # JSON arguments for the job handler
    sa.Column('input_key', sa.Text),                               # blob-storage key of the encrypted input dataset
    sa.Column('attempts', sa.Integer, nullable=False, server_default='0'),
    sa.Column('max_attempts', sa.Integer, nullable=False, server_default='3'),
    sa.Column('run_after', sa.Float),                              # not claimable before this time (retry backoff)
    sa.Column('locked_by', sa.String(80)),                         # worker id holding the job
    sa.Column('heartbeat_at', sa.Float),
    sa.Column('started_at', sa.Float),
    sa.Column('cancel_requested', sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column('idempotency_key', sa.String(80)),
)
sa.Index('ix_jobs_claim', jobs_t.c.status, jobs_t.c.run_after, jobs_t.c.created_at)
sa.Index('ux_jobs_idempotency', jobs_t.c.workspace_id, jobs_t.c.idempotency_key, unique=True)

score_batches_t = sa.Table(
    'score_batches', metadata,
    sa.Column('id', sa.String(32), primary_key=True),
    sa.Column('workspace_id', sa.String(80), nullable=False, index=True),
    sa.Column('model_id', sa.String(32), nullable=False),
    sa.Column('created_at', sa.Float, nullable=False),
    sa.Column('rows', sa.Integer, nullable=False),
    sa.Column('drift', sa.Text),
)

audit_t = sa.Table(
    'audit_log', metadata,
    sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
    sa.Column('workspace_id', sa.String(80), nullable=False, index=True),
    sa.Column('actor', sa.String(120)),
    sa.Column('action', sa.String(60), nullable=False),
    sa.Column('detail', sa.Text),
    sa.Column('ts', sa.Float, nullable=False),
    sa.Column('prev_hash', sa.String(64)),       # hash chain: each entry commits to the one before it
    sa.Column('hash', sa.String(64)),
)

workspaces_t = sa.Table(
    'workspaces', metadata,
    sa.Column('id', sa.String(80), primary_key=True),
    sa.Column('name', sa.String(160), nullable=False),
    sa.Column('plan', sa.String(24), nullable=False, server_default='trial'),
    sa.Column('status', sa.String(12), nullable=False, server_default='active'),   # active | deleted
    sa.Column('sso_required', sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column('created_at', sa.Float, nullable=False),
)

memberships_t = sa.Table(
    'memberships', metadata,
    sa.Column('workspace_id', sa.String(80), primary_key=True),
    sa.Column('user_uid', sa.String(128), primary_key=True),
    sa.Column('email', sa.String(254)),
    sa.Column('role', sa.String(12), nullable=False),            # viewer | analyst | admin | owner
    sa.Column('status', sa.String(12), nullable=False),          # active | disabled
    sa.Column('created_at', sa.Float, nullable=False),
)
sa.Index('ix_memberships_user', memberships_t.c.user_uid)

invites_t = sa.Table(
    'invites', metadata,
    sa.Column('token_hash', sa.String(64), primary_key=True),     # only a hash is stored; the token is shown once
    sa.Column('workspace_id', sa.String(80), nullable=False, index=True),
    sa.Column('email', sa.String(254), nullable=False),
    sa.Column('role', sa.String(12), nullable=False),
    sa.Column('created_by', sa.String(254)),
    sa.Column('created_at', sa.Float, nullable=False),
    sa.Column('expires_at', sa.Float, nullable=False),
    sa.Column('accepted_at', sa.Float),
)

usage_t = sa.Table(
    'usage_counters', metadata,
    sa.Column('workspace_id', sa.String(80), primary_key=True),
    sa.Column('period', sa.String(7), primary_key=True),          # calendar month, UTC: YYYY-MM
    sa.Column('metric', sa.String(24), primary_key=True),         # scored_accounts | training_runs | api_calls
    sa.Column('value', sa.Float, nullable=False, server_default='0'),
)

keys_t = sa.Table(
    'workspace_keys', metadata,
    sa.Column('workspace_id', sa.String(80), primary_key=True),
    sa.Column('provider', sa.String(20), nullable=False),
    sa.Column('wrapped_dek', sa.LargeBinary),
    sa.Column('status', sa.String(12), nullable=False),            # active | shredded
    sa.Column('created_at', sa.Float, nullable=False),
    sa.Column('shredded_at', sa.Float),
)

kv_t = sa.Table(
    'workspace_kv', metadata,
    sa.Column('workspace_id', sa.String(80), primary_key=True),
    sa.Column('key', sa.String(40), primary_key=True),
    sa.Column('value', sa.Text, nullable=False),
    sa.Column('updated_at', sa.Float, nullable=False),
)

_engine: Engine | None = None
_lock = threading.Lock()

# The workspace whose rows the current request/job may touch. On PostgreSQL this becomes `app.workspace_id`, which row-level
# security policies check (migration 0003). Empty means "no tenant": nothing is visible (fail closed). '*' is the system context.
workspace_context: contextvars.ContextVar[str] = contextvars.ContextVar('churn_workspace', default='')


@contextlib.contextmanager
def workspace_scope(ws: str):
    token = workspace_context.set(ws)
    try:
        yield
    finally:
        workspace_context.reset(token)


@contextlib.contextmanager
def system_context():
    """Cross-tenant access for the few operations that need it (login lookup, queue admission, job claiming)."""
    with workspace_scope('*'):
        yield


_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrations')


def _adopt_legacy(conn) -> None:
    """A database created by the old create_all() has no alembic_version. Bring it to the baseline shape."""
    metadata.create_all(conn, checkfirst=True)
    cols = {c['name'] for c in sa.inspect(conn).get_columns('audit_log')}
    for name in ('prev_hash', 'hash'):
        if name not in cols:
            conn.exec_driver_sql(f'ALTER TABLE audit_log ADD COLUMN {name} VARCHAR(64)')


def migrate(engine: Engine) -> None:
    """Bring the schema to the latest revision (idempotent; safe when several workers start at once on Postgres)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option('script_location', _MIGRATIONS_DIR)
    with engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            conn.exec_driver_sql('SELECT pg_advisory_xact_lock(727274)')
        cfg.attributes['connection'] = conn
        tables = set(sa.inspect(conn).get_table_names())
        if 'alembic_version' not in tables and 'models' in tables:
            _adopt_legacy(conn)
            command.stamp(cfg, '0001')
        command.upgrade(cfg, 'head')


def new_id() -> str:
    return uuid.uuid4().hex


def get_engine() -> Engine:
    global _engine
    with _lock:
        if _engine is None:
            url = config.database_url()
            kwargs: dict[str, Any] = {'future': True, 'pool_pre_ping': True}
            if not url.startswith('sqlite'):
                kwargs.update(pool_size=config.DB_POOL_SIZE, max_overflow=config.DB_POOL_SIZE, pool_recycle=1800)
            if url.startswith('sqlite'):
                kwargs['connect_args'] = {'check_same_thread': False, 'timeout': 30}
            _engine = sa.create_engine(url, **kwargs)
            if url.startswith('postgresql'):
                @sa.event.listens_for(_engine, 'begin')
                def _tenant_context(conn):  # noqa: ANN001
                    conn.exec_driver_sql("SELECT set_config('app.workspace_id', %s, true)", (workspace_context.get(),))
            if url.startswith('sqlite'):
                @sa.event.listens_for(_engine, 'connect')
                def _pragmas(dbapi_conn, _):  # noqa: ANN001
                    cur = dbapi_conn.cursor()
                    cur.execute('PRAGMA journal_mode=WAL')
                    cur.execute('PRAGMA synchronous=NORMAL')
                    cur.close()
            migrate(_engine)
        return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests point DATA_DIR/DATABASE_URL elsewhere and call this)."""
    global _engine
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(',', ':'))


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return None if np.isnan(o) else float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(o)


def _audit_hash(prev: str, workspace_id: str, actor: str | None, action: str, detail: str | None, ts: float) -> str:
    import hashlib
    payload = '\x1f'.join([prev, workspace_id, actor or '', action, detail or '', repr(round(ts, 6))])
    return hashlib.sha256(payload.encode()).hexdigest()


def audit(workspace_id: str, actor: str | None, action: str, detail: Any = None) -> None:
    """Append a tamper-evident entry: its hash covers the previous entry's hash, so any edit or deletion breaks the chain."""
    detail_s = dumps(detail) if detail is not None else None
    ts = time.time()
    with get_engine().begin() as conn:
        if conn.dialect.name == 'postgresql':       # serialise appends per workspace so the chain cannot fork
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(hashtext(:ws))'), {'ws': workspace_id})
        prev = conn.execute(sa.select(audit_t.c.hash).where(audit_t.c.workspace_id == workspace_id)
                            .order_by(audit_t.c.id.desc()).limit(1)).scalar() or ''
        conn.execute(audit_t.insert().values(workspace_id=workspace_id, actor=actor, action=action, detail=detail_s, ts=ts, prev_hash=prev,
                                             hash=_audit_hash(prev, workspace_id, actor, action, detail_s, ts)))


def verify_audit(workspace_id: str) -> dict:
    """Recompute the chain. Reports the first entry that was altered, removed or reordered."""
    prev, n = '', 0
    with get_engine().connect() as conn:
        for r in conn.execute(sa.select(audit_t).where(audit_t.c.workspace_id == workspace_id).order_by(audit_t.c.id)):
            if r.hash is None:                      # entries from before the chain existed
                continue
            if r.prev_hash != prev or r.hash != _audit_hash(prev, workspace_id, r.actor, r.action, r.detail, r.ts):
                return {'ok': False, 'checked': n, 'first_bad_id': r.id}
            prev, n = r.hash, n + 1
    return {'ok': True, 'checked': n, 'first_bad_id': None}
