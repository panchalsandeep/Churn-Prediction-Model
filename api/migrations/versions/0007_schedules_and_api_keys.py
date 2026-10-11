"""Scheduled pipeline runs and machine API keys for data ingestion.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if 'schedules' not in have:
        op.create_table(
            'schedules',
            sa.Column('workspace_id', sa.String(80), primary_key=True),
            sa.Column('kind', sa.String(24), primary_key=True),
            sa.Column('every_hours', sa.Float, nullable=False),
            sa.Column('next_run_at', sa.Float, nullable=False),
            sa.Column('enabled', sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column('last_job_id', sa.String(32)),
            sa.Column('last_status', sa.String(200)),
            sa.Column('updated_at', sa.Float, nullable=False),
        )
        op.create_index('ix_schedules_next_run_at', 'schedules', ['next_run_at'])
    if 'api_keys' not in have:
        op.create_table(
            'api_keys',
            sa.Column('id', sa.String(16), primary_key=True),
            sa.Column('workspace_id', sa.String(80), nullable=False),
            sa.Column('name', sa.String(80), nullable=False),
            sa.Column('key_hash', sa.String(64), nullable=False),
            sa.Column('scope', sa.String(16), nullable=False),
            sa.Column('created_by', sa.String(254)),
            sa.Column('created_at', sa.Float, nullable=False),
            sa.Column('last_used_at', sa.Float),
            sa.Column('revoked_at', sa.Float),
        )
        op.create_index('ix_api_keys_workspace_id', 'api_keys', ['workspace_id'])
        op.create_index('ix_api_keys_key_hash', 'api_keys', ['key_hash'], unique=True)
    if op.get_bind().dialect.name == 'postgresql':
        cond = "(workspace_id = current_setting('app.workspace_id', true) OR current_setting('app.workspace_id', true) = '*')"
        for table in ('schedules', 'api_keys'):
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
            op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({cond}) WITH CHECK ({cond})')


def downgrade() -> None:
    op.drop_table('api_keys')
    op.drop_table('schedules')
