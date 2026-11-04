"""map users.onboardedBy usernames to user ids

users."onboardedBy" is a foreign key to users.id, but registration used to store
the onboarding contact's username in it. SQLite accepted that; PostgreSQL rejects
the INSERT. Rows holding a username get the id of that user, or NULL when no such
user exists. Integer values are left alone, so on PostgreSQL this is a no-op.
An all-digit username was already stored as an integer by SQLite and cannot be
told apart from an id, so it stays as it is.

Data only: no schema change, downgrade does nothing.

Revision ID: f1a2b3c4d5e7
Revises: b2c3d4e5f6a8
Create Date: 2026-11-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e7'
down_revision = 'b2c3d4e5f6a8'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        'SELECT id, "onboardedBy" FROM users WHERE "onboardedBy" IS NOT NULL'
    )).fetchall()
    for user_id, value in rows:
        if not isinstance(value, str):
            continue
        contact_id = bind.execute(
            sa.text('SELECT id FROM users WHERE lower(username) = :username'),
            {'username': value.lower()},
        ).scalar()
        bind.execute(
            sa.text('UPDATE users SET "onboardedBy" = :contact WHERE id = :id'),
            {'contact': contact_id, 'id': user_id},
        )


def downgrade():
    pass
