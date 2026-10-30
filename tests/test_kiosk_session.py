"""
Kiosk sessions are short-lived shared-terminal sessions: refreshing a kiosk token must
keep the kiosk_session claim and the kiosk lifetime instead of minting a 12-hour session,
and a kiosk token only works on the kiosk endpoints and never carries the user's roles.
"""
from datetime import timedelta

import pytest
from flask_jwt_extended import (create_access_token, create_refresh_token, decode_token,
                                jwt_required)

from app import create_app
from app.api_v1 import kiosk as kiosk_mod
from app.models import (KioskSessionModel, MeshtasticModel, RadioModel, SystemSettingsModel,
                        UserModel, UserRoleModel)

PREFIX = '/api/v1'


@pytest.fixture
def kiosk(db):
    old_enabled = SystemSettingsModel.get_setting('kiosk_enrollment_enabled', False)
    old_timeout = SystemSettingsModel.get_setting('kiosk_session_timeout_minutes', '10')
    SystemSettingsModel.set_setting('kiosk_enrollment_enabled', True)
    SystemSettingsModel.set_setting('kiosk_session_timeout_minutes', '10')
    yield
    db.session.rollback()
    KioskSessionModel.query.delete()
    db.session.commit()
    SystemSettingsModel.set_setting('kiosk_enrollment_enabled', old_enabled)
    SystemSettingsModel.set_setting('kiosk_session_timeout_minutes', old_timeout)


def test_kiosk_refresh_keeps_kiosk_claim_and_lifetime(client, db, kiosk):
    user = UserModel.query.filter_by(username='kioskuser').first() or \
        UserModel.create_user(username='kioskuser', email='kioskuser@example.com')
    r = client.post(PREFIX + '/kiosk/session')
    assert r.status_code == 200
    session_id = r.get_json()['session_id']

    h = {'Authorization': 'Bearer ' + create_access_token(identity=str(user.id))}
    r = client.post(PREFIX + f'/kiosk/session/{session_id}/authenticate', headers=h)
    assert r.status_code == 200

    status = client.get(PREFIX + f'/kiosk/session/{session_id}/status').get_json()
    assert status['status'] == 'authenticated'

    r = client.post(PREFIX + '/auth/refresh',
                    headers={'Authorization': 'Bearer ' + status['refresh_token']})
    assert r.status_code == 200
    claims = decode_token(r.get_json()['access_token'])
    assert claims.get('kiosk_session') is True
    assert claims['exp'] - claims['iat'] <= 10 * 60


def test_kiosk_refresh_never_outlives_refresh_token(client, db, kiosk):
    user = UserModel.query.filter_by(username='kioskuser').first() or \
        UserModel.create_user(username='kioskuser', email='kioskuser@example.com')
    # A kiosk refresh token with only 60 seconds left, while the kiosk timeout is 10 minutes.
    refresh_token = create_refresh_token(identity=str(user.id),
                                         additional_claims={'kiosk_session': True},
                                         expires_delta=timedelta(seconds=60))

    r = client.post(PREFIX + '/auth/refresh',
                    headers={'Authorization': 'Bearer ' + refresh_token})
    assert r.status_code == 200
    claims = decode_token(r.get_json()['access_token'])
    assert claims.get('kiosk_session') is True
    assert claims['exp'] <= decode_token(refresh_token)['exp']


# --- Kiosk tokens are deny-by-default and never carry the user's roles ---

DENIED_MSG = 'Not available for kiosk sessions'


def _user(db, username, admin=False):
    user = UserModel.query.filter_by(username=username).first() or \
        UserModel.create_user(username=username, email=f'{username}@example.com')
    if admin:
        role = UserRoleModel.query.filter_by(name='administrator').first() or \
            UserRoleModel.create_role(name='administrator', description='admin')
        if role not in user.roles:
            user.roles.append(role)
            db.session.commit()
    return user


