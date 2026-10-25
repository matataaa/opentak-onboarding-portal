"""add device account fields to event_sessions

Revision ID: c1d2e3f4a5b7
Revises: b9c0d1e2f3a4
Create Date: 2026-10-25 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b7'
down_revision = 'b9c0d1e2f3a4'
branch_labels = None
depends_on = None

TABLE = 'event_sessions'
FK_NAME = 'fk_event_sessions_event_link'


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


def fk_exists(table_name, fk_name):
    if _offline():
        return False
    inspector = sa.inspect(op.get_bind())
    return fk_name in [fk["name"] for fk in inspector.get_foreign_keys(table_name)]


def upgrade():
    add_parked = not column_exists(TABLE, 'parkedAt')
    add_hash = not column_exists(TABLE, 'clientKeyHash')
    add_link = not column_exists(TABLE, 'eventLinkId')
    add_fk = not fk_exists(TABLE, FK_NAME)

    if not (add_parked or add_hash or add_link or add_fk):
        return

    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        if add_parked:
            batch_op.add_column(sa.Column('parkedAt', sa.DateTime(), nullable=True))
        if add_hash:
            batch_op.add_column(sa.Column('clientKeyHash', sa.String(length=128), nullable=True))
        if add_link:
            batch_op.add_column(sa.Column('eventLinkId', sa.Integer(), nullable=True))
        if add_fk:
            batch_op.create_foreign_key(FK_NAME, 'event_links', ['eventLinkId'], ['id'])


def downgrade():
    drop_fk = fk_exists(TABLE, FK_NAME) or _offline()
    drop_cols = [
        name for name in ('eventLinkId', 'clientKeyHash', 'parkedAt')
        if column_exists(TABLE, name) or _offline()
    ]

    if not (drop_fk or drop_cols):
        return

    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        if drop_fk:
            batch_op.drop_constraint(FK_NAME, type_='foreignkey')
        for name in drop_cols:
            batch_op.drop_column(name)
