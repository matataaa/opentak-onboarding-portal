"""add the announcement and radio CHECK constraints declared in the models

app/models.py declares check_announcement_target_type, check_announcement_status
and check_radio_type, but no migration created them. Existing values outside
the allowed sets are rewritten first (and logged) so the constraints can be
added; the replacements keep the app's current behaviour for those rows:

- announcements.status invalid -> 'draft': an unknown status is already treated
  like a draft (not shown to users, not published by the scheduler, editable).
- announcements.target_type invalid -> 'users', and status -> 'draft': an
  unknown target type currently reaches nobody. 'all' would suddenly show the
  announcement to everyone; as an unpublished draft targeting its explicit user
  list it stays invisible until an admin reviews and sends it. A row that was
  'sent' keeps sent_at and its email status; sending it again is an explicit
  admin action.
- radios.radioType invalid -> 'other': anything but 'meshtastic' is already
  treated as a non-Meshtastic radio.

Databases built by create_all and stamped already have the constraints: each
one is only added when no check constraint with that name exists.

Downgrade drops the constraints; rewritten values are not restored.

Revision ID: b6c7d8e9f0a2
Revises: a5b6c7d8e9f1
Create Date: 2026-10-30 18:30:00.000000

"""
import logging

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b6c7d8e9f0a2'
down_revision = 'a5b6c7d8e9f1'
branch_labels = None
depends_on = None

logger = logging.getLogger('alembic.env')

# table -> [(constraint name, condition)], conditions exactly as in app/models.py
CHECKS = {
    'announcements': [
        ('check_announcement_target_type', "target_type IN ('all', 'roles', 'users')"),
        ('check_announcement_status', "status IN ('draft', 'scheduled', 'sent')"),
    ],
    'radios': [
        ('check_radio_type', "\"radioType\" IN ('meshtastic', 'other')"),
    ],
}

# (table, column, allowed values, SET clause for rows outside them)
NORMALISE = [
    ('announcements', 'target_type', ('all', 'roles', 'users'), "\"target_type\" = 'users', \"status\" = 'draft'"),
    ('announcements', 'status', ('draft', 'scheduled', 'sent'), "\"status\" = 'draft'"),
    ('radios', 'radioType', ('meshtastic', 'other'), "\"radioType\" = 'other'"),
]


def table_exists(table_name):
    """Check if a table exists (dialect-agnostic). Fresh inspector: it caches."""
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def check_names(table_name):
    return {c['name'] for c in sa.inspect(op.get_bind()).get_check_constraints(table_name)}


def normalise(table_name, column, allowed, set_clause):
    bind = op.get_bind()
    allowed_sql = ', '.join(f"'{v}'" for v in allowed)
    where = f'"{column}" IS NULL OR "{column}" NOT IN ({allowed_sql})'
    rows = bind.execute(sa.text(f'SELECT "id", "{column}" FROM "{table_name}" WHERE {where}')).all()
    if not rows:
        return
    for row_id, value in rows:
        logger.info('%s id=%s: invalid %s %r, rewriting (%s)', table_name, row_id, column, value, set_clause)
    bind.execute(sa.text(f'UPDATE "{table_name}" SET {set_clause} WHERE {where}'))


def upgrade():
    for table_name, column, allowed, set_clause in NORMALISE:
        if table_exists(table_name):
            normalise(table_name, column, allowed, set_clause)

    for table_name, checks in CHECKS.items():
        if not table_exists(table_name):
            continue
        existing = check_names(table_name)
        missing = [(name, cond) for name, cond in checks if name not in existing]
        if missing:
            with op.batch_alter_table(table_name) as batch_op:
                for name, cond in missing:
                    batch_op.create_check_constraint(name, cond)


def downgrade():
    for table_name, checks in reversed(list(CHECKS.items())):
        if not table_exists(table_name):
            continue
        existing = check_names(table_name)
        present = [name for name, _ in checks if name in existing]
        if present:
            with op.batch_alter_table(table_name) as batch_op:
                for name in present:
                    batch_op.drop_constraint(name, type_='check')
