"""add events module tables

Revision ID: b8e1f2a3c4d5
Revises: a7f3c9e21b45
Create Date: 2026-10-21 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8e1f2a3c4d5'
down_revision = 'a7f3c9e21b45'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'event_teams',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('otsGroupId', sa.Integer(), nullable=True),
        sa.Column('color', sa.String(length=20), nullable=True),
        sa.Column('createdAt', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['otsGroupId'], ['ots_groups.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('validFrom', sa.DateTime(), nullable=True),
        sa.Column('validUntil', sa.DateTime(), nullable=True),
        sa.Column('cutoffTime', sa.String(length=5), nullable=True),
        sa.Column('timezone', sa.String(length=64), nullable=True),
        sa.Column('playerAccessMode', sa.String(length=20), nullable=False),
        sa.Column('accountCleanup', sa.String(length=20), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('createdAt', sa.DateTime(), nullable=True),
        sa.Column('createdBy', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['createdBy'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'event_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('eventId', sa.Integer(), nullable=False),
        sa.Column('eventTeamId', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('tokenHash', sa.String(length=128), nullable=False),
        sa.Column('tokenVersion', sa.Integer(), nullable=False),
        sa.Column('createdAt', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['eventId'], ['events.id']),
        sa.ForeignKeyConstraint(['eventTeamId'], ['event_teams.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tokenHash'),
    )
    op.create_table(
        'event_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('eventId', sa.Integer(), nullable=False),
        sa.Column('eventTeamId', sa.Integer(), nullable=False),
        sa.Column('otsUsername', sa.String(length=200), nullable=True),
        sa.Column('mode', sa.String(length=20), nullable=True),
        sa.Column('createdAt', sa.DateTime(), nullable=True),
        sa.Column('expiresAt', sa.DateTime(), nullable=True),
        sa.Column('closedAt', sa.DateTime(), nullable=True),
        sa.Column('closeReason', sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(['eventId'], ['events.id']),
        sa.ForeignKeyConstraint(['eventTeamId'], ['event_teams.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('event_sessions')
    op.drop_table('event_links')
    op.drop_table('events')
    op.drop_table('event_teams')
