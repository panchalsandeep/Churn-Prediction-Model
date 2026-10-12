"""Plans, seat limits, usage metering and quota checks.

Definitions (assumptions, see docs/ROADMAP.md section 6; confirm before selling on them):
  seat              a person with an active membership in the workspace. Pending invitations hold a seat until they expire.
  scored_accounts   customer rows scored per calendar month (training scores everyone once; batch scoring adds its rows).
  training_runs     successful model trainings per month (the free demo dataset does not count).
  api_calls         authenticated API requests per month. Metered and reported, never blocked (a dashboard must keep loading).
  rows_per_upload   largest dataset accepted in one job.

The numbers in PLANS are placeholders chosen to exercise the machinery, NOT a price list. Override per deployment with
PLAN_LIMITS_JSON='{"team": {"seats": 25}}'. A workspace without a record (local development) is on the unlimited 'dev' plan.

Billing itself (invoices, Stripe) is not built: /api/usage/export gives the numbers a billing system would consume.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from collections import Counter

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from . import config, db

log = logging.getLogger('churnsight.quotas')

UNLIMITED = None
PLANS: dict[str, dict] = {
    'trial': {'seats': 3, 'scored_accounts': 50_000, 'training_runs': 10, 'api_calls': 50_000, 'rows_per_upload': 50_000},
    'team': {'seats': 10, 'scored_accounts': 500_000, 'training_runs': 60, 'api_calls': 1_000_000, 'rows_per_upload': 250_000},
    'business': {'seats': 50, 'scored_accounts': 5_000_000, 'training_runs': 400, 'api_calls': 10_000_000, 'rows_per_upload': 250_000},
    'enterprise': {'seats': UNLIMITED, 'scored_accounts': UNLIMITED, 'training_runs': UNLIMITED, 'api_calls': UNLIMITED, 'rows_per_upload': 250_000},
    'dev': {'seats': UNLIMITED, 'scored_accounts': UNLIMITED, 'training_runs': UNLIMITED, 'api_calls': UNLIMITED, 'rows_per_upload': UNLIMITED},
}
METRICS = ('scored_accounts', 'training_runs', 'api_calls')
TRAIN_KINDS = ('train', 'pipeline_train')
HARD = ('scored_accounts', 'training_runs')              # blocked at the limit; api_calls are soft


class QuotaExceeded(Exception):
    """HTTP 402: the plan does not allow this. Carries the numbers so the UI can say exactly why."""

    def __init__(self, message: str, metric: str, limit: int | None, used: int, status: int = 402):
        super().__init__(message)
        self.metric, self.limit, self.used, self.status = metric, limit, used, status
        self.code = 'quota_exceeded'


def plans() -> dict[str, dict]:
    merged = {k: dict(v) for k, v in PLANS.items()}
    try:
        for name, over in json.loads(os.environ.get('PLAN_LIMITS_JSON') or '{}').items():
            merged.setdefault(name, dict(PLANS['trial'])).update(over)
    except (ValueError, AttributeError):
        log.warning('PLAN_LIMITS_JSON is not valid JSON; ignored')
    return merged


def period(now: float | None = None) -> str:
    return time.strftime('%Y-%m', time.gmtime(now if now is not None else time.time()))


def plan_of(ws: str) -> str:
    with db.system_context(), db.get_engine().connect() as c:
        row = c.execute(sa.select(db.workspaces_t.c.plan).where(db.workspaces_t.c.id == ws)).first()
    return row.plan if row else 'dev'


def limits(ws: str) -> dict:
    name = plan_of(ws)
    return {'plan': name, **plans().get(name, plans()['trial'])}


# ── metering ──────────────────────────────────────────────────────────────────────────────────────

_buffer: Counter = Counter()
_buf_lock = threading.Lock()
_flusher: threading.Thread | None = None
FLUSH_EVERY_S = 5.0


def _upsert(conn, ws: str, per: str, metric: str, amount: float) -> None:
    values = dict(workspace_id=ws, period=per, metric=metric, value=amount)
    ins = (postgresql if conn.dialect.name == 'postgresql' else sqlite).insert(db.usage_t).values(**values)
    conn.execute(ins.on_conflict_do_update(index_elements=['workspace_id', 'period', 'metric'],
                                           set_={'value': db.usage_t.c.value + amount}))


def record(ws: str, metric: str, amount: float = 1, buffered: bool = False) -> None:
    """Add to this month's counter. `buffered` batches writes (used for per-request api_calls) and flushes every few seconds."""
    if metric not in METRICS or not amount or plan_of_cached(ws) == 'dev':
        return
    if buffered:
        global _flusher
        with _buf_lock:
            _buffer[(ws, period(), metric)] += amount
            if _flusher is None or not _flusher.is_alive():
                _flusher = threading.Thread(target=_flush_loop, name='usage-flush', daemon=True)
                _flusher.start()
        return
    with db.system_context(), db.get_engine().begin() as c:
        _upsert(c, ws, period(), metric, amount)


_flush_lock = threading.Lock()      # a reader must never see counts that left the buffer but are not in the database yet


