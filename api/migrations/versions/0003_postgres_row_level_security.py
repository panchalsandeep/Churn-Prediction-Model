"""PostgreSQL row-level security: a tenant can only ever see its own rows, even if application code forgets a WHERE clause.

Revision ID: 0003
Revises: 0002

How it works
  * The app sets `app.workspace_id` for every transaction (db.py: begin hook, taken from a context variable that the request
    or job runner fills in). With no workspace set, the setting is empty and *no rows are visible* (fail closed).
  * `'*'` means "system context" and is only set inside `db.system_context()` for the few cross-tenant operations
    (login-time membership lookup, job-queue admission, the worker claiming jobs).
  * FORCE ROW LEVEL SECURITY makes the policy apply to the table owner too. For defence in depth the application should connect
    as a role that does NOT own the tables and is not a superuser (superusers and BYPASSRLS roles skip RLS).

PostgreSQL only. On SQLite this migration does nothing (isolation there is enforced in application code and tested).
NOTE: written and CI-tested against a Postgres service container; not exercised on a local Postgres in this repository's development environment.
"""
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

TENANT_TABLES = ['workspace_settings', 'models', 'customers', 'alerts', 'interventions', 'jobs', 'score_batches',
                 'audit_log', 'workspace_kv', 'workspace_keys', 'memberships', 'invites']
POLICY_TABLES = {'workspaces': 'id'}                       # keyed by `id` instead of `workspace_id`

USING = ("({col} = current_setting('app.workspace_id', true) OR current_setting('app.workspace_id', true) = '*')")


def upgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    for table in TENANT_TABLES:
        _protect(table, 'workspace_id')
    for table, col in POLICY_TABLES.items():
        _protect(table, col)


def _protect(table: str, col: str) -> None:
    op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
    cond = USING.format(col=col)
    op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({cond}) WITH CHECK ({cond})')


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    for table in [*TENANT_TABLES, *POLICY_TABLES]:
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {table}')
        op.execute(f'ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} DISABLE ROW LEVEL SECURITY')
