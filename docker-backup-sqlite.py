#!/usr/bin/env python3
"""
Back up the SQLite database before the entrypoint applies pending migrations.

Resolves SQLALCHEMY_DATABASE_URI like app/settings.py and Flask-SQLAlchemy do
(env/.env, default sqlite:///db.sqlite, relative paths under instance/), without
importing the Flask app. Does nothing for non-SQLite databases, a missing database
file, or a database that is already at the migration head. Keeps at most one backup
per starting revision while the database is unchanged, so a crash-looping container
cannot fill the disk.
Exits non-zero if a needed backup cannot be made, so the migration does not run.
"""

import glob
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = os.path.dirname(os.path.abspath(__file__))


def sqlite_path():
    load_dotenv(os.path.join(ROOT, '.env'))
    url = make_url(os.environ.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///db.sqlite'))
    if url.drivername not in ('sqlite', 'sqlite+pysqlite'):
        return None
    if url.database in (None, '', ':memory:'):
        return None
    path = url.database[5:] if url.query.get('uri') else url.database
    if not os.path.isabs(path):
        path = os.path.join(ROOT, 'instance', path)
    return path


def main():
    db = sqlite_path()
    if not db or not os.path.exists(db):
        return

    src = sqlite3.connect(Path(db).as_uri() + '?mode=ro', uri=True)
    try:
        try:
            current = {row[0] for row in src.execute('SELECT version_num FROM alembic_version')}
        except sqlite3.OperationalError:
            current = set()
        heads = set(ScriptDirectory(os.path.join(ROOT, 'migrations')).get_heads())
        if current == heads:
            return

        rev = '_'.join(sorted(current)) or 'base'
        # Skip only if a backup for this revision is at least as new as the database
        # (a crash loop doesn't change it; a restore and further use does)
        existing = [p for p in glob.glob(f'{glob.escape(db)}.pre-{rev}-*')
                    if os.path.getmtime(p) >= os.path.getmtime(db)]
        if existing:
            print(f'Database backup for revision {rev} already exists: {existing[0]}')
            return

        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        target = f'{db}.pre-{rev}-{stamp}'
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        except Exception:
            dst.close()
            os.remove(target)
            raise
        dst.close()
        print(f'Backed up database (revision {rev}) to {target} before migrating')
    finally:
        src.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'ERROR: could not back up the database before migrating: {e}', file=sys.stderr)
        sys.exit(1)
