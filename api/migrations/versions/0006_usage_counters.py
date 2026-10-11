"""Usage metering: one counter per workspace, calendar month and metric.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if 'usage_counters' not in sa.inspect(op.get_bind()).get_table_names():       # legacy adoption may have created it already
        op.create_table(
            'usage_counters',
            sa.Column('workspace_id', sa.String(80), primary_key=True),
            sa.Column('period', sa.String(7), primary_key=True),
            sa.Column('metric', sa.String(24), primary_key=True),
            sa.Column('value', sa.Float, nullable=False, server_default='0'),
        )
    if op.get_bind().dialect.name == 'postgresql':
        cond = "(workspace_id = current_setting('app.workspace_id', true) OR current_setting('app.workspace_id', true) = '*')"
        op.execute('ALTER TABLE usage_counters ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE usage_counters FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON usage_counters USING ({cond}) WITH CHECK ({cond})')


def downgrade() -> None:
    op.drop_table('usage_counters')
