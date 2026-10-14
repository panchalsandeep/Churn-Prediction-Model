"""Durable job queue: claimable rows with heartbeats, retries, cancellation and idempotency keys.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A legacy database adopted via create_all() already has the current shape of `jobs`; only add what is missing.
    insp = sa.inspect(op.get_bind())
    have = {c['name'] for c in insp.get_columns('jobs')}
    cols = [
        sa.Column('params', sa.Text, nullable=True),
        sa.Column('input_key', sa.Text, nullable=True),
        sa.Column('attempts', sa.Integer, nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer, nullable=False, server_default='3'),
        sa.Column('run_after', sa.Float, nullable=True),
        sa.Column('locked_by', sa.String(80), nullable=True),
        sa.Column('heartbeat_at', sa.Float, nullable=True),
        sa.Column('started_at', sa.Float, nullable=True),
        sa.Column('cancel_requested', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('idempotency_key', sa.String(80), nullable=True),
    ]
    missing = [c for c in cols if c.name not in have]
    if missing:
        with op.batch_alter_table('jobs') as batch:
            for c in missing:
                batch.add_column(c)
    existing = {i['name'] for i in insp.get_indexes('jobs')}
    if 'ix_jobs_claim' not in existing:
        op.create_index('ix_jobs_claim', 'jobs', ['status', 'run_after', 'created_at'])
    if 'ux_jobs_idempotency' not in existing:
        op.create_index('ux_jobs_idempotency', 'jobs', ['workspace_id', 'idempotency_key'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_jobs_idempotency', 'jobs')
    op.drop_index('ix_jobs_claim', 'jobs')
    with op.batch_alter_table('jobs') as batch:
        for col in ('idempotency_key', 'cancel_requested', 'started_at', 'heartbeat_at', 'locked_by', 'run_after',
                    'max_attempts', 'attempts', 'input_key', 'params'):
            batch.drop_column(col)
