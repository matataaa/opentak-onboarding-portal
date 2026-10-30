"""add language to pending_registrations

Revision ID: d2e3f4a5b6c8
Revises: c1d2e3f4a5b7
Create Date: 2026-10-30 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2e3f4a5b6c8'
down_revision = 'c1d2e3f4a5b7'
branch_labels = None
depends_on = None

TABLE = 'pending_registrations'
COLUMN = 'language'


def _offline():
    # Offline (--sql) mode has no connection to inspect; there the schema is
    # assumed to match the previous revision so the DDL is always emitted.
    return op.get_context().as_sql


def column_exists(table_name, column_name):
    """Check if a column already exists in a table (dialect-agnostic)."""
    if _offline():
        return False
    inspector = sa.inspect(op.get_bind())
    return column_name in [col["name"] for col in inspector.get_columns(table_name)]


def upgrade():
    # Nullable, no server default: NULL means the registrant gave no language.
    if column_exists(TABLE, COLUMN):
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.String(length=5), nullable=True))


def downgrade():
    if not (column_exists(TABLE, COLUMN) or _offline()):
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_column(COLUMN)
