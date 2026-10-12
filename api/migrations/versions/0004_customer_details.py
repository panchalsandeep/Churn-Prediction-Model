"""Split the wide encrypted blobs (attrs, reasons) out of `customers` into `customer_details`.

Revision ID: 0004
Revises: 0003

The hot table (`customers`) is what every list, sort, tier count and alert query touches. Keeping multi-KB ciphertext
in it bloats every page read. The details table is only read by key (drawer, alerts, segments, search).
"""
import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    # A legacy database adopted via create_all() may already have the new table and the new customers shape.
    if 'customer_details' not in insp.get_table_names():
        op.create_table(
            'customer_details',
            sa.Column('workspace_id', sa.String(80), primary_key=True),
            sa.Column('customer_id', sa.String(120), primary_key=True),
            sa.Column('reasons', sa.Text),
            sa.Column('attrs', sa.Text),
        )
    old = {c['name'] for c in insp.get_columns('customers')} & {'reasons', 'attrs'}
    if old == {'reasons', 'attrs'}:
        op.execute('INSERT INTO customer_details (workspace_id, customer_id, reasons, attrs) '
                   'SELECT workspace_id, customer_id, reasons, attrs FROM customers')
        with op.batch_alter_table('customers') as batch:
            batch.drop_column('reasons')
            batch.drop_column('attrs')
    if op.get_bind().dialect.name == 'postgresql':
        cond = "(workspace_id = current_setting('app.workspace_id', true) OR current_setting('app.workspace_id', true) = '*')"
        op.execute('ALTER TABLE customer_details ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE customer_details FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON customer_details USING ({cond}) WITH CHECK ({cond})')


def downgrade() -> None:
    with op.batch_alter_table('customers') as batch:
        batch.add_column(sa.Column('reasons', sa.Text, nullable=True))
        batch.add_column(sa.Column('attrs', sa.Text, nullable=True))
    op.execute('UPDATE customers SET reasons = (SELECT d.reasons FROM customer_details d WHERE d.workspace_id = customers.workspace_id '
               'AND d.customer_id = customers.customer_id), attrs = (SELECT d.attrs FROM customer_details d '
               'WHERE d.workspace_id = customers.workspace_id AND d.customer_id = customers.customer_id)')
    op.drop_table('customer_details')
