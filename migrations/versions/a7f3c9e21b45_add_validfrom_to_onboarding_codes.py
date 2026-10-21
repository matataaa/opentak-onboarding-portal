"""add validFrom to onboarding codes

Revision ID: a7f3c9e21b45
Revises: 4b754g491aa1
Create Date: 2026-10-21 10:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7f3c9e21b45'
down_revision = '4b754g491aa1'
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table keeps this safe on SQLite as well as PostgreSQL.
    with op.batch_alter_table('onboardingcodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('validFrom', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('onboardingcodes', schema=None) as batch_op:
        batch_op.drop_column('validFrom')
