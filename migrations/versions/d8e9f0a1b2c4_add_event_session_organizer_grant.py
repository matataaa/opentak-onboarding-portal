"""add event_sessions.organizerGrant

The organizer (HQ) memberships a device account was granted on the groups of
the other teams of its event, as a JSON list of [group name, IN|OUT] pairs
(NULL = none). Changes and the close job remove exactly these, even after the
event's teams or organizer settings changed. Skipped when the column already
exists (databases built by create_all and stamped).

Revision ID: d8e9f0a1b2c4
Revises: c7d8e9f0a1b3
Create Date: 2026-10-30 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8e9f0a1b2c4'
down_revision = 'c7d8e9f0a1b3'
branch_labels = None
depends_on = None


def column_exists(table_name, column_name):
    """Check if a column exists (dialect-agnostic); offline (--sql) mode assumes it does not."""
    if op.get_context().as_sql:
        return False
    return column_name in [c['name'] for c in sa.inspect(op.get_bind()).get_columns(table_name)]


def upgrade():
    if not column_exists('event_sessions', 'organizerGrant'):
        with op.batch_alter_table('event_sessions', schema=None) as batch_op:
            batch_op.add_column(sa.Column('organizerGrant', sa.Text(), nullable=True))


def downgrade():
    if column_exists('event_sessions', 'organizerGrant') or op.get_context().as_sql:
        with op.batch_alter_table('event_sessions', schema=None) as batch_op:
            batch_op.drop_column('organizerGrant')
