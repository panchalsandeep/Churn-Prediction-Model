"""Phase 0 migrations: a fresh install, an upgrade from the old create_all() schema, and no drift between models and migrations."""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from churn import db


def new_engine(tmp_path, name='m.db'):
    return sa.create_engine('sqlite:///' + str(tmp_path / name).replace(os.sep, '/'))


def test_fresh_database_reaches_head_with_no_drift_from_the_models(tmp_path):
    engine = new_engine(tmp_path)
    db.migrate(engine)
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={'compare_type': True}), db.metadata)
        version = conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar()
    assert diffs == [], f'models and migrations disagree: {diffs}'
    assert version == '0007'
    tables = set(sa.inspect(engine).get_table_names())
    assert {'workspaces', 'memberships', 'invites', 'workspace_keys', 'audit_log', 'customers'} <= tables


def test_migrations_are_idempotent(tmp_path):
    engine = new_engine(tmp_path)
    db.migrate(engine)
    db.migrate(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT COUNT(*) FROM alembic_version')).scalar() == 1


def test_audit_log_is_append_only_after_migration(tmp_path):
    engine = new_engine(tmp_path)
    db.migrate(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO audit_log (workspace_id, action, ts) VALUES ('w', 'a', 1.0)"))
        with pytest.raises(Exception, match='append-only'):
            conn.execute(sa.text("UPDATE audit_log SET action = 'b'"))
    with engine.begin() as conn:
        with pytest.raises(Exception, match='append-only'):
            conn.execute(sa.text('DELETE FROM audit_log'))


def test_a_database_made_by_the_old_create_all_is_adopted_without_data_loss(tmp_path):
    """The previous version created tables with create_all(): no alembic_version, no workspace tables, no audit hash columns."""
    engine = new_engine(tmp_path, 'legacy.db')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE models (id VARCHAR(32) PRIMARY KEY, workspace_id VARCHAR(80) NOT NULL, created_at FLOAT NOT NULL, '
                             'algorithm VARCHAR(40), status VARCHAR(16) NOT NULL, pr_auc FLOAT, roc_auc FLOAT, schema_signature VARCHAR(24), '
                             'artifact_path TEXT, data_path TEXT, meta TEXT NOT NULL)')
        conn.exec_driver_sql('CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id VARCHAR(80) NOT NULL, actor VARCHAR(120), '
                             'action VARCHAR(60) NOT NULL, detail TEXT, ts FLOAT NOT NULL)')
        conn.exec_driver_sql("INSERT INTO models VALUES ('m1','local',1.0,'rf','champion',0.5,0.8,'sig','p','d','{}')")
        conn.exec_driver_sql("INSERT INTO audit_log (workspace_id, actor, action, ts) VALUES ('local','me','old.event',1.0)")
    db.migrate(engine)
    insp = sa.inspect(engine)
    assert {'prev_hash', 'hash'} <= {c['name'] for c in insp.get_columns('audit_log')}
    assert {'workspaces', 'memberships', 'workspace_keys', 'customers'} <= set(insp.get_table_names())
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT id FROM models")).scalar() == 'm1'                         # data survived
        assert conn.execute(sa.text("SELECT action FROM audit_log")).scalar() == 'old.event'
        assert conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == '0007'
    with engine.begin() as conn:                                                                        # and the log is now protected
        with pytest.raises(Exception, match='append-only'):
            conn.execute(sa.text("UPDATE audit_log SET actor = 'x'"))


def test_0004_moves_wide_columns_to_customer_details_without_losing_rows(tmp_path):
    from alembic import command
    from alembic.config import Config
    engine = new_engine(tmp_path, 'split.db')
    cfg = Config()
    cfg.set_main_option('script_location', db._MIGRATIONS_DIR)
    with engine.begin() as conn:
        cfg.attributes['connection'] = conn
        command.upgrade(cfg, '0003')
        conn.exec_driver_sql("INSERT INTO customers (workspace_id, customer_id, model_id, prob, tier, reasons, attrs) "
                             "VALUES ('w','c1','m',0.5,'High','R1','A1'), ('w','c2','m',0.1,'Low',NULL,'A2')")
    with engine.begin() as conn:
        cfg.attributes['connection'] = conn
        command.upgrade(cfg, '0004')
    with engine.connect() as conn:
        assert {c['name'] for c in sa.inspect(conn).get_columns('customers')}.isdisjoint({'attrs', 'reasons'})
        rows = {r[0]: (r[1], r[2]) for r in conn.exec_driver_sql('SELECT customer_id, reasons, attrs FROM customer_details')}
        assert rows == {'c1': ('R1', 'A1'), 'c2': (None, 'A2')}
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM customers').scalar() == 2
    with engine.begin() as conn:                                  # and it can be reversed
        cfg.attributes['connection'] = conn
        command.downgrade(cfg, '0003')
        assert conn.exec_driver_sql("SELECT attrs FROM customers WHERE customer_id='c1'").scalar() == 'A1'


def test_pre_chain_audit_entries_do_not_break_verification(env):
    """Entries written before the hash chain existed have no hash; they are skipped, later entries still verify."""
    with db.get_engine().begin() as conn:
        conn.execute(sa.text("INSERT INTO audit_log (workspace_id, actor, action, ts) VALUES ('legacy-ws', 'me', 'old', 1.0)"))
    db.audit('legacy-ws', 'me', 'new')
    assert db.verify_audit('legacy-ws') == {'ok': True, 'checked': 1, 'first_bad_id': None}
