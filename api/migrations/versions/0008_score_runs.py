"""Prediction log index for realized-performance monitoring, shadow evaluation and backtests.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if 'score_runs' not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            'score_runs',
            sa.Column('id', sa.String(32), primary_key=True),
            sa.Column('workspace_id', sa.String(80), nullable=False),
            sa.Column('model_id', sa.String(32), nullable=False),
            sa.Column('kind', sa.String(8), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('as_of', sa.String(10)),
            sa.Column('horizon_days', sa.Integer),
            sa.Column('scored_at', sa.Float, nullable=False),
            sa.Column('rows', sa.Integer, nullable=False),
            sa.Column('status', sa.String(10), nullable=False),
            sa.Column('log_key', sa.Text),
            sa.Column('expected', sa.Text),
            sa.Column('realized', sa.Text),
            sa.Column('resolved_at', sa.Float),
        )
        op.create_index('ix_score_runs_workspace_id', 'score_runs', ['workspace_id'])
        op.create_index('ix_score_runs_ws_status', 'score_runs', ['workspace_id', 'status'])
    if op.get_bind().dialect.name == 'postgresql':
        cond = "(workspace_id = current_setting('app.workspace_id', true) OR current_setting('app.workspace_id', true) = '*')"
        op.execute('ALTER TABLE score_runs ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE score_runs FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON score_runs USING ({cond}) WITH CHECK ({cond})')


def downgrade() -> None:
    op.drop_table('score_runs')
