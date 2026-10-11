"""Persistence (SQLAlchemy Core). SQLite by default; set DATABASE_URL for Postgres.

Everything is keyed by workspace_id, so accounts never see each other's data.
"""
from __future__ import annotations

import json
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
    sa.Column('reasons', sa.Text),
    sa.Column('attrs', sa.Text),
)
sa.Index('ix_customers_ws_prob', customers_t.c.workspace_id, customers_t.c.prob)
sa.Index('ix_customers_ws_tier', customers_t.c.workspace_id, customers_t.c.tier)

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
)

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


def new_id() -> str:
    return uuid.uuid4().hex


def get_engine() -> Engine:
    global _engine
    with _lock:
        if _engine is None:
            url = config.database_url()
            kwargs: dict[str, Any] = {'future': True, 'pool_pre_ping': True}
            if url.startswith('sqlite'):
                kwargs['connect_args'] = {'check_same_thread': False, 'timeout': 30}
            _engine = sa.create_engine(url, **kwargs)
            if url.startswith('sqlite'):
                @sa.event.listens_for(_engine, 'connect')
                def _pragmas(dbapi_conn, _):  # noqa: ANN001
                    cur = dbapi_conn.cursor()
                    cur.execute('PRAGMA journal_mode=WAL')
                    cur.execute('PRAGMA synchronous=NORMAL')
                    cur.close()
            metadata.create_all(_engine)
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


def audit(workspace_id: str, actor: str | None, action: str, detail: Any = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(audit_t.insert().values(workspace_id=workspace_id, actor=actor, action=action,
                                             detail=dumps(detail) if detail is not None else None, ts=time.time()))
