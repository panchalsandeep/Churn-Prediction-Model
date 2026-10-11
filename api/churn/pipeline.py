"""The B2B pipeline for a workspace: tables in, target definition, point-in-time features, training, scheduled scoring.

State is small and lives in the workspace key-value store (`pipeline`): the target definition, the event vocabulary used for features, and
the last run. The schedule lives in the `schedules` table so any worker can claim due runs without scanning every workspace.
"""
from __future__ import annotations

import logging
import time

import sqlalchemy as sa

from . import connectors, db, features, ingest, jobs, service

log = logging.getLogger('churnsight.pipeline')
MIN_EVERY_HOURS = 1.0
MAX_EVERY_HOURS = 24.0 * 31


def get(ws: str) -> dict:
    cfg = service.kv_get(ws, 'pipeline', {}) or {}
    return {'target': cfg.get('target'), 'vocab': cfg.get('vocab'), 'last_run': cfg.get('last_run'), 'trained_at': cfg.get('trained_at')}


def _save(ws: str, **changes) -> dict:
    cfg = get(ws)
    cfg.update(changes)
    service.kv_set(ws, 'pipeline', cfg)
    return cfg


def set_target(ws: str, spec: dict) -> dict:
    """Validate a target definition against the uploaded tables and store it. Returns the preview so the UI can show what it means."""
    tables = ingest.load_all(ws)
    preview = features.preview_target(tables, spec)
    _save(ws, target=preview['spec'])
    return preview


def preview(ws: str, spec: dict) -> dict:
    return features.preview_target(ingest.load_all(ws), spec)


def status(ws: str) -> dict:
    cfg = get(ws)
    sched = get_schedule(ws)
    return {'tables': ingest.table_info(ws), 'target': cfg['target'], 'trained_at': cfg['trained_at'], 'last_run': cfg['last_run'], 'schedule': sched,
            'ready_to_train': bool(cfg['target']) and 'accounts' in ingest.table_info(ws)}


# ── job bodies (registered in handlers.py) ──────────────────────────────────────────────────────────

def run_train(ws: str, actor: str | None, algorithm: str, progress) -> dict:
    cfg = get(ws)
    if not cfg['target']:
        raise ValueError('Define what "churned" means first (target definition).')
    progress('Loading tables', 2)
    tables = ingest.load_all(ws)
    try:
        progress('Building point-in-time features', 4)
        df, info = features.build_training_frame(tables, cfg['target'])
    except features.TargetError as exc:
        raise ValueError(str(exc)) from exc
    result = service.run_training(ws, df, algorithm, actor, progress, None, 'pipeline', 'churned', None, None)
    _save(ws, vocab=info['vocab'], trained_at=time.time(),
          last_run={'kind': 'train', 'at': time.time(), 'rows': info['rows'], 'snapshots': info['snapshots'], 'model_id': result['model_id'], 'status': result['status']})
    scored = None
    if result['status'] == 'champion':
        scored = run_score(ws, actor, progress, record=False)
    return {**result, 'features': info, 'scored_today': scored}


def run_score(ws: str, actor: str | None, progress, record: bool = True) -> dict:
    """Score every live account as of the latest data with the champion, exactly as nightly scheduling does."""
    cfg = get(ws)
    if not cfg['target']:
        raise ValueError('Define what "churned" means first (target definition).')
    progress('Syncing data sources', 3)
    synced = connectors.sync_all(ws)
    progress('Loading tables', 5)
    tables = ingest.load_all(ws)
    try:
        df, info = features.build_scoring_frame(tables, cfg['target'], cfg['vocab'])
    except features.TargetError as exc:
        raise ValueError(str(exc)) from exc
    out = service.run_batch_scoring(ws, df, actor, progress, allow_missing=True)
    if record:
        _save(ws, last_run={'kind': 'score', 'at': time.time(), 'rows': out['rows'], 'as_of': info['as_of'], 'drift': out['drift'].get('status')})
    failed = [s for s in synced if not s['ok']]
    return {**out, 'as_of': info['as_of'], 'sources': synced,
            'warnings': [f"Source {s['name']} failed ({s['detail']}); scored with the data already loaded." for s in failed]}


# ── schedule ────────────────────────────────────────────────────────────────────────────────────────

def get_schedule(ws: str) -> dict | None:
    s = db.schedules_t
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(s).where(s.c.workspace_id == ws, s.c.kind == 'pipeline_score')).first()
    if not r:
        return None
    return {'enabled': bool(r.enabled), 'every_hours': r.every_hours, 'next_run_at': r.next_run_at, 'last_job_id': r.last_job_id, 'last_status': r.last_status}


def set_schedule(ws: str, enabled: bool, every_hours: float) -> dict:
    try:
        every = float(every_hours)
    except (TypeError, ValueError):
        raise ValueError('every_hours must be a number.') from None
    if not MIN_EVERY_HOURS <= every <= MAX_EVERY_HOURS:
        raise ValueError(f'every_hours must be between {MIN_EVERY_HOURS:g} and {MAX_EVERY_HOURS:g}.')
    if enabled and not get(ws)['trained_at']:
        raise ValueError('Train a model from your tables first; scheduled scoring uses it.')
    now = time.time()
    s = db.schedules_t
    with db.get_engine().begin() as c:
        c.execute(sa.delete(s).where(s.c.workspace_id == ws, s.c.kind == 'pipeline_score'))
        c.execute(s.insert().values(workspace_id=ws, kind='pipeline_score', every_hours=every, next_run_at=now + every * 3600, enabled=bool(enabled), updated_at=now))
    return get_schedule(ws)


def _periodic_tick() -> None:
    tick()


jobs.add_periodic(_periodic_tick, 15.0)


def tick(now: float | None = None) -> int:
    """Start every scheduled run that is due. Safe to call from many workers at once: the UPDATE that advances `next_run_at` is the claim."""
    now = now if now is not None else time.time()
    s, started = db.schedules_t, 0
    with db.system_context(), db.get_engine().connect() as c:
        due = c.execute(sa.select(s.c.workspace_id, s.c.every_hours, s.c.next_run_at).where(s.c.enabled.is_(True), s.c.next_run_at <= now, s.c.kind == 'pipeline_score')).all()
    for r in due:
        with db.system_context(), db.get_engine().begin() as c:
            won = c.execute(sa.update(s).where(s.c.workspace_id == r.workspace_id, s.c.kind == 'pipeline_score', s.c.next_run_at == r.next_run_at)
                            .values(next_run_at=now + r.every_hours * 3600, updated_at=now)).rowcount
        if won != 1:
            continue                                                     # another worker took it
        status, job_id = 'queued', None
        try:
            with db.workspace_scope(r.workspace_id):
                job_id = jobs.submit(r.workspace_id, 'pipeline_score', {'actor': 'scheduler'}, None, f'sched-{r.workspace_id}-{int(r.next_run_at)}')
            started += 1
        except Exception as exc:  # noqa: BLE001  (busy, over quota, ...: record why and try again next interval)
            status = f'skipped: {exc}'[:200]
            log.info('scheduled run for %s skipped: %s', r.workspace_id, exc)
        with db.system_context(), db.get_engine().begin() as c:
            c.execute(sa.update(s).where(s.c.workspace_id == r.workspace_id, s.c.kind == 'pipeline_score').values(last_job_id=job_id, last_status=status))
    return started
