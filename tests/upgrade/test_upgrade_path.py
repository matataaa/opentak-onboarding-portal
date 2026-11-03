"""
Opt-in upgrade-path test.

Proves that an install sitting at the last upstream release revision upgrades
to our head without data loss (other than the documented, intentional losses),
that the result is usable through the ORM, and that downgrade/upgrade round
trips.

Run with:
    RUN_UPGRADE_TESTS=1 pytest --no-cov -q tests/upgrade
    RUN_UPGRADE_TESTS=1 UPGRADE_TEST_PG_URL=postgresql://user:pw@host:5432/postgres \
        pytest --no-cov -q tests/upgrade

SQLite always runs; PostgreSQL runs when UPGRADE_TEST_PG_URL is set (the role
needs CREATEDB; a throw-away database portal_upgrade_<random> is created and
dropped per run).

Migrations run in a subprocess (`flask db ...`) exactly as docker-entrypoint.sh
does, so nothing here depends on the test process's app/db state.
"""
import json
import os
import subprocess
import sys
import textwrap
import uuid
from collections import Counter
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

pytestmark = [
    pytest.mark.upgrade,
    pytest.mark.skipif(
        not os.environ.get('RUN_UPGRADE_TESTS'),
        reason='opt-in: set RUN_UPGRADE_TESTS=1 (and UPGRADE_TEST_PG_URL for PostgreSQL)',
    ),
]

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# Head of the last upstream release. A new upstream migration means this (and
# SEEDS below) must be revisited on purpose.
UPSTREAM_REV = '4b754g491aa1'

HEAD_REV = 'b2c3d4e5f6a8'
# The revision whose downgrade refuses while created_by is NULL.
CREATOR_NULLABLE_REV = 'a1b2c3d4e5f7'

# a5b6c7d8e9f1 adds the primary keys to the OTS group association tables and
# merges duplicate (group, user) / (group, code) rows into one row first.
EXPECT_DUPLICATES_MERGED = True

# Same environment tests/conftest.py provides (app/settings.py reads it at import).
BASE_ENV = {
    'TESTING': 'True',
    'DEBUG': 'False',
    'SECRET_KEY': 'test-secret-key',
    'JWT_SECRET_KEY': 'test-jwt-secret-key-at-least-32-bytes',
    'OTS_URL': 'http://localhost:8080',
    'OTS_USERNAME': 'test',
    'OTS_PASSWORD': 'test',
    'MAIL_ENABLED': 'False',
    'MAIL_PORT': '25',
    'MAIL_USE_TLS': 'False',
    'MAIL_USE_SSL': 'False',
}

# Tables that intentionally lose data between UPSTREAM_REV and head.
#   approver_onboardingcode_association: table dropped (f4a5b6c7d8e0)
#   group_*_association: rows with a NULL FK deleted (f4a5b6c7d8e0), duplicate
#     pairs merged (a5b6c7d8e9f1)
#   system_settings: zerotier_icon row deleted (e3f4a5b6c7d9)
DROPPED_TABLES = {'approver_onboardingcode_association'}
# column dropped (f4a5b6c7d8e0)
DROPPED_COLUMNS = {'ots_groups': {'direction'}}
# Columns added by our migrations: table -> {column: expected value on pre-existing rows}
ADDED_COLUMNS = {
    'onboardingcodes': {'validFrom': None},
    'announcements': {
        'email_status': None, 'email_attempts': 0, 'email_next_attempt_at': None,
        'email_last_error': None, 'email_pending_user_ids': None, 'email_failures': None,
    },
    'ots_groups': {'ots_missing_since': None},
    'pending_registrations': {'language': None},
}
NEW_EMPTY_TABLES = {
    'event_teams': {'id', 'name', 'otsGroupId', 'color', 'createdAt', 'leaderUserId'},
    'events': {'id', 'name', 'description', 'validFrom', 'validUntil', 'playerAccessMode',
               'accountCleanup', 'active', 'createdAt', 'createdBy', 'organizerTeamId',
               'organizerDirection'},
    'event_links': {'id', 'eventId', 'eventTeamId', 'role', 'tokenHash', 'tokenVersion',
                    'createdAt', 'onboardingCodeId', 'tokenNonce', 'direction'},
    'event_sessions': {'id', 'eventId', 'eventTeamId', 'otsUsername', 'mode', 'createdAt',
                       'expiresAt', 'closedAt', 'closeReason', 'parkedAt', 'clientKeyHash',
                       'eventLinkId', 'role', 'direction', 'otsGroupId', 'organizerGrant',
                       'leaderSessionId'},
    'event_leader_sessions': {'id', 'eventId', 'eventTeamId', 'eventLinkId', 'userId', 'displayName',
                              'createdAt', 'revokedAt', 'revokedBy'},
    'event_team_leaders': {'id', 'eventId', 'eventTeamId', 'userId', 'source', 'eventLinkId', 'linkVersion',
                           'linkNonce', 'createdAt', 'createdBy'},
    'role_permissions': {'id', 'role_id', 'permission'},
}

NOW = datetime(2026, 1, 15, 12, 0, 0)


# --------------------------------------------------------------------------
# subprocess / database plumbing
# --------------------------------------------------------------------------

def _env(db_url):
    env = dict(os.environ)
    env.update(BASE_ENV)
    env['SQLALCHEMY_DATABASE_URI'] = db_url
    return env


