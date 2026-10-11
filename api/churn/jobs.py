"""Durable background jobs (training, batch scoring) on a database-backed queue.

A job is a row: kind + JSON params + (optionally) the uploaded dataset, stored encrypted in blob storage. Any number of
workers, in this process or in separate `python -m churn.worker` processes, claim queued rows with an optimistic UPDATE,
heartbeat while they run, and finish them. If a worker dies its heartbeat goes stale and the reaper requeues the job
(with backoff, up to max_attempts) or fails it. Jobs can be cancelled and de-duplicated with an idempotency key.

Modes
  SYNC_JOBS=1          run the job inline in the request (serverless, tests)
  EMBEDDED_WORKER=1    worker threads inside the web process (default; fine for one container)
  EMBEDDED_WORKER=0    web only enqueues; run `python -m churn.worker` separately (how you scale out)

Hard timeouts: a dedicated worker process exits (after marking the job failed) if a job overruns JOB_TIMEOUT_S plus a
grace period, and its supervisor restarts it. Embedded threads cannot be killed, so there a timeout is cooperative:
the job is marked failed and stops at its next progress report.
"""
from __future__ import annotations

import gzip
import io
import logging
import os
import socket
import threading
import time
import traceback
from typing import Callable

import pandas as pd
import sqlalchemy as sa

from . import config, crypto, db, quotas, storage

log = logging.getLogger('churnsight.jobs')

TERMINAL = ('done', 'error', 'cancelled')
HANDLERS: dict[str, Callable] = {}
TRANSIENT = (sa.exc.OperationalError, ConnectionError, TimeoutError, storage.StorageError)


