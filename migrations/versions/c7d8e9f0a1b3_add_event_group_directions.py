"""add event group directions, organizer team and session role

- event_links.direction: which way the device accounts of a link take part in
  their team's OTS group (BOTH, IN = send only, OUT = receive only).
- events.organizerTeamId / events.organizerDirection: an organizer (HQ) team
  and how it takes part in every team group of the event.
- event_sessions.role: marks special device sessions (e.g. a leader's own device).
- event_sessions.direction / event_sessions.otsGroupId: the direction and team
  group a device account was actually granted in OTS (NULL = legacy: BOTH and
  the team's current group). Open device sessions are backfilled with BOTH and
  their team's current group, which is what they were given before this change.

Allowed values are validated by the API, not by CHECK constraints. Every step
is skipped when it is already in place (databases built by create_all and
stamped).

Revision ID: c7d8e9f0a1b3
Revises: b6c7d8e9f0a2
Create Date: 2026-10-30 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7d8e9f0a1b3'
down_revision = 'b6c7d8e9f0a2'
branch_labels = None
depends_on = None

FK_NAME = 'fk_events_organizerTeamId_event_teams'


def _offline():
    # Offline (--sql) mode has no connection to inspect; there the schema is
    # assumed to match the previous revision so the DDL is always emitted.
    return op.get_context().as_sql


def column_exists(table_name, column_name):
    """Check if a column exists (dialect-agnostic). Fresh inspector: it caches."""
    if _offline():
        return False
    return column_name in [c['name'] for c in sa.inspect(op.get_bind()).get_columns(table_name)]


def organizer_fk_name():
    """Name of the FK on events.organizerTeamId, '' if unnamed, None if absent."""
    if _offline():
        return None
    for fk in sa.inspect(op.get_bind()).get_foreign_keys('events'):
        if fk['constrained_columns'] == ['organizerTeamId']:
            return fk.get('name') or ''
    return None


def upgrade():
    if not column_exists('event_links', 'direction'):
        with op.batch_alter_table('event_links', schema=None) as batch_op:
            batch_op.add_column(sa.Column('direction', sa.String(length=4), nullable=False,
                                          server_default='BOTH'))

    add_direction = not column_exists('events', 'organizerDirection')
    add_team = not column_exists('events', 'organizerTeamId')
    add_fk = organizer_fk_name() is None
    if add_direction or add_team or add_fk:
        with op.batch_alter_table('events', schema=None) as batch_op:
            if add_direction:
                batch_op.add_column(sa.Column('organizerDirection', sa.String(length=4), nullable=False,
                                              server_default='OUT'))
            if add_team:
                batch_op.add_column(sa.Column('organizerTeamId', sa.Integer(), nullable=True))
            if add_fk:
                batch_op.create_foreign_key(FK_NAME, 'event_teams', ['organizerTeamId'], ['id'])

    session_cols = [
        sa.Column(name, type_, nullable=True)
        for name, type_ in (('role', sa.String(length=20)), ('direction', sa.String(length=4)),
                            ('otsGroupId', sa.Integer()))
        if not column_exists('event_sessions', name)
    ]
    if session_cols:
        with op.batch_alter_table('event_sessions', schema=None) as batch_op:
            for col in session_cols:
                batch_op.add_column(col)

    # Device accounts created before this revision hold IN + OUT on their team's group
    op.execute(
        'UPDATE "event_sessions" SET "direction" = \'BOTH\', "otsGroupId" = ('
        'SELECT "event_teams"."otsGroupId" FROM "event_teams" '
        'WHERE "event_teams"."id" = "event_sessions"."eventTeamId") '
        'WHERE "mode" = \'device\' AND "closedAt" IS NULL AND "direction" IS NULL AND "otsGroupId" IS NULL'
    )


def downgrade():
    drop_session_cols = [name for name in ('otsGroupId', 'direction', 'role')
                         if column_exists('event_sessions', name) or _offline()]
    if drop_session_cols:
        with op.batch_alter_table('event_sessions', schema=None) as batch_op:
            for name in drop_session_cols:
                batch_op.drop_column(name)

    fk_name = FK_NAME if _offline() else organizer_fk_name()
    drop_team = column_exists('events', 'organizerTeamId') or _offline()
    drop_direction = column_exists('events', 'organizerDirection') or _offline()
    if fk_name is not None or drop_team or drop_direction:
        with op.batch_alter_table('events', schema=None) as batch_op:
            if fk_name:
                batch_op.drop_constraint(fk_name, type_='foreignkey')
            # An unnamed FK (SQLite create_all) goes away with the batch table rebuild.
            if drop_team:
                batch_op.drop_column('organizerTeamId')
            if drop_direction:
                batch_op.drop_column('organizerDirection')

    if column_exists('event_links', 'direction') or _offline():
        with op.batch_alter_table('event_links', schema=None) as batch_op:
            batch_op.drop_column('direction')
