"""Shared fixtures: an isolated data dir + database, inline jobs, and one trained workspace."""
from __future__ import annotations

import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))

os.environ.pop('VERCEL', None)
os.environ.pop('AUTH_FAIL_CLOSED', None)
os.environ.pop('AUTH_REQUIRED', None)
os.environ['FLASK_ENV'] = 'development'
os.environ['SYNC_JOBS'] = '1'


@pytest.fixture(scope='module')
def env(tmp_path_factory):
    """Fresh DATA_DIR and sqlite DB for a test module."""
    data = tmp_path_factory.mktemp('data')
    os.environ['DATA_DIR'] = str(data)
    os.environ.pop('DATABASE_URL', None)
    pg_url = os.environ.get('TEST_DATABASE_URL')
    if pg_url:                       # run the suite against PostgreSQL, one throw-away schema per test module
        import uuid
        import sqlalchemy as sa
        schema = f't_{uuid.uuid4().hex[:8]}'
        with sa.create_engine(pg_url, isolation_level='AUTOCOMMIT').connect() as c:
            c.exec_driver_sql(f'CREATE SCHEMA {schema}')
        os.environ['DATABASE_URL'] = pg_url + ('&' if '?' in pg_url else '?') + f'options=-csearch_path%3D{schema}'
    from churn import config, crypto, db, jobs, registry, storage
    from churn.security import limiter
    config.SYNC_JOBS = True
    db.reset_engine()
    jobs.reset_state()
    registry.forget()
    storage.reset()
    crypto.reset()
    limiter.reset()
    yield data
    db.reset_engine()


@pytest.fixture(scope='module')
def client(env):
    import index
    index.app.config['TESTING'] = True
    with index.app.test_client() as c:
        yield c


@pytest.fixture(scope='module')
def demo_csv_bytes():
    from churn.demo_data import generate_demo
    return generate_demo(3000, seed=11).to_csv(index=False).encode()


@pytest.fixture(scope='module')
def trained(client, demo_csv_bytes):
    """Train once per module through the real upload endpoint."""
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(demo_csv_bytes), 'demo.csv')}, content_type='multipart/form-data')
    assert res.status_code == 200, res.get_json()
    job = res.get_json()['job']
    assert job['status'] == 'done', job
    return job['result']


@pytest.fixture(autouse=True)
def ambient_workspace_context(request):
    """On PostgreSQL (row-level security) a test that calls services directly has no request to set a tenant. Give it the system
    context, except in the RLS suite itself, which must run with an empty context to prove the database fails closed."""
    if not os.environ.get('TEST_DATABASE_URL') or request.node.path.name == 'test_phase0_postgres.py':
        yield
        return
    from churn import db
    token = db.workspace_context.set('*')
    yield
    db.workspace_context.reset(token)
