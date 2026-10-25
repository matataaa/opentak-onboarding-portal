"""add tokenNonce to event_links

Revision ID: b9c0d1e2f3a4
Revises: a7b8c9d0e1f2
Create Date: 2026-10-23 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b9c0d1e2f3a4'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def column_exists(table_name, column_name):
    """Check if a column already exists in a table (dialect-agnostic).

    Offline (--sql) mode has no connection to inspect; there the schema is
    assumed to match the previous revision so the DDL is always emitted.
    """
    if op.get_context().as_sql:
        return False
    inspector = sa.inspect(op.get_bind())
    return column_name in [col["name"] for col in inspector.get_columns(table_name)]


def upgrade():
    # NULL = legacy link whose token cannot be re-derived.
    if not column_exists('event_links', 'tokenNonce'):
        with op.batch_alter_table('event_links', schema=None) as batch_op:
            batch_op.add_column(sa.Column('tokenNonce', sa.String(length=64), nullable=True))


def downgrade():
    if column_exists('event_links', 'tokenNonce') or op.get_context().as_sql:
        with op.batch_alter_table('event_links', schema=None) as batch_op:
            batch_op.drop_column('tokenNonce')
