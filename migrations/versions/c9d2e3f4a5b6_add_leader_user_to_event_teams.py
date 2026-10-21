"""add leaderUserId to event teams

Revision ID: c9d2e3f4a5b6
Revises: b8e1f2a3c4d5
Create Date: 2026-10-21 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9d2e3f4a5b6'
down_revision = 'b8e1f2a3c4d5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('event_teams', schema=None) as batch_op:
        batch_op.add_column(sa.Column('leaderUserId', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_event_teams_leader_user', 'users', ['leaderUserId'], ['id']
        )


def downgrade():
    with op.batch_alter_table('event_teams', schema=None) as batch_op:
        batch_op.drop_constraint('fk_event_teams_leader_user', type_='foreignkey')
        batch_op.drop_column('leaderUserId')
