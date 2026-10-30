"""
Switchable modules (meshtastic, radios, events): a disabled module's endpoints answer
403 MODULE_DISABLED for every caller, while settings/auth endpoints stay reachable so
an admin can turn it back on. Missing rows mean the default (meshtastic/radios on,
events off).
"""
import pytest
from flask_jwt_extended import create_access_token

from app.models import SystemSettingsModel, UserModel, UserRoleModel, db as _db

PREFIX = '/api/v1'
MODULE_KEYS = ('meshtastic_enabled', 'radios_enabled', 'events_enabled')


@pytest.fixture
def setting(db):
    """Set settings for one test; rows are restored (or removed) afterwards."""
    originals = {}

    def _set(key, value):
        if key not in originals:
            row = SystemSettingsModel.query.filter_by(key=key).first()
            originals[key] = row.value if row else None
        SystemSettingsModel.set_setting(key, value, category='modules')

    yield _set
    _db.session.rollback()
    for key, value in originals.items():
        row = SystemSettingsModel.query.filter_by(key=key).first()
        if value is None:
            _db.session.delete(row)
        else:
            row.value = value
    _db.session.commit()


@pytest.fixture
def no_module_rows(db):
    """Remove the module rows for one test, restoring them afterwards."""
    saved = []
    for row in SystemSettingsModel.query.filter(SystemSettingsModel.key.in_(MODULE_KEYS)).all():
        saved.append((row.key, row.value, row.category))
        _db.session.delete(row)
    _db.session.commit()
    yield
    _db.session.rollback()
    SystemSettingsModel.query.filter(SystemSettingsModel.key.in_(MODULE_KEYS)).delete()
    for key, value, category in saved:
        _db.session.add(SystemSettingsModel(key=key, value=value, category=category))
    _db.session.commit()


@pytest.fixture
def admin_headers(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    admin = UserModel.query.filter_by(username='gateadmin').first() or \
        UserModel.create_user(username='gateadmin', email='gateadmin@example.com')
    if role not in admin.roles:
        admin.roles.append(role)
        _db.session.commit()
    return {'Authorization': 'Bearer ' + create_access_token(identity=str(admin.id))}


def _disabled(r):
    return r.status_code == 403 and (r.get_json() or {}).get('code') == 'MODULE_DISABLED'


GATED = [
    ('meshtastic_enabled', 'GET', '/meshtastic'),
    ('meshtastic_enabled', 'GET', '/meshtastic/groups'),
    ('radios_enabled', 'GET', '/radios'),
    ('events_enabled', 'GET', '/events'),
    ('events_enabled', 'GET', '/events/teams'),
]


def test_defaults_when_rows_missing(app, no_module_rows):
    assert SystemSettingsModel.is_module_enabled('meshtastic') is True
    assert SystemSettingsModel.is_module_enabled('radios') is True
    assert SystemSettingsModel.is_module_enabled('events') is False


def test_initialize_defaults_seeds_modules(db, no_module_rows):
    SystemSettingsModel.initialize_defaults()
    rows = {s.key: s for s in SystemSettingsModel.query.filter(SystemSettingsModel.key.in_(MODULE_KEYS))}
    assert rows['meshtastic_enabled'].value == 'true'
    assert rows['meshtastic_enabled'].category == 'modules'
    assert rows['radios_enabled'].value == 'true'
    assert rows['radios_enabled'].category == 'modules'
    assert rows['events_enabled'].value == 'false'


@pytest.mark.parametrize('key,method,path', GATED)
def test_disabled_module_refused_for_admin_and_anonymous(client, setting, admin_headers, key, method, path):
    setting(key, False)
    assert _disabled(client.open(PREFIX + path, method=method, headers=admin_headers))
    assert _disabled(client.open(PREFIX + path, method=method))


@pytest.mark.parametrize('key,method,path', GATED)
def test_enabled_module_works(client, setting, admin_headers, key, method, path):
    setting(key, True)
    assert client.open(PREFIX + path, method=method, headers=admin_headers).status_code == 200
    # Anonymous callers get the endpoint's own answer, not the gate's
    assert not _disabled(client.open(PREFIX + path, method=method))


def test_prefix_matches_on_segment_boundary(client, setting):
    setting('events_enabled', False)
    # /eventsX is not under /events (falls through to the SPA catch-all)
    assert not _disabled(client.get(PREFIX + '/eventsfoo'))


def test_public_join_refused_when_events_disabled(client, setting):
    setting('events_enabled', False)
    r = client.post(PREFIX + '/events/join/sometoken')
    assert _disabled(r)
    assert r.get_json()['error'] == 'The events module is disabled'


def test_radio_programming_needs_meshtastic(client, setting, admin_headers):
    setting('radios_enabled', True)
    setting('meshtastic_enabled', False)
    assert client.get(PREFIX + '/radios', headers=admin_headers).status_code == 200
    assert _disabled(client.post(PREFIX + '/radios/1/program-config', headers=admin_headers, json={}))
    assert _disabled(client.post(PREFIX + '/radios/1/compare-config', headers=admin_headers, json={}))
    assert _disabled(client.post(PREFIX + '/radios/enroll', headers=admin_headers, json={}))


def test_kiosk_token_is_gated(client, setting, admin_headers):
    setting('radios_enabled', False)
    user = UserModel.query.filter_by(username='gateadmin').first()
    kiosk = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(user.id), additional_claims={'kiosk_session': True})}
    assert _disabled(client.get(PREFIX + '/radios', headers=kiosk))


