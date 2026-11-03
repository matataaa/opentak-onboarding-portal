"""add role_permissions

- role_permissions: the permission keys granted to a custom role, one row per
  (role, permission). Built-in roles have no rows. Starts empty.

Production SQLite does not enforce foreign keys, so the ON DELETE CASCADE only
works on PostgreSQL; UserRoleModel.permissions removes the rows through the ORM
on every backend.

Skipped when the table is already in place (databases built by create_all and
stamped).

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-11-01 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a8'
down_revision = 'a1b2c3d4e5f7'
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
    if not table_exists('role_permissions'):
        op.create_table(
            'role_permissions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('role_id', sa.Integer(), nullable=False),
            sa.Column('permission', sa.String(length=100), nullable=False),
            sa.ForeignKeyConstraint(['role_id'], ['user_roles.id'], name='fk_role_permissions_role_id_user_roles',
                                    ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('role_id', 'permission', name='uq_role_permissions_role_id_permission'),
        )


def downgrade():
    if table_exists('role_permissions') or _offline():
        op.drop_table('role_permissions')
