"""
What a process does when it loads the app: one-off `flask` commands must not start the
scheduler or touch the database; a server (gunicorn, `flask run`) must, and seeds the roles.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy.pool import StaticPool

from app.models import db as _db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = """
import sys, json
sys.argv = {argv!r}
from app import app
from app.extensions import scheduler
print('PROBE' + json.dumps({{'running': scheduler.running, 'jobs': len(scheduler.get_jobs())}}))
scheduler.shutdown(wait=False) if scheduler.running else None
"""


def _env(db_path, **extra):
    env = {k: v for k, v in os.environ.items() if k != 'FLASK_RUN_FROM_CLI'}
    env.update({'TESTING': 'False', 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
                'SECRET_KEY': 'startup-test-secret-key', 'JWT_SECRET_KEY': 'startup-test-jwt-secret-key'})
    env.update(extra)
    return env


def _probe(db_path, argv, **extra):
    proc = subprocess.run([sys.executable, '-c', PROBE.format(argv=argv)], cwd=ROOT, env=_env(db_path, **extra),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    line = next(l for l in proc.stdout.splitlines() if l.startswith('PROBE'))
    return json.loads(line[len('PROBE'):])


def _tables(db_path):
    if not os.path.exists(db_path):
        return set()
    con = sqlite3.connect(db_path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_one_off_cli_command_starts_nothing(tmp_path):
    db_path = tmp_path / 'db.sqlite'
    state = _probe(db_path, ['flask', 'db', 'upgrade'], FLASK_RUN_FROM_CLI='true')
    assert state == {'running': False, 'jobs': 0}
    assert _tables(db_path) == set()


def test_flask_run_still_starts_the_scheduler(tmp_path):
    state = _probe(tmp_path / 'db.sqlite', ['flask', 'run'], FLASK_RUN_FROM_CLI='true')
    assert state['running'] is True and state['jobs'] > 0


@pytest.fixture
def migrated(tmp_path):
    db_path = tmp_path / 'db.sqlite'
    proc = subprocess.run([sys.executable, '-m', 'flask', '--app', 'app', 'db', 'upgrade'], cwd=ROOT,
                          env=_env(db_path), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    return db_path


def test_roles_seed_at_first_server_start_after_upgrade(migrated):
    con = sqlite3.connect(migrated)
    roles = {r[0] for r in con.execute('SELECT name FROM user_roles')}
    con.close()
    assert 'user_admin' not in roles  # flask db upgrade itself does not seed

    state = _probe(migrated, ['gunicorn', 'app:app'])
    assert state['running'] is True and state['jobs'] > 0

    con = sqlite3.connect(migrated)
    roles = {r[0] for r in con.execute('SELECT name FROM user_roles')}
    settings_count = con.execute('SELECT count(*) FROM system_settings').fetchone()[0]
    con.close()
    assert {'administrator', 'user_admin', 'user_readonly'} <= roles
    assert settings_count > 0


def test_pool_pre_ping_reaches_the_engine(app):
    assert app.config['SQLALCHEMY_ENGINE_OPTIONS']['pool_pre_ping'] is True
    assert _db.engine.pool._pre_ping is True
    assert isinstance(_db.engine.pool, StaticPool)  # the in-memory test database keeps working
