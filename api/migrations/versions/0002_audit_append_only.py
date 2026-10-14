"""audit log is append-only at the database level

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == 'sqlite':
        op.execute("CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END")
        op.execute("CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END")
    elif dialect == 'postgresql':
        op.execute("""
            CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'audit_log is append-only'; END;
            $$ LANGUAGE plpgsql
        """)
        op.execute('CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log '
                   'FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()')
        op.execute('CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log '
                   'FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()')


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == 'sqlite':
        op.execute('DROP TRIGGER IF EXISTS audit_log_no_update')
        op.execute('DROP TRIGGER IF EXISTS audit_log_no_delete')
    elif dialect == 'postgresql':
        op.execute('DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log')
        op.execute('DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log')
        op.execute('DROP FUNCTION IF EXISTS audit_log_append_only()')
