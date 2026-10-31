"""make announcements.created_by and api_keys.created_by nullable

A user who created announcements or API keys can be deleted; those records stay
and lose their creator (created_by NULL).

The foreign keys to users.id are left as they are, without ON DELETE: like the
other references to users, the delete code clears these columns itself. They
also have no common name to address them by (announcements_created_by_fkey on
PostgreSQL, unnamed on SQLite). On SQLite the batch mode rebuilds the tables
from reflection, which keeps the foreign keys, the key_hash unique constraint,
the named CHECK constraints of b6c7d8e9f0a2 and the server defaults.

Databases built by create_all and stamped already have the nullable columns:
each column is only altered when it is not in the wanted state yet.

Downgrade refuses to run while any created_by is NULL, before changing anything:
NOT NULL can't hold those rows, and deleting them or making up a creator would
lose or falsify data. The error lists the affected tables; the admin decides
what happens to those rows and runs the downgrade again.

Revision ID: a1b2c3d4e5f7
Revises: f0a1b2c3d4e6
Create Date: 2026-10-31 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f7'
down_revision = 'f0a1b2c3d4e6'
branch_labels = None
depends_on = None

TABLES = ('announcements', 'api_keys')


def _offline():
    # Offline (--sql) mode has no connection to inspect; there the schema is
    # assumed to match the previous revision so the DDL is always emitted.
    return op.get_context().as_sql


def created_by_nullable(table_name):
    """Fresh inspector: it caches."""
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return next(c['nullable'] for c in columns if c['name'] == 'created_by')


def set_created_by_nullable(nullable):
    for table_name in TABLES:
        if _offline() or created_by_nullable(table_name) != nullable:
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.alter_column('created_by', existing_type=sa.Integer(), nullable=nullable)


def upgrade():
    set_created_by_nullable(True)


def downgrade():
    if not _offline():  # offline, ALTER ... SET NOT NULL itself fails on NULL rows
        bind = op.get_bind()
        null_rows = {
            table_name: bind.execute(sa.text(
                f'SELECT COUNT(*) FROM "{table_name}" WHERE "created_by" IS NULL')).scalar()
            for table_name in TABLES
        }
        if any(null_rows.values()):
            found = ', '.join(f'{table_name}: {count}' for table_name, count in null_rows.items() if count)
            raise RuntimeError(
                f'Cannot downgrade {revision}: rows with created_by NULL exist ({found}). '
                'Their creator was deleted and the column becomes NOT NULL again. '
                'Assign a creator to these rows or delete them, then run the downgrade again. '
                'Nothing was changed.'
            )

    set_created_by_nullable(False)
