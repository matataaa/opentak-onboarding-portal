"""add event_leader_sessions and event_sessions.leaderSessionId

- event_leader_sessions: one row per person who joined through a team's leader
  link (display name, revoked state). The leader JWT carries the row id, so one
  person can be revoked without regenerating the link. eventLinkId and userId
  have no FK so the audit trail survives link deletion.
- event_sessions.leaderSessionId: the leader session that created a device
  (no FK; NULL = not created by a leader).

Every step is skipped when it is already in place (databases built by
create_all and stamped).

Revision ID: e9f0a1b2c3d5
Revises: d8e9f0a1b2c4
Create Date: 2026-10-30 23:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e9f0a1b2c3d5'
down_revision = 'd8e9f0a1b2c4'
branch_labels = None
depends_on = None


def _offline():
    # Offline (--sql) mode has no connection to inspect; there the schema is
    # assumed to match the previous revision so the DDL is always emitted.
    return op.get_context().as_sql


def table_exists(table_name):
    """Check if a table exists (dialect-agnostic). Fresh inspector: it caches."""
    if _offline():
        return False
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def column_exists(table_name, column_name):
    """Check if a column exists (dialect-agnostic). Fresh inspector: it caches."""
    if _offline():
        return False
    return column_name in [c['name'] for c in sa.inspect(op.get_bind()).get_columns(table_name)]


def upgrade():
    if not table_exists('event_leader_sessions'):
        op.create_table(
            'event_leader_sessions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('eventId', sa.Integer(), nullable=False),
            sa.Column('eventTeamId', sa.Integer(), nullable=False),
            sa.Column('eventLinkId', sa.Integer(), nullable=True),
            sa.Column('userId', sa.Integer(), nullable=True),
            sa.Column('displayName', sa.String(length=80), nullable=False),
            sa.Column('createdAt', sa.DateTime(), nullable=True),
            sa.Column('revokedAt', sa.DateTime(), nullable=True),
            sa.Column('revokedBy', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['eventId'], ['events.id']),
            sa.ForeignKeyConstraint(['eventTeamId'], ['event_teams.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if not column_exists('event_sessions', 'leaderSessionId'):
        with op.batch_alter_table('event_sessions', schema=None) as batch_op:
            batch_op.add_column(sa.Column('leaderSessionId', sa.Integer(), nullable=True))


def downgrade():
    if column_exists('event_sessions', 'leaderSessionId') or _offline():
        with op.batch_alter_table('event_sessions', schema=None) as batch_op:
            batch_op.drop_column('leaderSessionId')

    if table_exists('event_leader_sessions') or _offline():
        op.drop_table('event_leader_sessions')
