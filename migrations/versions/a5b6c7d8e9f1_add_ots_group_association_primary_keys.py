"""add primary keys to the OTS group association tables

The models declare composite primary keys on group_user_association
(group_id, user_id) and group_onboardingcode_association (group_id,
onboardingcode_id), but 5cc784adacae created both tables without one, so
duplicate pairs could be stored (and updating such a row raises StaleDataError).

Duplicate pairs are merged into one row first: only IN rows -> IN, only OUT
rows -> OUT, anything else (IN + OUT, any BOTH or unknown value) -> BOTH.

Tables built by create_all and stamped already have the primary key; tables
that already have any primary key are left alone.

Revision ID: a5b6c7d8e9f1
Revises: f4a5b6c7d8e0
Create Date: 2026-10-30 18:00:00.000000

"""
import logging

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a5b6c7d8e9f1'
down_revision = 'f4a5b6c7d8e0'
branch_labels = None
depends_on = None

logger = logging.getLogger('alembic.env')

# (table, second primary key column); the first one is group_id
ASSOCIATION_TABLES = [
    ('group_onboardingcode_association', 'onboardingcode_id'),
    ('group_user_association', 'user_id'),
]


def pk_name(table_name):
    # PostgreSQL's default name, which is what create_all ends up with there
    return f'{table_name}_pkey'


def table_exists(table_name):
    """Check if a table exists (dialect-agnostic). Fresh inspector: it caches."""
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def get_pk(table_name):
    return sa.inspect(op.get_bind()).get_pk_constraint(table_name)


def merge_duplicates(table_name, col):
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(
        f'SELECT "group_id", "{col}", '
        f'SUM(CASE WHEN "direction" = \'IN\' THEN 1 ELSE 0 END), '
        f'SUM(CASE WHEN "direction" = \'OUT\' THEN 1 ELSE 0 END), '
        f'SUM(CASE WHEN "direction" NOT IN (\'IN\', \'OUT\') THEN 1 ELSE 0 END) '
        f'FROM "{table_name}" GROUP BY "group_id", "{col}" HAVING COUNT(*) > 1'
    )).all()
    for group_id, other_id, n_in, n_out, n_other in duplicates:
        if n_in and not n_out and not n_other:
            direction = 'IN'
        elif n_out and not n_in and not n_other:
            direction = 'OUT'
        else:
            direction = 'BOTH'
        params = {'group_id': group_id, 'other_id': other_id, 'direction': direction}
        bind.execute(sa.text(
            f'DELETE FROM "{table_name}" WHERE "group_id" = :group_id AND "{col}" = :other_id'
        ), params)
        bind.execute(sa.text(
            f'INSERT INTO "{table_name}" ("group_id", "{col}", "direction") '
            f'VALUES (:group_id, :other_id, :direction)'
        ), params)
    if duplicates:
        logger.info('%s: merged %d duplicate (group_id, %s) pair(s)', table_name, len(duplicates), col)


def upgrade():
    for table_name, col in ASSOCIATION_TABLES:
        if not table_exists(table_name) or get_pk(table_name)['constrained_columns']:
            continue
        merge_duplicates(table_name, col)
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_primary_key(pk_name(table_name), ['group_id', col])


def downgrade():
    # Merged duplicate rows are not split again. Only the primary key this
    # migration creates (matched by name) is dropped; an unnamed one from
    # create_all on SQLite is kept, and f4a5b6c7d8e0's downgrade copes with it.
    # On PostgreSQL create_all uses the same default name, so its key is
    # dropped too; upgrading again restores it.
    for table_name, col in reversed(ASSOCIATION_TABLES):
        if not table_exists(table_name):
            continue
        pk = get_pk(table_name)
        if pk.get('name') == pk_name(table_name) and pk['constrained_columns'] == ['group_id', col]:
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_constraint(pk_name(table_name), type_='primary')