def test_settings_reachable_with_everything_off(client, setting, admin_headers):
    for key in MODULE_KEYS:
        setting(key, False)
    assert client.get(PREFIX + '/settings').status_code == 200
    assert client.get(PREFIX + '/admin/settings', headers=admin_headers).status_code == 200
    assert _disabled(client.get(PREFIX + '/meshtastic', headers=admin_headers))

    r = client.put(PREFIX + '/admin/settings/key/meshtastic_enabled', headers=admin_headers,
                   json={'value': 'true'})
    assert r.status_code == 200
    assert client.get(PREFIX + '/meshtastic', headers=admin_headers).status_code == 200


def test_admin_settings_lists_modules_category(client, setting, admin_headers):
    setting('meshtastic_enabled', True)
    data = client.get(PREFIX + '/admin/settings', headers=admin_headers).get_json()
    assert 'meshtastic_enabled' in [s['key'] for s in data['modules']]


def test_permissions_omit_disabled_modules(client, setting, admin_headers):
    setting('meshtastic_enabled', False)
    setting('radios_enabled', True)
    setting('events_enabled', False)
    modules = client.get(PREFIX + '/auth/permissions', headers=admin_headers).get_json()['modules']
    assert 'meshtastic' not in modules
    assert 'events' not in modules
    assert 'radios' in modules
    assert 'users' in modules

    setting('events_enabled', True)
    modules = client.get(PREFIX + '/auth/permissions', headers=admin_headers).get_json()['modules']
    assert 'events' in modules


@pytest.mark.parametrize('meshtastic,radios', [(True, True), (True, False), (False, True), (False, False)])
def test_public_settings_effective_flags(client, setting, meshtastic, radios):
    setting('meshtastic_enabled', meshtastic)
    setting('radios_enabled', radios)
    setting('events_enabled', True)
    for key in ('claim_radio_enabled', 'user_program_radio_enabled', 'user_validate_radio_enabled',
                'user_radio_enrollment_enabled', 'meshtastic_homepage_icon_enabled'):
        setting(key, True)

    s = client.get(PREFIX + '/settings').get_json()
    assert s['meshtastic_enabled'] is meshtastic
    assert s['radios_enabled'] is radios
    assert s['events_enabled'] is True
    assert s['claim_radio_enabled'] is radios
    assert s['user_program_radio_enabled'] is (radios and meshtastic)
    assert s['user_validate_radio_enabled'] is (radios and meshtastic)
    assert s['user_radio_enrollment_enabled'] is (radios and meshtastic)
    assert s['meshtastic_homepage_icon_enabled'] is meshtastic


def test_public_settings_stored_flag_off_stays_off(client, setting):
    setting('meshtastic_enabled', True)
    setting('radios_enabled', True)
    setting('claim_radio_enabled', False)
    setting('meshtastic_homepage_icon_enabled', False)
    s = client.get(PREFIX + '/settings').get_json()
    assert s['claim_radio_enabled'] is False
    assert s['meshtastic_homepage_icon_enabled'] is False


@pytest.mark.parametrize('key', MODULE_KEYS)
def test_invalid_module_value_rejected(client, setting, admin_headers, key):
    setting(key, True)
    r = client.put(PREFIX + f'/admin/settings/key/{key}', headers=admin_headers, json={'value': 'yes'})
    assert r.status_code == 400
    # A JSON boolean is accepted and stored as 'false'
    r = client.put(PREFIX + f'/admin/settings/key/{key}', headers=admin_headers, json={'value': False})
    assert r.status_code == 200
    assert r.get_json()['setting']['value'] == 'false'
