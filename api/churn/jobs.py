"""Background jobs with persisted progress (training, batch scoring).

Locally and in containers jobs run on a small thread pool and the client polls /api/jobs/<id>.
On serverless platforms (SYNC_JOBS) they run inline, because threads don't outlive the request.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import sqlalchemy as sa

from . import config, db

log = logging.getLogger('churnsight.jobs')
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='job')
_marked_stale = False
_lock = threading.Lock()


def _mark_stale_once() -> None:
    """Jobs left 'running' by a previous process can never finish; fail them."""
    global _marked_stale
    with _lock:
        if _marked_stale:
            return
        _marked_stale = True
    with db.get_engine().begin() as c:
        c.execute(sa.update(db.jobs_t).where(db.jobs_t.c.status.in_(['queued', 'running']))
                  .values(status='error', error='Interrupted by a server restart', finished_at=time.time()))


def reset_state() -> None:
    global _marked_stale
    _marked_stale = False


def _update(job_id: str, **values) -> None:
    with db.get_engine().begin() as c:
        c.execute(sa.update(db.jobs_t).where(db.jobs_t.c.id == job_id).values(**values))


def submit(ws: str, kind: str, fn: Callable[[Callable[[str, int], None]], dict]) -> str:
    _mark_stale_once()
    job_id = db.new_id()
    with db.get_engine().begin() as c:
        c.execute(db.jobs_t.insert().values(id=job_id, workspace_id=ws, kind=kind, status='queued', progress=0,
                                            stage='Queued', created_at=time.time()))

    def run() -> None:
        _update(job_id, status='running', stage='Starting', progress=1)

        def progress(stage: str, pct: int) -> None:
            _update(job_id, stage=stage, progress=int(max(0, min(99, pct))))

        try:
            result = fn(progress)
            _update(job_id, status='done', stage='Done', progress=100, result=db.dumps(result), finished_at=time.time())
        except ValueError as exc:      # user-facing validation problems
            _update(job_id, status='error', error=str(exc), finished_at=time.time())
        except Exception as exc:       # noqa: BLE001
            log.error('job %s failed: %s\n%s', job_id, exc, traceback.format_exc())
            _update(job_id, status='error', error=f'{kind} failed: {exc}', finished_at=time.time())

    if config.SYNC_JOBS:
        run()
    else:
        _executor.submit(run)
    return job_id


def get(ws: str, job_id: str) -> dict | None:
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(db.jobs_t).where(db.jobs_t.c.id == job_id, db.jobs_t.c.workspace_id == ws)).first()
    if not r:
        return None
    d = dict(r._mapping)
    d['result'] = db.loads(d.get('result'))
    return d
