"""PostgreSQL-only guarantees: row-level security and database-enforced append-only audit.

Skipped unless TEST_DATABASE_URL points at a PostgreSQL database. Connect as a role that is NOT a superuser and does not have
BYPASSRLS (superusers skip RLS by design). CI provisions such a role; see .github/workflows/ci.yml.

STATUS: written to run in CI against a Postgres service container. It has not been executed on a local Postgres in this repository's
development environment (no Postgres or running Docker daemon was available).
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

PG_URL = os.environ.get('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not (PG_URL or '').startswith('postgresql'), reason='needs TEST_DATABASE_URL (PostgreSQL)')


@pytest.fixture(scope='module')
def pg(env):
    from churn import db
    assert db.get_engine().dialect.name == 'postgresql'
    yield db


def insert_customer(db, ws, cid):
    with db.workspace_scope(ws), db.get_engine().begin() as c:
        c.execute(db.customers_t.insert().values(workspace_id=ws, customer_id=cid, model_id='m', prob=0.5, tier='Low'))


def test_role_is_not_a_superuser_and_cannot_bypass_rls(pg):
    with pg.system_context(), pg.get_engine().connect() as c:
        su, bypass = c.execute(sa.text('SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user')).one()
    assert not su and not bypass, 'run the tests as a plain role, otherwise RLS is not being tested'


def test_every_tenant_table_has_forced_row_level_security(pg):
    tables = ['customers', 'alerts', 'interventions', 'jobs', 'models', 'score_batches', 'audit_log', 'workspace_kv',
              'workspace_keys', 'memberships', 'invites', 'workspaces', 'workspace_settings']
    with pg.system_context(), pg.get_engine().connect() as c:
        rows = {r.relname: (r.relrowsecurity, r.relforcerowsecurity)
                for r in c.execute(sa.text('SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)'), {'t': tables})}
    assert set(rows) == set(tables) and all(rows[t] == (True, True) for t in tables), rows


def test_a_tenant_sees_only_its_own_rows_even_without_a_where_clause(pg):
    insert_customer(pg, 'tenant_a', 'A1')
    insert_customer(pg, 'tenant_b', 'B1')
    with pg.workspace_scope('tenant_a'), pg.get_engine().connect() as c:
        ids = {r.customer_id for r in c.execute(sa.select(pg.customers_t.c.customer_id))}      # deliberately unfiltered
    assert ids == {'A1'}


def test_no_tenant_context_means_no_rows_fail_closed(pg):
    insert_customer(pg, 'tenant_a', 'A2')
    with pg.get_engine().connect() as c:                                                     # no workspace context at all
        assert c.execute(sa.select(sa.func.count()).select_from(pg.customers_t)).scalar() == 0


def test_a_tenant_cannot_write_rows_into_another_tenant(pg):
    with pytest.raises(Exception, match='row-level security'):
        with pg.workspace_scope('tenant_a'), pg.get_engine().begin() as c:
            c.execute(pg.customers_t.insert().values(workspace_id='tenant_b', customer_id='SNEAK', model_id='m', prob=0.1, tier='Low'))


def test_system_context_sees_everything_but_is_only_set_explicitly(pg):
    with pg.system_context(), pg.get_engine().connect() as c:
        ids = {r.customer_id for r in c.execute(sa.select(pg.customers_t.c.customer_id))}
    assert {'A1', 'B1'} <= ids
    assert pg.workspace_context.get() == ''                                                  # and it did not leak out of the block


def test_audit_log_is_append_only_in_postgres(pg):
    pg.audit('tenant_a', 'tester', 'pg.test')
    with pytest.raises(Exception, match='append-only'):
        with pg.workspace_scope('tenant_a'), pg.get_engine().begin() as c:
            c.execute(sa.text("UPDATE audit_log SET actor = 'forged' WHERE workspace_id = 'tenant_a'"))
    with pytest.raises(Exception, match='append-only'):
        with pg.workspace_scope('tenant_a'), pg.get_engine().begin() as c:
            c.execute(sa.text('TRUNCATE audit_log'))
    assert pg.verify_audit('tenant_a')['ok'] is True


def test_concurrent_audit_appends_keep_one_unbroken_chain(pg):
    from concurrent.futures import ThreadPoolExecutor

    def write(i):
        with pg.workspace_scope('chain_ws'):
            pg.audit('chain_ws', 'racer', f'event.{i}')

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(write, range(40)))
    with pg.workspace_scope('chain_ws'):
        verdict = pg.verify_audit('chain_ws')
    assert verdict == {'ok': True, 'checked': 40, 'first_bad_id': None}