def _kiosk_tokens(client, user):
    """Sign `user` in at a kiosk through the real session -> authenticate -> status flow."""
    session_id = client.post(PREFIX + '/kiosk/session').get_json()['session_id']
    h = {'Authorization': 'Bearer ' + create_access_token(identity=str(user.id))}
    r = client.post(PREFIX + f'/kiosk/session/{session_id}/authenticate', headers=h)
    assert r.status_code == 200
    status = client.get(PREFIX + f'/kiosk/session/{session_id}/status').get_json()
    assert status['status'] == 'authenticated'
    return status['access_token'], status['refresh_token']


def _bearer(token):
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def world(db, kiosk):
    admin = _user(db, 'kioskadmin', admin=True)
    plain = _user(db, 'kioskplain')
    other = _user(db, 'kioskother')
    radios = {}
    for name, owner in (('kiosk-admin-radio', admin), ('kiosk-plain-radio', plain),
                        ('kiosk-other-radio', other)):
        radios[name] = RadioModel.create(mac=None, name=name, platform='meshtastic',
                                         radioType='meshtastic', owner=owner.id,
                                         assignedTo=owner.id)
    channel = MeshtasticModel.create_meshtastic(name='kiosk-test-channel',
                                                url='https://meshtastic.org/e/#kiosk')
    old = {k: SystemSettingsModel.get_setting(k, False)
           for k in ('user_radio_enrollment_enabled', 'user_program_radio_enabled')}
    users = (admin, plain, other)
    # Tests append the ids of radios they enroll, so teardown only removes this test's radios.
    radio_ids = [radio.id for radio in radios.values()]
    yield {'admin': admin, 'plain': plain, 'other': other, 'radios': radios, 'channel': channel,
           'radio_ids': radio_ids}
    db.session.rollback()
    for k, v in old.items():
        SystemSettingsModel.set_setting(k, v)
    user_ids = [user.id for user in users]
    RadioModel.query.filter(RadioModel.id.in_(radio_ids)).delete(synchronize_session=False)
    MeshtasticModel.query.filter_by(name='kiosk-test-channel').delete()
    KioskSessionModel.query.filter(KioskSessionModel.user_id.in_(user_ids)) \
        .delete(synchronize_session=False)
    for user in users:
        db.session.delete(user)  # also removes the role links
    db.session.commit()


DENIED = [
    ('GET', '/users'),
    ('GET', '/users/{user}'),
    ('PUT', '/users/{user}'),
    ('GET', '/roles'),
    ('GET', '/admin/settings'),
    ('PUT', '/admin/settings/key/kiosk_enrollment_enabled'),
    ('GET', '/admin/api-keys'),
    ('POST', '/auth/impersonate/{user}'),
    ('GET', '/auth/me'),
    ('GET', '/auth/permissions'),
    ('GET', '/radios/{radio}'),
    ('POST', '/auth/change-password'),
    ('POST', '/kiosk/session/{session}/authenticate'),
]


@pytest.mark.parametrize('method,path', DENIED)
def test_admin_kiosk_token_denied(client, world, method, path):
    access, _ = _kiosk_tokens(client, world['admin'])
    session_id = client.post(PREFIX + '/kiosk/session').get_json()['session_id']
    path = path.format(user=world['plain'].id, radio=world['radios']['kiosk-other-radio'].id,
                       session=session_id)
    r = client.open(PREFIX + path, method=method, headers=_bearer(access),
                    json={'newPassword': 'Kiosk-Pass-1234', 'value': 'false'})
    assert r.status_code == 403
    assert r.get_json() == {'error': DENIED_MSG}
    # The kiosk login was not completed and the setting was not changed.
    assert KioskSessionModel.get_by_session_id(session_id).status == 'pending'
    assert SystemSettingsModel.get_setting('kiosk_enrollment_enabled') in (True, 'true', 'True')


def test_admin_kiosk_refresh_token_only_refreshes(client, world):
    _, refresh = _kiosk_tokens(client, world['admin'])
    r = client.get(PREFIX + '/users', headers=_bearer(refresh))
    assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}

    r = client.post(PREFIX + '/auth/refresh', headers=_bearer(refresh))
    assert r.status_code == 200
    access = r.get_json()['access_token']
    claims = decode_token(access)
    assert claims['roles'] == []
    assert claims['kiosk_session'] is True
    r = client.get(PREFIX + '/users', headers=_bearer(access))
    assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}


