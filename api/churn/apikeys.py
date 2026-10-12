"""Machine credentials for pushing data (events, tickets, invoices) from a customer's own systems.

A key belongs to one workspace and has one scope (`ingest`: it can append rows to the data tables and do nothing else). Only a SHA-256
hash is stored; the key is shown once at creation. Keys are random 256-bit tokens, so a fast hash is appropriate (unlike passwords).
They are accepted only by the ingestion endpoint: every other endpoint wants a signed-in user.
"""
from __future__ import annotations

import hashlib
import secrets
import time

import sqlalchemy as sa

from . import db

PREFIX = 'cs_live_'
SCOPES = ('ingest',)
MAX_KEYS = 10
_last_touch: dict[str, float] = {}


class KeyError_(ValueError):
    pass


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create(ws: str, name: str, scope: str, creator: str | None) -> dict:
    name = (name or '').strip()[:80]
    if not name:
        raise KeyError_('Give the key a name (for example "event pipeline").')
    if scope not in SCOPES:
        raise KeyError_(f'scope must be one of: {", ".join(SCOPES)}.')
    k = db.api_keys_t
    with db.get_engine().begin() as c:
        live = c.execute(sa.select(sa.func.count()).where(k.c.workspace_id == ws, k.c.revoked_at.is_(None))).scalar()
        if live >= MAX_KEYS:
            raise KeyError_(f'A workspace can have {MAX_KEYS} active keys. Revoke one first.')
        raw = PREFIX + secrets.token_urlsafe(32)
        key_id = secrets.token_hex(8)
        c.execute(k.insert().values(id=key_id, workspace_id=ws, name=name, key_hash=_hash(raw), scope=scope, created_by=creator, created_at=time.time()))
    db.audit(ws, creator, 'apikey.create', {'id': key_id, 'name': name, 'scope': scope})
    return {'id': key_id, 'name': name, 'scope': scope, 'key': raw}


def verify(raw: str, scope: str) -> dict | None:
    """Resolve a bearer value to {id, workspace_id, name} or None. Runs outside any workspace context (the key *is* the context)."""
    if not raw or not raw.startswith(PREFIX) or len(raw) > 128:
        return None
    k = db.api_keys_t
    with db.system_context(), db.get_engine().connect() as c:
        row = c.execute(sa.select(k).where(k.c.key_hash == _hash(raw))).first()
    if not row or row.revoked_at is not None or row.scope != scope:
        return None
    now = time.time()
    if now - _last_touch.get(row.id, 0) > 60:                  # last_used_at is informational: write at most once a minute per key
        _last_touch[row.id] = now
        with db.system_context(), db.get_engine().begin() as c:
            c.execute(sa.update(k).where(k.c.id == row.id).values(last_used_at=now))
    return {'id': row.id, 'workspace_id': row.workspace_id, 'name': row.name}


def list_keys(ws: str) -> list[dict]:
    k = db.api_keys_t
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(k).where(k.c.workspace_id == ws).order_by(k.c.created_at)).all()
    return [{'id': r.id, 'name': r.name, 'scope': r.scope, 'created_by': r.created_by, 'created_at': r.created_at,
             'last_used_at': r.last_used_at, 'revoked': r.revoked_at is not None} for r in rows]


def revoke(ws: str, key_id: str, actor: str | None) -> bool:
    k = db.api_keys_t
    with db.get_engine().begin() as c:
        n = c.execute(sa.update(k).where(k.c.id == key_id, k.c.workspace_id == ws, k.c.revoked_at.is_(None)).values(revoked_at=time.time())).rowcount
    if n:
        db.audit(ws, actor, 'apikey.revoke', {'id': key_id})
    return bool(n)
