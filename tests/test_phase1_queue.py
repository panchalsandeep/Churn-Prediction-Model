"""Phase 1: the durable job queue (claims, heartbeats, reaping, retries, cancellation, idempotency, separate worker process)."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pandas as pd
import pytest
import sqlalchemy as sa

from churn import config, crypto, db, handlers, jobs, storage  # noqa: F401  (handlers registers the real kinds)

WS = 'q-ws'
CALLS: list = []


@pytest.fixture(autouse=True)
def async_mode(env, monkeypatch):
    """Queue tests drive workers by hand: no inline execution, no background threads."""
    monkeypatch.setattr(config, 'SYNC_JOBS', False)
    monkeypatch.setattr(config, 'EMBEDDED_WORKER', False)
    monkeypatch.setattr(config, 'JOB_HEARTBEAT_S', 0.1)
    monkeypatch.setattr(config, 'JOB_STALE_S', 0.5)
    monkeypatch.setattr(config, 'JOB_MAX_ATTEMPTS', 2)
    with db.get_engine().begin() as c:
        c.execute(sa.delete(db.jobs_t))
    CALLS.clear()
    yield


@jobs.handler('t_echo')
def _echo(ws, params, df, progress):
    progress('working', 50)
    CALLS.append((ws, params, None if df is None else len(df)))
    return {'echo': params.get('v'), 'rows': None if df is None else len(df)}


@jobs.handler('t_slow')
def _slow(ws, params, df, progress):
    for i in range(200):
        progress('looping', i % 90)
        time.sleep(0.05)
    return {'finished': True}


@jobs.handler('t_value_error')
def _bad(ws, params, df, progress):
    raise ValueError('your column is wrong')


@jobs.handler('t_flaky')
def _flaky(ws, params, df, progress):
    CALLS.append('try')
    if len(CALLS) == 1:
        raise ConnectionError('database went away')
    return {'ok': True}


def row(job_id):
    with db.system_context(), db.get_engine().connect() as c:
        return c.execute(sa.select(db.jobs_t).where(db.jobs_t.c.id == job_id)).one()


def test_job_runs_with_params_and_encrypted_input_that_is_deleted_afterwards():
    df = pd.DataFrame({'a': [1, 2, 3], 'secret': ['x', 'y', 'z']})
    jid = jobs.submit(WS, 't_echo', {'v': 7}, df)
    r = row(jid)
    assert r.status == 'queued' and r.input_key
    blob = storage.get_storage().get(r.input_key)
    assert blob.startswith(crypto.MAGIC) and b'secret' not in blob             # never plaintext at rest
    assert jobs.run_next('w1')
    done = jobs.get(WS, jid)
    assert done['status'] == 'done' and done['result'] == {'echo': 7, 'rows': 3}
    assert not storage.get_storage().exists(r.input_key)                       # customer data does not outlive the job
    assert not jobs.run_next('w1')                                              # nothing left


def test_two_workers_never_claim_the_same_job():
    for i in range(6):
        with db.system_context(), db.get_engine().begin() as c:
            c.execute(db.jobs_t.insert().values(id=f'j{i}', workspace_id=f'w{i}', kind='t_echo', status='queued', progress=0,
                                                created_at=time.time() + i, attempts=0, max_attempts=2, cancel_requested=False))
    won, lock = [], threading.Lock()

    def grab(name):
        for _ in range(6):
            j = jobs.claim(name)
            if j:
                with lock:
                    won.append(j['id'])

    ts = [threading.Thread(target=grab, args=(f'w{k}',)) for k in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(won) == [f'j{i}' for i in range(6)]                           # each exactly once


def test_idempotency_key_returns_the_original_job():
    a = jobs.submit(WS, 't_echo', {'v': 1}, None, 'upload-123')
    b = jobs.submit(WS, 't_echo', {'v': 2}, None, 'upload-123')
    assert a == b
    with db.system_context(), db.get_engine().connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(db.jobs_t)).scalar() == 1


def test_one_active_job_per_workspace_and_a_bounded_queue(monkeypatch):
    jobs.submit(WS, 't_echo', {}, None)
    with pytest.raises(jobs.JobRejected) as e:
        jobs.submit(WS, 't_echo', {}, None)
    assert e.value.status == 409
    monkeypatch.setattr(config, 'MAX_QUEUED_JOBS', 1)
    with pytest.raises(jobs.JobRejected) as e:
        jobs.submit('another', 't_echo', {}, None)
    assert e.value.status == 503


def test_user_errors_fail_at_once_without_retry():
    jid = jobs.submit(WS, 't_value_error', {}, None)
    jobs.run_next('w')
    r = jobs.get(WS, jid)
    assert r['status'] == 'error' and r['error'] == 'your column is wrong' and r['attempts'] == 1


def test_transient_errors_are_retried_with_backoff():
    jid = jobs.submit(WS, 't_flaky', {}, None)
    jobs.run_next('w')
    r = row(jid)
    assert r.status == 'queued' and r.run_after > time.time() and r.attempts == 1
    assert not jobs.run_next('w')                                               # still backing off
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.update(db.jobs_t).values(run_after=time.time() - 1))
    assert jobs.run_next('w')
    assert jobs.get(WS, jid)['status'] == 'done' and len(CALLS) == 2


def test_a_dead_worker_is_detected_and_its_job_requeued_then_failed_when_attempts_run_out():
    jid = jobs.submit(WS, 't_echo', {'v': 1}, None)
    assert jobs.claim('dead-worker')                                            # claimed, then the process "dies": no heartbeat
    time.sleep(0.7)
    assert jobs.reap() == 1
    r = row(jid)
    assert r.status == 'queued' and r.locked_by is None and r.attempts == 1
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.update(db.jobs_t).values(run_after=None))
    assert jobs.claim('second-dead-worker')
    time.sleep(0.7)
    jobs.reap()
    r = row(jid)
    assert r.status == 'error' and 'retries are exhausted' in r.error


def test_a_reaped_zombie_cannot_overwrite_its_replacement():
    jid = jobs.submit(WS, 't_echo', {}, None)
    jobs.claim('zombie')
    time.sleep(0.7)
    jobs.reap()
    assert jobs.claim('replacement') is None                                       # backing off
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.update(db.jobs_t).values(run_after=None))
    assert jobs.claim('replacement')
    assert jobs._finish(jid, 'zombie', status='done', result='{}') is False        # not the owner any more
    assert row(jid).status == 'running' and row(jid).locked_by == 'replacement'


def test_cancel_a_queued_job_and_a_running_one():
    a = jobs.submit(WS, 't_echo', {}, pd.DataFrame({'a': [1]}))
    key = row(a).input_key
    assert jobs.cancel(WS, a)['status'] == 'cancelled' and not storage.get_storage().exists(key)
    assert not jobs.run_next('w')

    b = jobs.submit(WS, 't_slow', {}, None)
    t = threading.Thread(target=jobs.run_next, args=('w',))
    t.start()
    time.sleep(0.4)
    assert jobs.cancel(WS, b)['stage'] == 'Cancelling'
    t.join(10)
    assert not t.is_alive() and jobs.get(WS, b)['status'] == 'cancelled'


def test_jobs_are_invisible_across_workspaces():
    jid = jobs.submit(WS, 't_echo', {}, None)
    assert jobs.get('someone-else', jid) is None
    assert jobs.cancel('someone-else', jid) is None
    assert jobs.get(WS, jid)['status'] == 'queued'


def test_a_separate_worker_process_runs_a_real_training_job(env):
    """The web side only enqueues; `python -m churn.worker` in another process trains the model and the web side reads the result."""
    from churn import registry
    jid = jobs.submit(WS, 'demo', {'rows': 1200, 'algorithm': 'logistic_regression', 'actor': 'test'}, None)
    api_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'api')
    child_env = {**os.environ, 'PYTHONPATH': api_dir, 'DATA_DIR': os.environ['DATA_DIR'], 'EMBEDDED_WORKER': '0', 'SYNC_JOBS': '0',
                 'DATABASE_URL': config.database_url(), 'MAX_CONCURRENT_JOBS': '1', 'JOB_POLL_S': '0.2', 'PYTHONIOENCODING': 'utf-8'}
    proc = subprocess.Popen([sys.executable, '-m', 'churn.worker'], env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 240
        state = None
        while time.time() < deadline:
            state = jobs.get(WS, jid)
            if state['status'] in jobs.TERMINAL:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    finally:
        proc.terminate()
        out = proc.communicate(timeout=30)[0].decode('utf-8', 'replace')
    assert state and state['status'] == 'done', (state, out[-2000:])
    registry.forget()
    assert registry.champion(WS) is not None                                   # the model it trained is visible to this process