@pytest.mark.parametrize('who', ['admin', 'plain'])
def test_kiosk_access_token_has_no_roles(client, world, who):
    access, _ = _kiosk_tokens(client, world[who])
    claims = decode_token(access)
    assert claims['roles'] == []
    assert claims['kiosk_session'] is True


@pytest.mark.parametrize('who', ['admin', 'plain'])
def test_kiosk_features_work(client, world, who):
    SystemSettingsModel.set_setting('user_radio_enrollment_enabled', True)
    SystemSettingsModel.set_setting('user_program_radio_enabled', True)
    user = world[who]
    own = world['radios'][f'kiosk-{who}-radio']
    access, _ = _kiosk_tokens(client, user)
    h = _bearer(access)

    assert client.get(PREFIX + '/settings', headers=h).status_code == 200

    r = client.get(PREFIX + '/radios', headers=h)
    assert r.status_code == 200
    radios = r.get_json()['radios']
    assert radios and all(radio['assignedTo'] == user.id for radio in radios)
    assert own.id in [radio['id'] for radio in radios]

    r = client.post(PREFIX + '/radios/enroll', headers=h,
                    json={'name': f'kiosk-enrolled-{who}', 'platform': 'meshtastic'})
    assert r.status_code == 201
    world['radio_ids'].append(r.get_json()['radio']['id'])

    r = client.post(PREFIX + f'/radios/{own.id}/program-config', headers=h,
                    json={'channel_id': world['channel'].id})
    assert r.status_code == 200

    assert client.get(PREFIX + '/meshtastic/groups', headers=h).status_code == 200

    for path in ('/qr/atak', '/qr/itak'):
        r = client.get(PREFIX + path, headers=h)
        assert r.status_code != 403, path


def test_admin_kiosk_token_cannot_program_other_users_radio(client, world):
    SystemSettingsModel.set_setting('user_program_radio_enabled', True)
    access, _ = _kiosk_tokens(client, world['admin'])
    other = world['radios']['kiosk-other-radio']
    r = client.post(PREFIX + f'/radios/{other.id}/program-config', headers=_bearer(access),
                    json={'channel_id': world['channel'].id})
    assert r.status_code == 403
    assert r.get_json() == {'error': 'You are not assigned to this radio'}


def test_non_kiosk_tokens_unaffected(client, world):
    h = _bearer(create_access_token(identity=str(world['admin'].id),
                                    additional_claims={'roles': ['administrator']}))
    assert client.get(PREFIX + '/users', headers=h).status_code == 200
    assert client.get(PREFIX + '/auth/me', headers=h).status_code == 200


def test_new_route_is_denied_by_default_for_kiosk(db):
    """A route registered on api_v1 later is denied for kiosk tokens without touching the allowlist."""
    fresh = create_app()  # new app: rules can still be added before its first request
    fresh.config['WTF_CSRF_ENABLED'] = False

    def dummy():
        return {'ok': True}

    fresh.add_url_rule('/api/v1/zz-new-kiosk-endpoint', 'api_v1.zz_new_kiosk_endpoint',
                       jwt_required()(dummy), methods=['GET'])
    c = fresh.test_client()
    user = _user(db, 'kioskplain')
    kiosk_h = _bearer(create_access_token(identity=str(user.id),
                                          additional_claims={'kiosk_session': True}))
    plain_h = _bearer(create_access_token(identity=str(user.id)))
    try:
        r = c.get(PREFIX + '/zz-new-kiosk-endpoint', headers=kiosk_h)
        assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}
        assert c.get(PREFIX + '/zz-new-kiosk-endpoint', headers=plain_h).status_code == 200
    finally:
        db.session.rollback()
        db.session.delete(user)
        db.session.commit()


def test_kiosk_allowlist_has_no_stale_entries(app):
    rules = {}
    for rule in app.url_map.iter_rules():
        rules.setdefault(rule.endpoint, set()).update(rule.methods or ())
    for endpoint, method in kiosk_mod.KIOSK_ALLOWED:
        assert endpoint in rules, f'stale allowlist endpoint {endpoint}'
        assert method in rules[endpoint], f'{endpoint} does not accept {method}'
