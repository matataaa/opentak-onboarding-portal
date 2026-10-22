"""add ots_missing_since to ots_groups

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    # Plain nullable ADD COLUMN (NULL = group still exists in OTS). No table
    # recreate on SQLite, so the UNIQUE constraint on name is untouched.
    with op.batch_alter_table('ots_groups', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ots_missing_since', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('ots_groups', schema=None) as batch_op:
        batch_op.drop_column('ots_missing_since')
