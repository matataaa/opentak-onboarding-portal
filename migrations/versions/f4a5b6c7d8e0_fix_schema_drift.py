"""fix schema drift between models and migration history

Brings the migrated schema in line with app/models.py so `flask db check` is clean:

- drop the unused approver_onboardingcode_association table
- delete association rows with a NULL foreign key, then make group_id /
  onboardingcode_id / user_id NOT NULL on the two OTS group association tables
- drop the unused ots_groups.direction column
- make users.id NOT NULL (it is the primary key; already the case on
  PostgreSQL, so this only changes SQLite)

docker-entrypoint.sh can stamp a database without migrating it, so the actual
schema may not match the history: every step checks the live schema first.

Downgrade note: association rows deleted for having a NULL foreign key are not
restored, and users.id and primary key columns are left NOT NULL (see
downgrade()).

Revision ID: f4a5b6c7d8e0
Revises: e3f4a5b6c7d9
Create Date: 2026-10-30 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4a5b6c7d8e0'
down_revision = 'e3f4a5b6c7d9'
branch_labels = None
depends_on = None


# (table, foreign key columns) for the OTS group association tables
ASSOCIATION_TABLES = [
    ('group_onboardingcode_association', ('group_id', 'onboardingcode_id')),
    ('group_user_association', ('group_id', 'user_id')),
]


def table_exists(table_name):
    """Check if a table exists (dialect-agnostic). Fresh inspector: it caches."""
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def get_column(table_name, column_name):
    """Return the reflected column dict, or None if the table/column is missing."""
    if not table_exists(table_name):
        return None
    inspector = sa.inspect(op.get_bind())
    for col in inspector.get_columns(table_name):
        if col["name"] == column_name:
            return col
    return None


def column_exists(table_name, column_name):
    return get_column(table_name, column_name) is not None


def column_nullable(table_name, column_name):
    col = get_column(table_name, column_name)
    return col is not None and col["nullable"]


def upgrade():
    if table_exists('approver_onboardingcode_association'):
        op.drop_table('approver_onboardingcode_association')

    for table_name, columns in ASSOCIATION_TABLES:
        if not all(column_exists(table_name, c) for c in columns):
            continue
        where = " OR ".join(f'"{c}" IS NULL' for c in columns)
        op.execute(sa.text(f'DELETE FROM "{table_name}" WHERE {where}'))

        nullable_columns = [c for c in columns if column_nullable(table_name, c)]
        if nullable_columns:
            with op.batch_alter_table(table_name) as batch_op:
                for c in nullable_columns:
                    batch_op.alter_column(c, existing_type=sa.Integer(), nullable=False)

    if column_exists('ots_groups', 'direction'):
        with op.batch_alter_table('ots_groups') as batch_op:
            batch_op.drop_column('direction')

    if column_nullable('users', 'id'):
        with op.batch_alter_table('users') as batch_op:
            batch_op.alter_column('id', existing_type=sa.Integer(), nullable=False)


def downgrade():
    # users.id stays NOT NULL: it is the primary key, and PostgreSQL refuses a
    # nullable primary key column anyway.

    if table_exists('ots_groups') and not column_exists('ots_groups', 'direction'):
        with op.batch_alter_table('ots_groups') as batch_op:
            batch_op.add_column(sa.Column('direction', sa.String(), nullable=True))

    # Rows deleted for having a NULL foreign key are not restored. Columns that
    # are part of a primary key (databases built by create_all and stamped) stay
    # NOT NULL, since PostgreSQL refuses nullable primary key columns.
    for table_name, columns in reversed(ASSOCIATION_TABLES):
        if not table_exists(table_name):
            continue
        pk = set(sa.inspect(op.get_bind()).get_pk_constraint(table_name)['constrained_columns'] or [])
        not_nullable = [c for c in columns
                        if c not in pk and column_exists(table_name, c)
                        and not column_nullable(table_name, c)]
        if not_nullable:
            with op.batch_alter_table(table_name) as batch_op:
                for c in not_nullable:
                    batch_op.alter_column(c, existing_type=sa.Integer(), nullable=True)

    if not table_exists('approver_onboardingcode_association'):
        op.create_table('approver_onboardingcode_association',
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('onboardingcode_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['onboardingcode_id'], ['onboardingcodes.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], )
        )