def _run(db_url, argv, check=True):
    proc = subprocess.run(
        argv, cwd=REPO_ROOT, env=_env(db_url),
        capture_output=True, text=True, timeout=300,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"{' '.join(argv[2:])} exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def flask_db(db_url, *args, check=True):
    return _run(db_url, [sys.executable, '-m', 'flask', '--app', 'app', 'db', *args], check)


def current_heads(db_url):
    out = flask_db(db_url, 'heads').stdout
    return [ln.split()[0] for ln in out.splitlines() if '(head)' in ln]


def _backends():
    params = [pytest.param('sqlite', id='sqlite')]
    pg = os.environ.get('UPGRADE_TEST_PG_URL')
    params.append(pytest.param('pg', id='postgresql', marks=[] if pg else pytest.mark.skip(
        reason='UPGRADE_TEST_PG_URL not set')))
    return params


@pytest.fixture(scope='module')
def make_db(tmp_path_factory):
    """Factory returning a fresh empty database URL for the backend; cleans up."""
    cleanups = []
    tmp_path = tmp_path_factory.mktemp('upgrade')

    def factory(backend):
        if backend == 'sqlite':
            path = tmp_path / f'db-{uuid.uuid4().hex[:8]}.sqlite'
            return f'sqlite:///{path}'  # tmp_path is absolute -> sqlite:////abs/path
        admin_url = make_url(os.environ['UPGRADE_TEST_PG_URL'])
        name = f'portal_upgrade_{uuid.uuid4().hex[:10]}'
        admin = sa.create_engine(admin_url, isolation_level='AUTOCOMMIT')
        with admin.connect() as c:
            c.execute(sa.text(f'CREATE DATABASE "{name}"'))

        def drop():
            with admin.connect() as c:
                c.execute(sa.text(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname = :n AND pid <> pg_backend_pid()'), {'n': name})
                c.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
            admin.dispose()
        cleanups.append(drop)
        return admin_url.set(database=name).render_as_string(hide_password=False)

    yield factory
    for fn in reversed(cleanups):
        fn()


# --------------------------------------------------------------------------
# seed data: every table that exists at UPSTREAM_REV
# --------------------------------------------------------------------------

class Seeder:
    def __init__(self, conn, meta):
        self.conn, self.meta = conn, meta
        self.ids = {}

    def ins(self, table, as_=None, **values):
        res = self.conn.execute(self.meta.tables[table].insert().values(**values))
        pk = res.inserted_primary_key
        pk = pk[0] if pk and len(pk) == 1 else None
        if as_:
            self.ids[as_] = pk
        return pk


def seed_user_roles(s):
    s.ins('user_roles', 'role_admin', name='administrator', description='Full access', display_name='Administrator')
    s.ins('user_roles', 'role_member', name='member', description='Regular member', display_name='Member')
    s.ins('user_roles', 'role_approver', name='approver', description=None, display_name=None)


def seed_oidc_providers(s):
    s.ins('oidc_providers', 'oidc1', name='keycloak', display_name='Keycloak', button_color='#0b5ed7',
          discovery_url='https://sso.example.org/.well-known/openid-configuration',
          client_id='portal', client_secret='s3cr3t', enabled=True, role_claim='groups',
          role_mappings='{"tak-admin": "administrator"}', created_at=NOW, updated_at=NOW,
          sync_roles=True, icon_url='https://sso.example.org/icon.png')


def seed_users(s):
    ids = s.ids
    ids['user1'] = s.ins('users', username='alice', email='alice@example.org', firstName='Alice',
                         lastName='Anderson', callsign='VIPER-1', onboardedBy=None, expiryDate=None,
                         emailVerified=True, has_password=True, oidc_sub=None, oidc_provider_id=None,
                         language='en')
    ids['user2'] = s.ins('users', username='bob', email='bob@example.org', firstName='Bob',
                         lastName='Bakker', callsign='GHOST-2', onboardedBy=ids['user1'],
                         expiryDate=NOW + timedelta(days=30), emailVerified=False, has_password=False,
                         oidc_sub='sub-bob-123', oidc_provider_id=ids['oidc1'], language='nl')
    ids['user3'] = s.ins('users', username='carla', email='carla@example.org', firstName='Carla',
                         lastName='Clausen', callsign='EAGLE-3', onboardedBy=ids['user1'],
                         expiryDate=None, emailVerified=True, has_password=True, oidc_sub=None,
                         oidc_provider_id=None, language='de')


def seed_takprofiles(s):
    s.ins('takprofiles', 'prof1', name='Default ATAK', description='Default profile', isPublic=True,
          takTemplateFolderLocation='datapackages/default', takPrefFileLocation='datapackages/default/pref.pref',
          injectCallsign=True)
    s.ins('takprofiles', 'prof2', name='Private', description='Private profile', isPublic=False,
          takTemplateFolderLocation=None, takPrefFileLocation=None, injectCallsign=False)


def seed_taksettings(s):
    s.ins('taksettings', name='Team color', description='Default team color', key='locationTeam',
          value='Cyan', takProfileID=s.ids['prof1'])


def seed_onboardingcodes(s):
    ids = s.ids
    s.ins('onboardingcodes', 'code1', description='Public code', name='Open day', onboardingCode='OPEN-2026',
          uses=3, maxUses=50, userExpiryDate=NOW + timedelta(days=7), expiryDate=NOW + timedelta(days=60),
          onboardContactId=ids['user1'], autoApprove=True, requireApproval=False, approverRoleId=None)
    s.ins('onboardingcodes', 'code2', description='Needs approval', name='Guests', onboardingCode='GUEST-2026',
          uses=0, maxUses=5, userExpiryDate=None, expiryDate=None, onboardContactId=ids['user2'],
          autoApprove=False, requireApproval=True, approverRoleId=ids['role_approver'])


def seed_meshtastic_channel_groups(s):
    s.ins('meshtastic_channel_groups', 'mgroup1', name='LongFast set', description='Default channels',
          combined_url='https://meshtastic.org/e/#abc', isPublic=True, showOnHomepage=True,
          created_at=NOW, updated_at=NOW, yamlConfig='lora:\n  region: EU_868\n')


def seed_meshtastic(s):
    s.ins('meshtastic', 'mesh1', description='Primary', name='Primary', url='https://meshtastic.org/e/#primary',
          isPublic=True, yamlConfig='lora: {}', defaultRadioConfig=True, showOnHomepage=True,
          ots_id=7, synced_at=NOW, group_id=s.ids['mgroup1'], slot_number=0)


def seed_ots_groups(s):
    s.ins('ots_groups', 'ogroup1', name='Alpha', display_name='Team Alpha', description='Alpha team',
          direction='BOTH', active=True, synced_at=NOW, created_at=NOW)
    s.ins('ots_groups', 'ogroup2', name='Bravo', display_name='Team Bravo', description=None,
          direction='IN', active=False, synced_at=None, created_at=NOW)


def seed_announcements(s):
    s.ins('announcements', 'ann1', title='Welcome', content='Welcome to the portal', target_type='all',
          status='sent', scheduled_at=None, sent_at=NOW, send_email=True, created_by=s.ids['user1'],
          created_at=NOW, updated_at=NOW)
    # values outside the CHECK constraints added by b6c7d8e9f0a2 (no constraint upstream)
    s.ins('announcements', 'ann2', title='Bad target', content='x', target_type='everyone',
          status='pending', scheduled_at=None, sent_at=None, send_email=False, created_by=s.ids['user1'],
          created_at=NOW, updated_at=NOW)
    s.ins('announcements', 'ann3', title='Bad status', content='x', target_type='roles',
          status='pending', scheduled_at=None, sent_at=None, send_email=False, created_by=s.ids['user1'],
          created_at=NOW, updated_at=NOW)


def seed_announcement_reads(s):
    s.ins('announcement_reads', announcement_id=s.ids['ann1'], user_id=s.ids['user2'], read_at=NOW,
          email_opened=True, email_opened_at=NOW, dismissed=False)


def seed_api_keys(s):
    s.ins('api_keys', name='ci', description='CI key', key_prefix='otp_abcd', key_hash='h' * 64,
          permissions='["users:read"]', rate_limit=1000, is_active=True, expires_at=None,
          created_by=s.ids['user1'], created_at=NOW, last_used_at=NOW, last_used_ip='10.0.0.1', usage_count=4)


def seed_system_settings(s):
    s.ins('system_settings', key='site_name', value='My TAK Portal', category='general',
          description='Site name', created_at=NOW, updated_at=NOW)
    s.ins('system_settings', key='zerotier_icon', value='true', category='general',
          description='Show ZeroTier download icon on the dashboard', created_at=NOW, updated_at=NOW)


def seed_packages(s):
    s.ins('packages', name='ATAK-CIV', platform='android', typePackage='apk', description='ATAK',
          fileLocation='datapackages/atak.apk', imageLocation=None, version='5.2.0', revisionCode=3,
          apkHash=12345, osRequirement='9', takPreReq=None, apkSize=1000, fullPackageName='com.atakmap.app.civ')


def seed_radios(s):
    s.ins('radios', name='Heltec 1', platform='meshtastic', radioType='heltec_v3', description='Field radio',
          softwareVersion='2.5.0', model='V3', vendor='Heltec', shortName='H1', longName='Heltec One',
          assignedTo=s.ids['user2'], owner=s.ids['user1'], mac='AA:BB:CC:DD:EE:01', role='CLIENT',
          publicKey='pub==', privateKey='priv==', createdAt=NOW, updatedAt=NOW)
    s.ins('radios', name='LoRa 2', platform='other', radioType='lora', description=None,
          softwareVersion=None, model=None, vendor=None, shortName='L2', longName='LoRa Two',
          assignedTo=None, owner=None, mac='AA:BB:CC:DD:EE:02', role=None,
          publicKey=None, privateKey=None, createdAt=NOW, updatedAt=NOW)
    s.ins('radios', name='T-Beam 3', platform='meshtastic', radioType='meshtastic', description=None,
          softwareVersion=None, model=None, vendor=None, shortName='T3', longName='T-Beam Three',
          assignedTo=None, owner=None, mac='AA:BB:CC:DD:EE:03', role=None,
          publicKey=None, privateKey=None, createdAt=NOW, updatedAt=NOW)


def seed_one_time_tokens(s):
    s.ins('one_time_tokens', token='tok-' + 'a' * 20, user_id=s.ids['user2'], token_type='password_reset',
          is_used=False, expires_at=NOW + timedelta(hours=1), created_at=NOW, used_at=None)


def seed_pending_registrations(s):
    s.ins('pending_registrations', username='dave', email='dave@example.org', password='hashed-pw',
          firstName='Dave', lastName='Dekker', callsign='WOLF-4', onboarding_code_id=s.ids['code2'],
          verification_token='verify-dave', expires_at=NOW + timedelta(days=1), created_at=NOW,
          approval_status='pending_approval', approval_token='approve-dave', approved_by=None, approved_at=None)


def seed_kiosk_sessions(s):
    s.ins('kiosk_sessions', session_id='kiosk-1', user_id=s.ids['user3'], status='authenticated',
          created_at=NOW, expires_at=NOW + timedelta(minutes=10), authenticated_at=NOW,
          access_token='at', refresh_token='rt')


def seed_channel_group_membership(s):
    s.ins('channel_group_membership', channel_id=s.ids['mesh1'], group_id=s.ids['mgroup1'], slot_number=0)


def seed_associations(s):
    i = s.ids
    s.ins('role_takprofile_association', role_id=i['role_member'], takprofile_id=i['prof1'])
    s.ins('user_takprofile_association', user_id=i['user1'], takprofile_id=i['prof2'])
    s.ins('user_role_association', user_id=i['user1'], role_id=i['role_admin'])
    s.ins('user_role_association', user_id=i['user2'], role_id=i['role_member'])
    s.ins('user_role_association', user_id=i['user3'], role_id=None)  # NULL FK (kept: not an OTS group table)
    s.ins('role_onboardingcode_association', role_id=i['role_member'], onboardingcode_id=i['code1'])
    s.ins('user_onboardingcode_association', user_id=i['user2'], onboardingcode_id=i['code1'])
    s.ins('role_meshtastic_association', role_id=i['role_member'], meshtastic_id=i['mesh1'])
    s.ins('user_meshtastic_association', user_id=i['user1'], meshtastic_id=i['mesh1'])
    s.ins('announcement_role_association', announcement_id=i['ann1'], role_id=i['role_member'])
    s.ins('announcement_user_association', announcement_id=i['ann1'], user_id=i['user1'])
    s.ins('approver_onboardingcode_association', user_id=i['user1'], onboardingcode_id=i['code2'])
    s.ins('role_meshtastic_group_association', role_id=i['role_member'], group_id=i['mgroup1'])
    s.ins('user_meshtastic_group_association', user_id=i['user1'], group_id=i['mgroup1'])


def seed_ots_group_associations(s):
    i = s.ids
    # IN and OUT for the same (group, user) pair
    s.ins('group_user_association', group_id=i['ogroup1'], user_id=i['user1'], direction='IN')
    s.ins('group_user_association', group_id=i['ogroup1'], user_id=i['user1'], direction='OUT')
    s.ins('group_user_association', group_id=i['ogroup2'], user_id=i['user2'], direction='BOTH')
    s.ins('group_user_association', group_id=i['ogroup2'], user_id=None, direction='BOTH')  # NULL FK
    # OUT twice -> OUT; BOTH + IN -> BOTH
    s.ins('group_user_association', group_id=i['ogroup1'], user_id=i['user3'], direction='OUT')
    s.ins('group_user_association', group_id=i['ogroup1'], user_id=i['user3'], direction='OUT')
    s.ins('group_user_association', group_id=i['ogroup2'], user_id=i['user3'], direction='BOTH')
    s.ins('group_user_association', group_id=i['ogroup2'], user_id=i['user3'], direction='IN')
    # two rows for the same (group, code) pair: IN + OUT -> BOTH
    s.ins('group_onboardingcode_association', group_id=i['ogroup1'], onboardingcode_id=i['code1'], direction='BOTH')
    s.ins('group_onboardingcode_association', group_id=i['ogroup1'], onboardingcode_id=i['code2'], direction='IN')
    s.ins('group_onboardingcode_association', group_id=i['ogroup1'], onboardingcode_id=i['code2'], direction='OUT')
    # IN twice -> IN
    s.ins('group_onboardingcode_association', group_id=i['ogroup2'], onboardingcode_id=i['code1'], direction='IN')
    s.ins('group_onboardingcode_association', group_id=i['ogroup2'], onboardingcode_id=i['code1'], direction='IN')
    s.ins('group_onboardingcode_association', group_id=None, onboardingcode_id=i['code1'], direction='BOTH')  # NULL FK


# Ordered so that FK targets come first. Keys must equal the tables at UPSTREAM_REV
# (checked by the guard in the fixture): a new upstream table fails loudly.
SEEDS = {
    'user_roles': seed_user_roles,
    'oidc_providers': seed_oidc_providers,
    'users': seed_users,
    'takprofiles': seed_takprofiles,
    'taksettings': seed_taksettings,
    'onboardingcodes': seed_onboardingcodes,
    'meshtastic_channel_groups': seed_meshtastic_channel_groups,
    'meshtastic': seed_meshtastic,
    'ots_groups': seed_ots_groups,
    'announcements': seed_announcements,
    'announcement_reads': seed_announcement_reads,
    'api_keys': seed_api_keys,
    'system_settings': seed_system_settings,
    'packages': seed_packages,
    'radios': seed_radios,
    'one_time_tokens': seed_one_time_tokens,
    'pending_registrations': seed_pending_registrations,
    'kiosk_sessions': seed_kiosk_sessions,
    'channel_group_membership': seed_channel_group_membership,
}
# Association tables are all seeded by two grouped seeders (they need many parents).
ASSOCIATION_SEEDERS = [seed_associations, seed_ots_group_associations]
ASSOCIATION_TABLES = {
    'role_takprofile_association', 'user_takprofile_association', 'user_role_association',
    'role_onboardingcode_association', 'user_onboardingcode_association',
    'role_meshtastic_association', 'user_meshtastic_association',
    'announcement_role_association', 'announcement_user_association',
    'approver_onboardingcode_association', 'role_meshtastic_group_association',
    'user_meshtastic_group_association', 'group_user_association', 'group_onboardingcode_association',
}


def snapshot(engine, skip=()):
    """{table: Counter of row-tuples keyed by sorted column name} plus column lists."""
    meta = sa.MetaData()
    meta.reflect(engine)
    snap = {}
    with engine.connect() as conn:
        for name, table in meta.tables.items():
            if name == 'alembic_version' or name in skip:
                continue
            cols = sorted(c.name for c in table.columns)
            rows = conn.execute(sa.select(*[table.c[c] for c in cols])).all()
            snap[name] = (cols, [dict(zip(cols, r)) for r in rows])
    return snap


OTS_GROUP_ASSOCIATIONS = {'group_user_association': 'user_id',
                          'group_onboardingcode_association': 'onboardingcode_id'}


def merge_direction(directions):
    """a5b6c7d8e9f1's merge rule for the rows of one (group, x) pair."""
    kinds = set(directions)
    return kinds.pop() if kinds in ({'IN'}, {'OUT'}) else 'BOTH'


def expected_after(table, rows):
    """The pre-upgrade rows of `table` as the migrations should leave them."""
    if table in OTS_GROUP_ASSOCIATIONS:
        col = OTS_GROUP_ASSOCIATIONS[table]
        pairs = {}
        for r in rows:
            if r['group_id'] is not None and r[col] is not None:  # NULL FK rows deleted
                pairs.setdefault((r['group_id'], r[col]), []).append(r['direction'])
        return [{'group_id': g, col: x, 'direction': merge_direction(d)} for (g, x), d in pairs.items()]
    out = []
    for r in rows:
        r = dict(r)
        if table == 'announcements':
            if r['target_type'] not in ('all', 'roles', 'users'):
                r['target_type'], r['status'] = 'users', 'draft'
            if r['status'] not in ('draft', 'scheduled', 'sent'):
                r['status'] = 'draft'
        elif table == 'radios' and r['radioType'] not in ('meshtastic', 'other'):
            r['radioType'] = 'other'
        out.append(r)
    return out


def counts(snap):
    return {t: len(rows) for t, (_, rows) in snap.items()}


def seed_database(url):
    engine = sa.create_engine(url)
    meta = sa.MetaData()
    meta.reflect(engine)
    tables = set(meta.tables) - {'alembic_version'}
    expected = set(SEEDS) | ASSOCIATION_TABLES
    assert tables == expected, (
        f'schema at {UPSTREAM_REV} changed: unseeded={sorted(tables - expected)} '
        f'unknown={sorted(expected - tables)}')
    with engine.begin() as conn:
        s = Seeder(conn, meta)
        for fn in SEEDS.values():
            fn(s)
        for fn in ASSOCIATION_SEEDERS:
            fn(s)
    engine.dispose()
    return s.ids


class Upgrade:
    def __init__(self, url, ids, before, after):
        self.url, self.ids, self.before, self.after = url, ids, before, after


def build_upgrade(make_db, backend):
    url = make_db(backend)
    assert len(current_heads(url)) == 1, 'multiple alembic heads'
    flask_db(url, 'upgrade', UPSTREAM_REV)
    ids = seed_database(url)
    engine = sa.create_engine(url)
    before = snapshot(engine)
    engine.dispose()
    flask_db(url, 'upgrade', 'head')
    engine = sa.create_engine(url)
    after = snapshot(engine)
    engine.dispose()
    return Upgrade(url, ids, before, after)


@pytest.fixture(scope='module', params=_backends())
def backend(request):
    return request.param


@pytest.fixture(scope='module')
def up(backend, make_db):
    """One upgraded database per backend, shared by the read-only tests."""
    return build_upgrade(make_db, backend)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def rows_by(snap, table, col):
    return {r[col]: r for r in snap[table][1]}


def test_single_head_and_alembic_version(up):
    heads = current_heads(up.url)
    assert heads == [HEAD_REV], heads
    engine = sa.create_engine(up.url)
    with engine.connect() as c:
        assert [r[0] for r in c.execute(sa.text('SELECT version_num FROM alembic_version'))] == heads
    engine.dispose()


def test_db_check_clean(up):
    proc = flask_db(up.url, 'check', check=False)
    assert proc.returncode == 0, f'flask db check:\n{proc.stdout}\n{proc.stderr}'


def test_seed_covers_every_table(up):
    """Every table at the upstream revision had at least one row before the upgrade."""
    empty = [t for t, n in counts(up.before).items() if n == 0]
    assert not empty, f'unseeded (empty) tables: {empty}'
    assert len(up.before) == 33, len(up.before)


def test_row_counts_preserved_except_documented_losses(up):
    before, after = counts(up.before), counts(up.after)
    lossy = {'approver_onboardingcode_association', 'group_user_association',
             'group_onboardingcode_association', 'system_settings'}
    for table, n in before.items():
        if table in DROPPED_TABLES:
            assert table not in after, f'{table} should have been dropped'
        elif table not in lossy:
            assert after[table] == n, f'{table}: {n} rows before, {after[table]} after'
    # documented losses, exact: one NULL-FK row each, plus the merged duplicates
    assert before['group_user_association'] == 8 and after['group_user_association'] == 4
    assert before['group_onboardingcode_association'] == 6 and after['group_onboardingcode_association'] == 3
    assert before['system_settings'] == 2 and after['system_settings'] == 1  # zerotier_icon
    assert {r['key'] for r in up.after['system_settings'][1]} == {'site_name'}


def test_surviving_rows_are_value_identical(up):
    """Every pre-existing column on every surviving row is unchanged (except documented
    drops, merges and normalised values: see expected_after)."""
    def key(row):
        return tuple(sorted((k, repr(v)) for k, v in row.items()))
    for table, (cols, rows) in up.before.items():
        if table in DROPPED_TABLES:
            continue
        keep = [c for c in cols if c not in DROPPED_COLUMNS.get(table, ())]
        pre = Counter(key({c: r[c] for c in keep}) for r in expected_after(table, rows))
        post_cols, post_rows = up.after[table]
        post = Counter(key({c: r[c] for c in keep}) for r in post_rows)
        if table == 'system_settings':
            pre = Counter({k: v for k, v in pre.items() if "'zerotier_icon'" not in dict(k)['key']})
        assert post == pre, f'{table}: surviving rows differ\nlost: {pre - post}\ngained: {post - pre}'


def test_key_values(up):
    a, i = up.after, up.ids
    users = rows_by(a, 'users', 'username')
    assert set(users) == {'alice', 'bob', 'carla'}
    assert users['alice']['email'] == 'alice@example.org' and users['alice']['callsign'] == 'VIPER-1'
    assert users['bob']['onboardedBy'] == i['user1'] and users['bob']['language'] == 'nl'
    assert users['bob']['oidc_provider_id'] == i['oidc1']
    codes = rows_by(a, 'onboardingcodes', 'onboardingCode')
    assert codes['OPEN-2026']['uses'] == 3 and codes['OPEN-2026']['maxUses'] == 50
    assert codes['GUEST-2026']['requireApproval'] in (True, 1)
    assert {r['name'] for r in a['user_roles'][1]} == {'administrator', 'member', 'approver'}
    assert {r['name'] for r in a['ots_groups'][1]} == {'Alpha', 'Bravo'}
    gu = Counter((r['group_id'], r['user_id'], r['direction']) for r in a['group_user_association'][1])
    assert gu == Counter({(i['ogroup1'], i['user1'], 'BOTH'): 1, (i['ogroup2'], i['user2'], 'BOTH'): 1,
                          (i['ogroup1'], i['user3'], 'OUT'): 1, (i['ogroup2'], i['user3'], 'BOTH'): 1})
    go = Counter((r['group_id'], r['onboardingcode_id'], r['direction'])
                 for r in a['group_onboardingcode_association'][1])
    assert go == Counter({(i['ogroup1'], i['code1'], 'BOTH'): 1, (i['ogroup1'], i['code2'], 'BOTH'): 1,
                          (i['ogroup2'], i['code1'], 'IN'): 1})
    anns = rows_by(a, 'announcements', 'title')
    assert anns['Welcome']['status'] == 'sent' and anns['Welcome']['target_type'] == 'all'
    # invalid values normalised by b6c7d8e9f0a2
    assert (anns['Bad target']['target_type'], anns['Bad target']['status']) == ('users', 'draft')
    assert (anns['Bad status']['target_type'], anns['Bad status']['status']) == ('roles', 'draft')
    assert rows_by(a, 'system_settings', 'key')['site_name']['value'] == 'My TAK Portal'
    assert a['api_keys'][1][0]['key_hash'] == 'h' * 64
    assert a['oidc_providers'][1][0]['client_id'] == 'portal'
    radios = rows_by(a, 'radios', 'mac')
    assert radios['AA:BB:CC:DD:EE:01']['name'] == 'Heltec 1'
    assert radios['AA:BB:CC:DD:EE:01']['radioType'] == 'other'  # was 'heltec_v3'
    assert radios['AA:BB:CC:DD:EE:02']['radioType'] == 'other'  # was 'lora'
    assert radios['AA:BB:CC:DD:EE:03']['radioType'] == 'meshtastic'
    # a NULL FK on a non-OTS-group association table is not touched
    assert any(r['role_id'] is None for r in a['user_role_association'][1])


def test_duplicate_association_rows(up):
    """Documents current behaviour; flip EXPECT_DUPLICATES_MERGED when a PK migration lands."""
    i = up.ids
    gu = [r for r in up.after['group_user_association'][1]
          if (r['group_id'], r['user_id']) == (i['ogroup1'], i['user1'])]
    go = [r for r in up.after['group_onboardingcode_association'][1]
          if (r['group_id'], r['onboardingcode_id']) == (i['ogroup1'], i['code2'])]
    if EXPECT_DUPLICATES_MERGED:
        assert [r['direction'] for r in gu] == ['BOTH']  # IN + OUT
        assert [r['direction'] for r in go] == ['BOTH']  # IN + OUT
    else:
        assert sorted(r['direction'] for r in gu) == ['IN', 'OUT']
        assert sorted(r['direction'] for r in go) == ['IN', 'OUT']


def test_new_and_dropped_schema(up):
    a = up.after
    for table, cols in ADDED_COLUMNS.items():
        for row in a[table][1]:
            for col, expected in cols.items():
                assert row[col] == expected, f'{table}.{col}={row[col]!r}, expected {expected!r}'
    assert 'direction' not in a['ots_groups'][0]
    assert 'approver_onboardingcode_association' not in a
    for table, cols in NEW_EMPTY_TABLES.items():
        assert table in a, f'{table} missing'
        assert set(a[table][0]) == cols, f'{table} columns: {sorted(a[table][0])}'
        assert a[table][1] == [], f'{table} should be empty'
    engine = sa.create_engine(up.url)
    insp = sa.inspect(engine)
    for table, col in [('group_user_association', 'group_id'), ('group_user_association', 'user_id'),
                       ('group_onboardingcode_association', 'group_id'),
                       ('group_onboardingcode_association', 'onboardingcode_id'), ('users', 'id')]:
        c = next(c for c in insp.get_columns(table) if c['name'] == col)
        assert not c['nullable'], f'{table}.{col} should be NOT NULL'
    fks = {tuple(f['constrained_columns']): f['referred_table'] for f in insp.get_foreign_keys('event_sessions')}
    assert fks.get(('eventLinkId',)) == 'event_links'
    fks = {tuple(f['constrained_columns']): f['referred_table'] for f in insp.get_foreign_keys('event_teams')}
    assert fks.get(('leaderUserId',)) == 'users'
    # eventLinkId/userId deliberately without FK (audit survives link deletion)
    fks = {tuple(f['constrained_columns']): f['referred_table']
           for f in insp.get_foreign_keys('event_leader_sessions')}
    assert fks == {('eventId',): 'events', ('eventTeamId',): 'event_teams'}
    fks = {tuple(f['constrained_columns']): f['referred_table']
           for f in insp.get_foreign_keys('event_team_leaders')}
    assert fks == {('eventId',): 'events', ('eventTeamId',): 'event_teams', ('userId',): 'users'}
    uniques = {tuple(u['column_names']) for u in insp.get_unique_constraints('event_team_leaders')}
    assert ('eventId', 'eventTeamId', 'userId') in uniques
    fk = insp.get_foreign_keys('role_permissions')
    assert len(fk) == 1
    assert fk[0]['name'] == 'fk_role_permissions_role_id_user_roles'
    assert fk[0]['constrained_columns'] == ['role_id'] and fk[0]['referred_table'] == 'user_roles'
    assert fk[0]['options'].get('ondelete') == 'CASCADE'
    uniques = {u['name']: u['column_names'] for u in insp.get_unique_constraints('role_permissions')}
    assert uniques == {'uq_role_permissions_role_id_permission': ['role_id', 'permission']}
    fks = {tuple(f['constrained_columns']): f for f in insp.get_foreign_keys('events')}
    assert fks[('organizerTeamId',)]['referred_table'] == 'event_teams'
    assert fks[('organizerTeamId',)]['name'] == 'fk_events_organizerTeamId_event_teams'
    for table, col, default in [('event_links', 'direction', 'BOTH'), ('events', 'organizerDirection', 'OUT')]:
        c = next(c for c in insp.get_columns(table) if c['name'] == col)
        assert not c['nullable'] and default in (c['default'] or ''), f'{table}.{col}: {c}'
    engine.dispose()


CHECK_CONSTRAINTS = {
    'announcements': {'check_announcement_target_type', 'check_announcement_status'},
    'radios': {'check_radio_type'},
}


CREATOR_TABLES = ('announcements', 'api_keys')


def assert_creator_columns(insp, nullable):
    """created_by has the wanted nullability and kept its FK; the rebuild left api_keys' unique intact."""
    for table in CREATOR_TABLES:
        c = next(c for c in insp.get_columns(table) if c['name'] == 'created_by')
        assert c['nullable'] is nullable, f'{table}.created_by: {c}'
        fks = {tuple(f['constrained_columns']): f for f in insp.get_foreign_keys(table)}
        assert fks[('created_by',)]['referred_table'] == 'users', table
        assert not fks[('created_by',)]['options'].get('ondelete'), table
    assert ['key_hash'] in [u['column_names'] for u in insp.get_unique_constraints('api_keys')]


def assert_constraints(url, ids):
    """Primary keys and CHECK constraints exist and reject bad rows."""
    engine = sa.create_engine(url)
    insp = sa.inspect(engine)
    for table, col in OTS_GROUP_ASSOCIATIONS.items():
        assert insp.get_pk_constraint(table)['constrained_columns'] == ['group_id', col], table
    for table, names in CHECK_CONSTRAINTS.items():
        assert names <= {c['name'] for c in insp.get_check_constraints(table)}, table
    assert_creator_columns(insp, nullable=True)
    meta = sa.MetaData()
    meta.reflect(engine, only=['group_user_association', 'group_onboardingcode_association',
                               'announcements', 'radios'])
    t = meta.tables
    ann = dict(title='t', content='c', target_type='all', status='draft', send_email=False,
               created_by=ids['user1'], created_at=NOW, updated_at=NOW)
    radio = dict(name='r', platform='p', radioType='other')
    bad = [
        t['group_user_association'].insert().values(group_id=ids['ogroup1'], user_id=ids['user1'], direction='IN'),
        t['group_onboardingcode_association'].insert().values(
            group_id=ids['ogroup1'], onboardingcode_id=ids['code1'], direction='IN'),
        t['announcements'].insert().values(**{**ann, 'target_type': 'everyone'}),
        t['announcements'].insert().values(**{**ann, 'status': 'pending'}),
        t['radios'].insert().values(**{**radio, 'radioType': 'lora'}),
    ]
    for stmt in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as c:
                c.execute(stmt)
    with engine.begin() as c:  # valid values still go in (rolled back below)
        c.execute(t['announcements'].insert().values(**ann))
        c.execute(t['radios'].insert().values(**radio))
        c.execute(sa.text("DELETE FROM announcements WHERE title = 't'"))
        c.execute(sa.text("DELETE FROM radios WHERE name = 'r'"))
    engine.dispose()


def test_primary_keys_and_check_constraints(backend, make_db):
    up = build_upgrade(make_db, backend)  # own database: inserts are attempted
    assert_constraints(up.url, up.ids)


def test_downgrade_refused_while_creator_is_null(backend, make_db):
    up = build_upgrade(make_db, backend)  # own database: this test mutates it
    flask_db(up.url, 'downgrade', CREATOR_NULLABLE_REV)
    engine = sa.create_engine(up.url)
    ann_id = up.ids['ann1']
    with engine.begin() as c:
        c.execute(sa.text('UPDATE announcements SET created_by = NULL WHERE id = :i'), {'i': ann_id})
        c.execute(sa.text('UPDATE api_keys SET created_by = NULL'))
    before = snapshot(engine)
    engine.dispose()

    proc = flask_db(up.url, 'downgrade', check=False)

    assert proc.returncode != 0
    assert 'rows with created_by NULL exist (announcements: 1, api_keys: 1)' in proc.stderr
    engine = sa.create_engine(up.url)
    assert snapshot(engine) == before  # no rows touched
    with engine.connect() as c:
        assert c.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == CREATOR_NULLABLE_REV
    assert_creator_columns(sa.inspect(engine), nullable=True)

    # once the rows have a creator again the same downgrade goes through
    with engine.begin() as c:
        c.execute(sa.text('UPDATE announcements SET created_by = :u WHERE created_by IS NULL'), {'u': up.ids['user1']})
        c.execute(sa.text('UPDATE api_keys SET created_by = :u WHERE created_by IS NULL'), {'u': up.ids['user1']})
    engine.dispose()
    flask_db(up.url, 'downgrade')
    engine = sa.create_engine(up.url)
    assert_creator_columns(sa.inspect(engine), nullable=False)
    assert counts(snapshot(engine)) == counts(before)
    engine.dispose()
    flask_db(up.url, 'upgrade', 'head')
    assert_constraints(up.url, up.ids)
    proc = flask_db(up.url, 'check', check=False)
    assert proc.returncode == 0, f'flask db check:\n{proc.stdout}\n{proc.stderr}'


CREATE_ALL = textwrap.dedent('''
    from app import app
    from app.models import db
    with app.app_context():
        db.create_all()
''')


def test_create_all_stamped_database(backend, make_db):
    """docker-entrypoint.sh may stamp a create_all database: the new steps must be no-ops."""
    url = make_db(backend)
    _run(url, [sys.executable, '-c', CREATE_ALL])
    flask_db(url, 'stamp', 'f4a5b6c7d8e0')
    flask_db(url, 'upgrade', 'head')
    engine = sa.create_engine(url)
    with engine.begin() as c:
        meta = sa.MetaData()
        meta.reflect(c)
        s = Seeder(c, meta)
        s.ins('users', 'user1', username='alice', email='alice@example.org')
        s.ins('ots_groups', 'ogroup1', name='Alpha', active=True)
        s.ins('onboardingcodes', 'code1', name='c', onboardingCode='C-1', uses=0, maxUses=1,
              autoApprove=False, requireApproval=False)
        s.ins('group_user_association', group_id=s.ids['ogroup1'], user_id=s.ids['user1'], direction='BOTH')
        s.ins('group_onboardingcode_association', group_id=s.ids['ogroup1'],
              onboardingcode_id=s.ids['code1'], direction='BOTH')
        ids = s.ids
    engine.dispose()
    assert_constraints(url, ids)
    assert flask_db(url, 'check', check=False).returncode == 0
    flask_db(url, 'downgrade', 'f4a5b6c7d8e0')
    flask_db(url, 'upgrade', 'head')
    assert_constraints(url, ids)
    proc = flask_db(url, 'check', check=False)
    assert proc.returncode == 0, f'flask db check:\n{proc.stdout}\n{proc.stderr}'


ORM_SMOKE = textwrap.dedent('''
    import json, sys
    from app import app
    from app.models import db, UserModel, GroupUserAssociation, OTSGroupModel
    out = {}
    with app.app_context():
        for mapper in db.Model.registry.mappers:
            cls = mapper.class_
            cls.query.first()
            out.setdefault('models', []).append(cls.__name__)
        for table in db.metadata.sorted_tables:
            db.session.execute(table.select().limit(1)).first()
        group = OTSGroupModel.query.first()
        u1 = UserModel(username='smoke1', email='smoke1@example.org')
        u2 = UserModel(username='smoke2', email='smoke2@example.org')
        db.session.add_all([u1, u2])
        db.session.flush()
        assert u1.id and u2.id and u1.id != u2.id
        db.session.add(GroupUserAssociation(group_id=group.id, user_id=u1.id, direction='IN'))
        db.session.commit()
        assert UserModel.query.filter_by(username='smoke2').count() == 1
        assert GroupUserAssociation.query.filter_by(user_id=u1.id).count() == 1
    print(json.dumps(out))
''')


def test_orm_smoke(up):
    proc = _run(up.url, [sys.executable, '-c', ORM_SMOKE])
    models = json.loads(proc.stdout.strip().splitlines()[-1])['models']
    assert len(models) >= 20, models


def test_round_trip(backend, make_db):
    up = build_upgrade(make_db, backend)  # own database: this test mutates it
    before_counts = counts(up.after)
    flask_db(up.url, 'downgrade', UPSTREAM_REV)
    engine = sa.create_engine(up.url)
    down = snapshot(engine)
    engine.dispose()
    assert 'event_teams' not in down and 'approver_onboardingcode_association' in down
    # tables that do not lose data keep their rows through the downgrade
    lossy = {'group_user_association', 'group_onboardingcode_association', 'system_settings',
             'approver_onboardingcode_association'}
    for table, n in before_counts.items():
        if table in NEW_EMPTY_TABLES or table in lossy:
            continue
        assert counts(down)[table] == n, f'{table} after downgrade'
    # zerotier_icon is restored by e3f4a5b6c7d9's downgrade
    assert 'zerotier_icon' in {r['key'] for r in down['system_settings'][1]}
    flask_db(up.url, 'upgrade', 'head')
    engine = sa.create_engine(up.url)
    again = snapshot(engine)
    engine.dispose()
    for table, n in before_counts.items():
        if table in ('system_settings',):
            continue
        assert counts(again)[table] == n, f'{table} after re-upgrade'
    proc = flask_db(up.url, 'check', check=False)
    assert proc.returncode == 0, f'flask db check after round trip:\n{proc.stdout}\n{proc.stderr}'
    assert_constraints(up.url, up.ids)
