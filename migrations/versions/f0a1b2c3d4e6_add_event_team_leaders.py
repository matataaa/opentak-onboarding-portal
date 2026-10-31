"""add event_team_leaders

- event_team_leaders: portal users designated as leader of a team for one event
  (personal leaders, own login). source 'admin' (set by an admin) or 'link'
  (bound by scanning the leader link while logged in; eventLinkId/linkVersion/
  linkNonce record the link it is valid for, eventLinkId without FK). One row per
  (event, team, user).

Skipped when the table is already in place (databases built by create_all and
stamped).

Revision ID: f0a1b2c3d4e6
Revises: e9f0a1b2c3d5
Create Date: 2026-10-31 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f0a1b2c3d4e6'
down_revision = 'e9f0a1b2c3d5'
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


def upgrade():
    if not table_exists('event_team_leaders'):
        op.create_table(
            'event_team_leaders',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('eventId', sa.Integer(), nullable=False),
            sa.Column('eventTeamId', sa.Integer(), nullable=False),
            sa.Column('userId', sa.Integer(), nullable=False),
            sa.Column('source', sa.String(length=10), nullable=False),
            sa.Column('eventLinkId', sa.Integer(), nullable=True),
            sa.Column('linkVersion', sa.Integer(), nullable=True),
            sa.Column('linkNonce', sa.String(length=64), nullable=True),
            sa.Column('createdAt', sa.DateTime(), nullable=True),
            sa.Column('createdBy', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['eventId'], ['events.id']),
            sa.ForeignKeyConstraint(['eventTeamId'], ['event_teams.id']),
            sa.ForeignKeyConstraint(['userId'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('eventId', 'eventTeamId', 'userId', name='uq_event_team_leaders_event_team_user'),
        )


def downgrade():
    if table_exists('event_team_leaders') or _offline():
        op.drop_table('event_team_leaders')
