"""add onboardingCodeId to event links

Revision ID: d1e2f3a4b5c6
Revises: c9d2e3f4a5b6
Create Date: 2026-10-21 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = 'c9d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('event_links', schema=None) as batch_op:
        batch_op.add_column(sa.Column('onboardingCodeId', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_event_links_onboarding_code', 'onboardingcodes', ['onboardingCodeId'], ['id']
        )


def downgrade():
    with op.batch_alter_table('event_links', schema=None) as batch_op:
        batch_op.drop_constraint('fk_event_links_onboarding_code', type_='foreignkey')
        batch_op.drop_column('onboardingCodeId')