class JobRejected(Exception):
    """Raised when a job can't be accepted; `status` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class JobCancelled(Exception):
    pass


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


# ── enqueue ───────────────────────────────────────────────────────────────────────────────────────

def _put_input(ws: str, job_id: str, df: pd.DataFrame) -> str:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=3) as gz:
        gz.write(df.to_csv(index=False).encode())
    key = f'{storage.upload_prefix(ws)}/{job_id}.cse'
    return storage.get_storage().put(key, crypto.encrypt_bytes(ws, buf.getvalue(), f'job|{job_id}'))


def _load_input(ws: str, job_id: str, key: str) -> pd.DataFrame:
    raw = gzip.decompress(crypto.decrypt_bytes(ws, storage.get_storage().get(key), f'job|{job_id}'))
    return pd.read_csv(io.BytesIO(raw), keep_default_na=True)


def _admit(ws: str) -> None:
    """Fairness and back-pressure: one job per workspace, a bounded global queue, old rows cleaned up."""
    j = db.jobs_t
    reap()
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.delete(j).where(j.c.status.in_(TERMINAL), j.c.finished_at < time.time() - config.JOB_RETENTION_DAYS * 86400))
        mine = c.execute(sa.select(sa.func.count()).where(j.c.workspace_id == ws, j.c.status.in_(['queued', 'running']))).scalar()
        queued = c.execute(sa.select(sa.func.count()).where(j.c.status == 'queued')).scalar()
    if mine:
        raise JobRejected('You already have a training or scoring job in progress. Wait for it to finish.', 409)
    if queued >= config.MAX_QUEUED_JOBS:
        raise JobRejected('The server is busy training other models. Please try again in a few minutes.', 503)


def _by_key(ws: str, key: str) -> str | None:
    with db.get_engine().connect() as c:
        return c.execute(sa.select(db.jobs_t.c.id).where(db.jobs_t.c.workspace_id == ws, db.jobs_t.c.idempotency_key == key)).scalar()


def submit(ws: str, kind: str, params: dict | None = None, df: pd.DataFrame | None = None, idempotency_key: str | None = None) -> str:
    """Queue a job and return its id. The same idempotency key within a workspace returns the original job."""
    if kind not in HANDLERS:
        raise ValueError(f'Unknown job kind: {kind}')
    key = (idempotency_key or '').strip()[:80] or None
    if key and (existing := _by_key(ws, key)):
        return existing
    rows = int(len(df)) if df is not None else int((params or {}).get('rows') or 0) or None
    quotas.check_job(ws, kind, rows)
    _admit(ws)
    job_id = db.new_id()
    params = {**(params or {}), '_rows': rows}
    input_key = _put_input(ws, job_id, df) if df is not None else None
    try:
        with db.get_engine().begin() as c:
            c.execute(db.jobs_t.insert().values(
                id=job_id, workspace_id=ws, kind=kind, status='queued', progress=0, stage='Queued', created_at=time.time(),
                params=db.dumps(params or {}), input_key=input_key, attempts=0, max_attempts=config.JOB_MAX_ATTEMPTS,
                cancel_requested=False, idempotency_key=key))
    except sa.exc.IntegrityError:               # lost a race on the idempotency key
        if input_key:
            storage.get_storage().delete(input_key)
        if key and (existing := _by_key(ws, key)):
            return existing
        raise
    if config.SYNC_JOBS:
        run_next(f'inline-{os.getpid()}', only=job_id)
    elif config.EMBEDDED_WORKER:
        start_embedded()
        _wake.set()
    return job_id


# ── claim / run ───────────────────────────────────────────────────────────────────────────────────

def worker_id() -> str:
    return f'{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}'


def claim(worker: str, only: str | None = None) -> dict | None:
    """Atomically take one runnable job. Two workers can never both win the same row (the UPDATE is conditional)."""
    j, now = db.jobs_t, time.time()
    with db.system_context(), db.get_engine().begin() as c:
        q = sa.select(j.c.id, j.c.attempts).where(j.c.status == 'queued', sa.or_(j.c.run_after.is_(None), j.c.run_after <= now))
        q = q.where(j.c.id == only) if only else q.order_by(j.c.created_at).limit(1).with_for_update(skip_locked=True)
        row = c.execute(q).first()
        if not row:
            return None
        won = c.execute(sa.update(j).where(j.c.id == row.id, j.c.status == 'queued').values(
            status='running', locked_by=worker, heartbeat_at=now, started_at=now, attempts=row.attempts + 1,
            stage='Starting', progress=1, run_after=None)).rowcount
        if won != 1:
            return None
        return dict(c.execute(sa.select(j).where(j.c.id == row.id)).one()._mapping)


def _finish(job_id: str, worker: str, **values) -> bool:
    """Write a terminal/retry state only if we still own the job (a reaped zombie must not overwrite its replacement)."""
    j = db.jobs_t
    with db.system_context(), db.get_engine().begin() as c:
        return c.execute(sa.update(j).where(j.c.id == job_id, j.c.locked_by == worker, j.c.status == 'running').values(**values)).rowcount == 1


def reap() -> int:
    """Requeue (with backoff) or fail jobs whose worker stopped heartbeating."""
    j, now, n = db.jobs_t, time.time(), 0
    with db.system_context(), db.get_engine().begin() as c:
        stale = c.execute(sa.select(j.c.id, j.c.attempts, j.c.max_attempts, j.c.input_key).where(
            j.c.status == 'running', j.c.heartbeat_at < now - config.JOB_STALE_S)).all()
        for r in stale:
            if r.attempts < r.max_attempts:
                values = dict(status='queued', locked_by=None, stage='Retrying after a worker interruption', progress=0,
                              run_after=now + min(300, 5 * 2 ** r.attempts))
            else:
                values = dict(status='error', locked_by=None, error='The worker stopped responding and retries are exhausted.', finished_at=now)
            n += c.execute(sa.update(j).where(j.c.id == r.id, j.c.status == 'running', j.c.heartbeat_at < now - config.JOB_STALE_S).values(**values)).rowcount
            if values['status'] == 'error' and r.input_key:
                storage.get_storage().delete(r.input_key)
    return n


def run_next(worker: str, only: str | None = None) -> bool:
    """Claim and execute one job. Returns False when nothing was runnable."""
    job = claim(worker, only)
    if not job:
        return False
    execute(job, worker)
    return True


def execute(job: dict, worker: str) -> None:
    job_id, ws, kind = job['id'], job['workspace_id'], job['kind']
    stop = threading.Event()                    # set on cancel, timeout or lost lock; progress() turns it into JobCancelled
    why: dict[str, str] = {}
    done = threading.Event()

    def beat() -> None:
        started = time.time()
        while not done.wait(config.JOB_HEARTBEAT_S):
            try:
                with db.system_context(), db.get_engine().begin() as c:
                    r = c.execute(sa.select(db.jobs_t.c.cancel_requested).where(db.jobs_t.c.id == job_id, db.jobs_t.c.locked_by == worker,
                                                                               db.jobs_t.c.status == 'running')).first()
                    if r is None:
                        why['r'] = 'lost'
                        stop.set()
                        return
                    c.execute(sa.update(db.jobs_t).where(db.jobs_t.c.id == job_id).values(heartbeat_at=time.time()))
                if r.cancel_requested:
                    why['r'] = 'cancelled'
                    stop.set()
                if time.time() - started > config.JOB_TIMEOUT_S:
                    why['r'] = 'timeout'
                    stop.set()
                    _finish(job_id, worker, status='error', error=f'Timed out after {int(config.JOB_TIMEOUT_S)} s.', finished_at=time.time())
                    if _dedicated_worker:                    # a process we can restart: kill it rather than leave a runaway job
                        time.sleep(30)
                        log.error('job %s overran its timeout; exiting worker', job_id)
                        os._exit(3)
                    return
            except Exception:  # noqa: BLE001
                log.warning('heartbeat for %s failed: %s', job_id, traceback.format_exc(limit=1))

    def progress(stage: str, pct: int) -> None:
        if stop.is_set():
            raise JobCancelled(why.get('r', 'cancelled'))
        with db.get_engine().begin() as c:
            c.execute(sa.update(db.jobs_t).where(db.jobs_t.c.id == job_id).values(stage=stage, progress=int(max(0, min(99, pct))), heartbeat_at=time.time()))

    hb = threading.Thread(target=beat, name=f'hb-{job_id[:6]}', daemon=True)
    hb.start()
    input_key = job.get('input_key')
    log.info('job %s (%s) started attempt %s rid=%s', job_id, kind, job.get('attempts'), (db.loads(job.get('params')) or {}).get('rid'))
    try:
        with db.workspace_scope(ws):
            if job.get('cancel_requested'):
                raise JobCancelled('cancelled')
            df = _load_input(ws, job_id, input_key) if input_key else None
            result = HANDLERS[kind](ws, db.loads(job.get('params')) or {}, df, progress)
        if _finish(job_id, worker, status='done', stage='Done', progress=100, result=db.dumps(result), finished_at=time.time()):
            quotas.on_job_success(ws, kind, (db.loads(job.get('params')) or {}).get('_rows'))
    except JobCancelled:
        if why.get('r') not in ('timeout', 'lost'):
            _finish(job_id, worker, status='cancelled', stage='Cancelled', error='Cancelled.', finished_at=time.time())
    except ValueError as exc:                   # user-facing validation problems: retrying cannot help
        _finish(job_id, worker, status='error', error=str(exc), finished_at=time.time())
    except Exception as exc:                    # noqa: BLE001
        log.error('job %s failed: %s\n%s', job_id, exc, traceback.format_exc())
        if isinstance(exc, TRANSIENT) and job['attempts'] < job['max_attempts']:
            _finish(job_id, worker, status='queued', locked_by=None, stage='Retrying after a temporary error', progress=0,
                    run_after=time.time() + min(300, 5 * 2 ** job['attempts']))
            input_key = None                     # keep the input for the retry
        else:
            _finish(job_id, worker, status='error', error=f'{kind} failed: {exc}', finished_at=time.time())
    finally:
        done.set()
        if input_key:                            # customer data should not outlive the job
            try:
                storage.get_storage().delete(input_key)
            except Exception:  # noqa: BLE001
                log.warning('could not delete job input %s', input_key)


# ── workers ───────────────────────────────────────────────────────────────────────────────────────

_dedicated_worker = False
_wake = threading.Event()
_embedded: list[threading.Thread] = []
_embedded_lock = threading.Lock()
_stop_all = threading.Event()


def _loop(name: str, once: bool = False) -> None:
    last_reap = 0.0
    while not _stop_all.is_set():
        try:
            if time.time() - last_reap > config.JOB_STALE_S / 3:
                reap()
                last_reap = time.time()
            if run_next(f'{name}:{threading.get_ident()}'):
                if once:
                    return
                continue
        except Exception:  # noqa: BLE001
            log.error('worker loop error: %s', traceback.format_exc())
        if once:
            return
        _wake.wait(config.JOB_POLL_S)
        _wake.clear()


def start_embedded() -> None:
    with _embedded_lock:
        _embedded[:] = [t for t in _embedded if t.is_alive()]
        _stop_all.clear()
        while len(_embedded) < config.MAX_CONCURRENT_JOBS:
            t = threading.Thread(target=_loop, args=(f'{socket.gethostname()}:{os.getpid()}',), name=f'job-{len(_embedded)}', daemon=True)
            t.start()
            _embedded.append(t)


def stop_embedded(timeout: float = 5) -> None:
    _stop_all.set()
    _wake.set()
    for t in list(_embedded):
        t.join(timeout)
    _embedded.clear()


def run_worker(concurrency: int | None = None) -> None:
    """Entry point of `python -m churn.worker`: claim jobs until interrupted."""
    global _dedicated_worker
    _dedicated_worker = True
    config.EMBEDDED_WORKER = False
    _stop_all.clear()
    n = concurrency or config.MAX_CONCURRENT_JOBS
    log.info('worker %s starting with %d slot(s)', worker_id(), n)
    threads = [threading.Thread(target=_loop, args=(f'{socket.gethostname()}:{os.getpid()}',), name=f'job-{i}', daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    import signal
    shutdown = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: shutdown.set())      # Kubernetes sends SIGTERM: stop claiming, let running jobs finish
        except ValueError:
            pass                                                # not the main thread (tests)
    while any(t.is_alive() for t in threads) and not shutdown.is_set():
        time.sleep(0.5)
    if shutdown.is_set():
        log.info('worker shutting down; finishing running jobs')
        _stop_all.set()
        _wake.set()
        for t in threads:
            t.join(config.JOB_TIMEOUT_S)


def reset_state() -> None:
    """Test helper: stop embedded workers and forget them."""
    stop_embedded(2)
    _stop_all.clear()


# ── read / control ───────────────────────────────────────────────────────────────────────────────

_PUBLIC = ('id', 'workspace_id', 'kind', 'status', 'stage', 'progress', 'error', 'result', 'created_at', 'finished_at', 'attempts')


def get(ws: str, job_id: str) -> dict | None:
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(db.jobs_t).where(db.jobs_t.c.id == job_id, db.jobs_t.c.workspace_id == ws)).first()
    if not r:
        return None
    d = {k: r._mapping[k] for k in _PUBLIC}
    d['result'] = db.loads(d.get('result'))
    if r.status == 'queued':             # tell people where they are in line
        with db.system_context(), db.get_engine().connect() as c:
            ahead = c.execute(sa.select(sa.func.count()).where(db.jobs_t.c.status == 'queued', db.jobs_t.c.created_at < r.created_at)).scalar()
        d['stage'] = d['stage'] if (r.attempts or 0) > 0 else f'Queued · {ahead + 1} in line'
    return d


def cancel(ws: str, job_id: str) -> dict | None:
    """Queued jobs are cancelled at once; running jobs stop at their next progress report (within a few seconds)."""
    j = db.jobs_t
    with db.get_engine().begin() as c:
        r = c.execute(sa.select(j.c.status, j.c.input_key).where(j.c.id == job_id, j.c.workspace_id == ws)).first()
        if not r:
            return None
        if r.status == 'queued':
            if c.execute(sa.update(j).where(j.c.id == job_id, j.c.status == 'queued').values(
                    status='cancelled', stage='Cancelled', error='Cancelled.', finished_at=time.time())).rowcount and r.input_key:
                storage.get_storage().delete(r.input_key)
        elif r.status == 'running':
            c.execute(sa.update(j).where(j.c.id == job_id).values(cancel_requested=True, stage='Cancelling'))
    return get(ws, job_id)


def queue_depth() -> dict:
    j = db.jobs_t
    with db.system_context(), db.get_engine().connect() as c:
        rows = c.execute(sa.select(j.c.status, sa.func.count()).group_by(j.c.status)).all()
    return {s: int(n) for s, n in rows}