def flush() -> int:
    with _flush_lock:
        with _buf_lock:
            pending = dict(_buffer)
            _buffer.clear()
        if not pending:
            return 0
        try:
            with db.system_context(), db.get_engine().begin() as c:
                for (ws, per, metric), amount in pending.items():
                    _upsert(c, ws, per, metric, amount)
        except Exception:  # noqa: BLE001  (keep the counts for the next attempt rather than losing them)
            log.warning('usage flush failed; will retry')
            with _buf_lock:
                for k, v in pending.items():
                    _buffer[k] += v
            return 0
        return len(pending)


def _flush_loop() -> None:
    while True:
        time.sleep(FLUSH_EVERY_S)
        flush()


atexit.register(flush)

_plan_cache: dict[str, tuple[float, str]] = {}


def plan_of_cached(ws: str, ttl: float = 30.0) -> str:
    hit = _plan_cache.get(ws)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    name = plan_of(ws)
    _plan_cache[ws] = (time.time(), name)
    return name


def forget_plans() -> None:
    _plan_cache.clear()


def usage(ws: str, per: str | None = None) -> dict[str, int]:
    per = per or period()
    with _flush_lock:
        with db.system_context(), db.get_engine().connect() as c:
            rows = c.execute(sa.select(db.usage_t.c.metric, db.usage_t.c.value).where(db.usage_t.c.workspace_id == ws, db.usage_t.c.period == per)).all()
        out = {m: 0 for m in METRICS}
        out.update({r.metric: int(r.value) for r in rows})
        with _buf_lock:
            for (w, p, metric), amount in _buffer.items():
                if w == ws and p == per:
                    out[metric] = out.get(metric, 0) + int(amount)
    return out


def pending_invites(ws: str) -> int:
    inv = db.invites_t
    with db.get_engine().connect() as c:
        return int(c.execute(sa.select(sa.func.count()).where(inv.c.workspace_id == ws, inv.c.accepted_at.is_(None),
                                                              inv.c.expires_at > time.time())).scalar() or 0)


def seats(ws: str) -> dict:
    from . import tenancy
    lim = limits(ws)['seats']
    used, pending = tenancy.seat_count(ws), pending_invites(ws)
    return {'used': used, 'pending': pending, 'limit': lim, 'available': None if lim is None else max(0, lim - used - pending)}


# ── enforcement ───────────────────────────────────────────────────────────────────────────────────

def check_seat(ws: str) -> None:
    s = seats(ws)
    if s['limit'] is not None and s['used'] + s['pending'] >= s['limit']:
        raise QuotaExceeded(f"Your {limits(ws)['plan']} plan includes {s['limit']} seats and all are in use or reserved by pending invitations. "
                            'Remove a member, revoke an invitation or upgrade the plan.', 'seats', s['limit'], s['used'] + s['pending'])


def check_job(ws: str, kind: str, rows: int | None) -> None:
    """Called before a job is accepted. Raises QuotaExceeded with the figures the UI needs."""
    lim = limits(ws)
    cap = lim['rows_per_upload']
    if cap is not None and rows and rows > cap:
        raise QuotaExceeded(f"This dataset has {rows:,} rows; the {lim['plan']} plan accepts up to {cap:,} per upload.", 'rows_per_upload', cap, rows)
    if kind == 'demo':
        return                                                    # the sample dataset is free and unmetered
    used = usage(ws)
    if kind in TRAIN_KINDS and lim['training_runs'] is not None and used['training_runs'] >= lim['training_runs']:
        raise QuotaExceeded(f"You have used all {lim['training_runs']} training runs included this month.", 'training_runs',
                            lim['training_runs'], used['training_runs'])
    if lim['scored_accounts'] is not None and rows and used['scored_accounts'] + rows > lim['scored_accounts']:
        raise QuotaExceeded(f"Scoring {rows:,} accounts would exceed this month's allowance of {lim['scored_accounts']:,} "
                            f"({used['scored_accounts']:,} already used).", 'scored_accounts', lim['scored_accounts'], used['scored_accounts'])


def on_job_success(ws: str, kind: str, rows: int | None, result: dict | None = None) -> None:
    if kind == 'demo':
        return
    if kind in TRAIN_KINDS or (kind == 'mlops_check' and isinstance(result, dict) and result.get('retrained')):
        record(ws, 'training_runs', 1)
    if kind.startswith('pipeline_') and isinstance(result, dict):        # row counts are only known once the tables have been read
        rows = result.get('rows') or (result.get('scored_today') or {}).get('rows') or rows
    if rows:
        record(ws, 'scored_accounts', rows)


def api_over_limit(ws: str) -> bool:
    lim = limits(ws)['api_calls']
    return lim is not None and usage(ws)['api_calls'] > lim


def summary(ws: str) -> dict:
    lim = limits(ws)
    used = usage(ws)
    return {'plan': lim['plan'], 'period': period(), 'seats': seats(ws),
            'metrics': {m: {'used': used[m], 'limit': lim[m], 'hard': m in HARD,
                            'percent': None if lim[m] is None else round(100 * used[m] / max(lim[m], 1), 1)} for m in METRICS},
            'rows_per_upload': lim['rows_per_upload']}


def export_rows(ws: str, months: int = 12) -> list[dict]:
    u = db.usage_t
    with db.system_context(), db.get_engine().connect() as c:
        rows = c.execute(sa.select(u).where(u.c.workspace_id == ws).order_by(u.c.period.desc(), u.c.metric).limit(months * len(METRICS))).all()
    return [{'period': r.period, 'metric': r.metric, 'value': int(r.value)} for r in rows]
