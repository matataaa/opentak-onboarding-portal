"""remove dead zerotier_icon setting

Revision ID: e3f4a5b6c7d9
Revises: d2e3f4a5b6c8
Create Date: 2026-10-30 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e3f4a5b6c7d9'
down_revision = 'd2e3f4a5b6c8'
branch_labels = None
depends_on = None


def upgrade():
    # The ZeroTier dashboard tile was removed; its setting is no longer read anywhere.
    op.execute(sa.text("DELETE FROM system_settings WHERE key = 'zerotier_icon'"))


def downgrade():
    op.execute(sa.text(
        "INSERT INTO system_settings (key, value, category, description, created_at, updated_at) "
        "SELECT 'zerotier_icon', 'false', 'general', 'Show ZeroTier download icon on the dashboard', "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
        "WHERE NOT EXISTS (SELECT 1 FROM system_settings WHERE key = 'zerotier_icon')"
    ))
