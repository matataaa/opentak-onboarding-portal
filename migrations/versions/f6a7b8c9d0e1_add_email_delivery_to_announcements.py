"""add email delivery tracking to announcements

Revision ID: f6a7b8c9d0e1
Revises: e2f3a4b5c6d7
Create Date: 2026-10-22 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade():
    # Plain ADD COLUMNs: existing rows get NULL / 0, so announcements that were
    # already sent are never mailed retroactively. email_status has no CHECK
    # constraint (values are validated in code).
    with op.batch_alter_table('announcements', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email_status', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('email_attempts', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('email_next_attempt_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('email_last_error', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('email_pending_user_ids', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('email_failures', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('announcements', schema=None) as batch_op:
        batch_op.drop_column('email_failures')
        batch_op.drop_column('email_pending_user_ids')
        batch_op.drop_column('email_last_error')
        batch_op.drop_column('email_next_attempt_at')
        batch_op.drop_column('email_attempts')
        batch_op.drop_column('email_status')
