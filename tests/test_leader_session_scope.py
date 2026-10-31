"""
Event-leader sessions are deny-by-default (app/api_v1/leader_scope.py): a token with
the `event_leader_team` claim may only call the allowlisted endpoints.
"""
import datetime

import pytest
from flask_jwt_extended import create_access_token, create_refresh_token

from app import create_app
from app.api_v1 import leader_scope
from app.models import (EventLeaderSessionModel, EventModel, EventLinkModel, EventSessionModel,
                        EventTeamModel, SystemSettingsModel, UserModel, UserRoleModel)
from tests.test_event_my_team import (STRONG, _leader_claims, _leader_headers, _leader_link, _team,
                                      _user, strong_key)  # noqa: F401
from tests.test_event_device_accounts import ots  # noqa: F401  (fake OTS)

DENIED_MSG = 'Not available for event leader sessions'
REVOKED_MSG = 'Leader session is no longer valid'
PREFIX = '/api/v1'

# (method, path) - none are allowlisted, all are real routes guarded by a JWT or API-key decorator.
NON_ALLOWLISTED = [
    ('POST', '/auth/complete-profile'),
    ('POST', '/auth/change-password'),
    ('POST', '/auth/set-password'),
    ('GET', '/users'),
    ('PUT', '/users/1'),
    ('GET', '/tak-profiles'),
    ('GET', '/radios'),
    ('GET', '/admin/announcements'),
    ('GET', '/announcements/unread-count'),
    ('GET', '/approvals/check'),
    ('GET', '/admin/settings'),
    ('POST', '/auth/impersonate/1'),
]


@pytest.fixture(autouse=True)
def _clean(db):
    yield
    db.session.rollback()
    for model in (EventLeaderSessionModel, EventSessionModel, EventLinkModel, EventTeamModel, EventModel):
        model.query.delete()
    db.session.commit()


@pytest.fixture
def world(db, strong_key):
    SystemSettingsModel.set_setting('events_enabled', True)
    now = datetime.datetime.now()
    event = EventModel(name='ev', playerAccessMode='shared',
                       validFrom=now - datetime.timedelta(hours=1),
                       validUntil=now + datetime.timedelta(hours=5))
    db.session.add(event)
    db.session.commit()
    leader = _user(db, 'scopeleader')
    team, _ = _team(db, 'A', leader, event)
    return dict(event=event, leader=leader, team=team)


def _claims(w):
    return _leader_claims(w['team'], w['event'])


def _call(client, method, path, headers=None):
    return client.open(PREFIX + path, method=method, headers=headers or {}, json={})


@pytest.mark.parametrize('method,path', NON_ALLOWLISTED)
def test_leader_denied_on_non_allowlisted(client, world, method, path):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    r = _call(client, method, path, h)
    assert r.status_code == 403
    assert r.get_json() == {'error': DENIED_MSG}


def test_leader_refresh_token_denied_on_non_allowlisted(client, world):
    h = {'Authorization': 'Bearer ' + create_refresh_token(
        identity=str(world['leader'].id), additional_claims=_claims(world))}
    for method, path in NON_ALLOWLISTED:
        r = _call(client, method, path, h)
        assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}, path
    r = client.post(PREFIX + '/auth/refresh', headers=h)
    assert r.status_code == 200
    # the refreshed access token keeps the leader scope
    new = {'Authorization': 'Bearer ' + r.get_json()['access_token']}
    assert _call(client, 'POST', '/auth/change-password', new).get_json() == {'error': DENIED_MSG}


def test_leader_allowlisted_endpoints_work(client, world, ots):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    assert client.get(PREFIX + '/events/my-team', headers=h).status_code == 200
    assert client.get(PREFIX + '/events/my-team/accounts', headers=h).status_code == 200
    for path in ('/auth/me', '/auth/permissions'):
        r = client.get(PREFIX + path, headers=h)
        assert r.status_code == 200, path
    # Not blocked by the scope hook (endpoint's own validation answers); the hook's
    # 401/403 messages must not appear and nothing may 5xx.
    for method, path in (('POST', '/events/my-team/enrollment'),
                         ('POST', '/events/my-team/accounts/999999/move')):
        r = client.open(PREFIX + path, method=method, headers=h, json={'target': 'team'})
        assert r.status_code < 500, (path, r.status_code)
        assert r.status_code not in (401, 403), (path, r.status_code, r.get_json())
        # enrollment succeeds with the fake OTS; moving an unknown device is a 404
        assert r.status_code == (200 if path.endswith('enrollment') else 404), path
        assert (r.get_json() or {}).get('error') not in (DENIED_MSG, REVOKED_MSG), path


def _revoked(client, h):
    for method, path in (('GET', '/auth/me'), ('GET', '/auth/permissions')):
        r = _call(client, method, path, h)
        assert r.status_code == 401 and r.get_json() == {'error': REVOKED_MSG}, path
    # my-team endpoints check the session themselves and keep answering 403
    for method, path in (('GET', '/events/my-team'), ('GET', '/events/my-team/accounts'),
                         ('POST', '/events/my-team/enrollment'),
                         ('POST', '/events/my-team/accounts/1/move')):
        r = _call(client, method, path, h)
        assert r.status_code == 403 and r.get_json().get('error') != REVOKED_MSG, path


def _refresh_headers(w):
    return {'Authorization': 'Bearer ' + create_refresh_token(
        identity=str(w['leader'].id), additional_claims=_claims(w))}


def test_regenerated_link_revokes_session(client, db, world):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    rh = _refresh_headers(world)
    link = _leader_link(world['team'], world['event'])
    link.tokenVersion += 1
    db.session.commit()
    _revoked(client, h)
    r = client.post(PREFIX + '/auth/refresh', headers=rh)
    assert r.status_code == 401 and 'access_token' not in (r.get_json() or {})


def test_nonce_change_revokes_session(client, db, world):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    rh = _refresh_headers(world)
    link = _leader_link(world['team'], world['event'])
    link.tokenNonce = 'other'
    db.session.commit()
    _revoked(client, h)
    assert client.post(PREFIX + '/auth/refresh', headers=rh).status_code == 401


def test_deleted_link_revokes_session(client, db, world):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    rh = _refresh_headers(world)
    db.session.delete(_leader_link(world['team'], world['event']))
    db.session.commit()
    _revoked(client, h)
    assert client.post(PREFIX + '/auth/refresh', headers=rh).status_code == 401


def test_replaced_leader_revokes_session(client, db, world):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    rh = _refresh_headers(world)
    other = _user(db, 'scopeleader2')
    world['team'].leaderUserId = other.id
    db.session.commit()
    _revoked(client, h)
    assert client.post(PREFIX + '/auth/refresh', headers=rh).status_code == 401


def test_deactivated_event_revokes_session(client, db, world):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    rh = _refresh_headers(world)
    world['event'].active = False
    db.session.commit()
    _revoked(client, h)
    assert client.post(PREFIX + '/auth/refresh', headers=rh).status_code == 401


def test_events_disabled_revokes_session(client, db, world):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    rh = _refresh_headers(world)
    SystemSettingsModel.set_setting('events_enabled', False)
    try:
        _revoked(client, h)
        assert client.post(PREFIX + '/auth/refresh', headers=rh).status_code == 401
    finally:
        SystemSettingsModel.set_setting('events_enabled', True)


def test_leader_me_is_minimal_and_does_not_leak_admin(client, db, world):
    leader = world['leader']
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='admin')
    if role not in leader.roles:
        leader.roles.append(role)
    leader.email = 'real@example.com'
    leader.firstName, leader.lastName = 'Real', 'Person'
    leader.onboardedBy = _user(db, 'scopeonboarder').id
    db.session.commit()
    h = _leader_headers(leader, world['team'], world['event'])
    body = client.get(PREFIX + '/auth/me', headers=h).get_json()
    assert set(body) == {'id', 'username', 'roles', 'has_password', 'language'}
    assert body['roles'] == [] and body['has_password'] is True
    raw = str(body)
    assert 'real@example.com' not in raw and 'Real' not in raw and 'administrator' not in raw
    perms = client.get(PREFIX + '/auth/permissions', headers=h).get_json()
    assert perms['roles'] == ['event_leader'] and perms['isAdmin'] is False


def test_refreshed_leader_token_has_no_real_roles(client, db, world):
    leader = world['leader']
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='admin')
    if role not in leader.roles:
        leader.roles.append(role)
        db.session.commit()
    r = client.post(PREFIX + '/auth/refresh', headers=_refresh_headers(world))
    assert r.status_code == 200
    from flask_jwt_extended import decode_token
    claims = decode_token(r.get_json()['access_token'])
    assert claims['roles'] == []
    assert claims['event_leader_team'] == world['team'].id


def test_leader_cannot_use_access_token_as_refresh(client, world):
    h = _leader_headers(world['leader'], world['team'], world['event'])
    assert client.post(PREFIX + '/auth/refresh', headers=h).status_code == 401


def test_normal_and_admin_tokens_unaffected(client, db, world):
    u = _user(db, 'scopeplain')
    admin = _user(db, 'scopeadmin')
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='admin')
    if role not in admin.roles:
        admin.roles.append(role)
        db.session.commit()
    plain = {'Authorization': 'Bearer ' + create_access_token(identity=str(u.id))}
    adm = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(admin.id), additional_claims={'roles': ['administrator']})}
    imp = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(u.id), additional_claims={'impersonated_by': admin.id})}
    kiosk = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(u.id), additional_claims={'kiosk_session': True})}
    for h in (plain, adm, imp, kiosk):
        for method, path in NON_ALLOWLISTED:
            r = _call(client, method, path, h)
            assert (r.get_json() or {}).get('error') != DENIED_MSG, (path, h)
        if h is not kiosk:  # kiosk tokens are restricted by their own allowlist (kiosk.py)
            assert client.get(PREFIX + '/auth/me', headers=h).status_code == 200
    assert client.get(PREFIX + '/users', headers=adm).status_code == 200


def test_no_or_invalid_token_gets_endpoint_own_401(client):
    assert client.get(PREFIX + '/users').status_code == 401
    r = client.get(PREFIX + '/users', headers={'Authorization': 'Bearer garbage'})
    assert r.status_code in (401, 422)
    assert (r.get_json() or {}).get('error') != DENIED_MSG
    # public endpoints still work without a token
    assert client.get(PREFIX + '/settings').status_code == 200


def test_new_route_is_denied_by_default(db, world):
    """A route registered on api_v1 later is denied for leaders without touching the allowlist."""
    fresh = create_app()  # new app: rules can still be added before its first request
    fresh.config['WTF_CSRF_ENABLED'] = False

    def dummy():
        return {'ok': True}

    from flask_jwt_extended import jwt_required
    fresh.add_url_rule('/api/v1/zz-new-endpoint', 'api_v1.zz_new_endpoint',
                       jwt_required()(dummy), methods=['GET'])
    c = fresh.test_client()
    leader = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(world['leader'].id), additional_claims=_claims(world))}
    plain = {'Authorization': 'Bearer ' + create_access_token(identity=str(world['leader'].id))}
    r = c.get(PREFIX + '/zz-new-endpoint', headers=leader)
    assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}
    assert c.get(PREFIX + '/zz-new-endpoint', headers=plain).status_code == 200


def test_allowlist_has_no_stale_entries(app):
    rules = {}
    for rule in app.url_map.iter_rules():
        rules.setdefault(rule.endpoint, set()).update(rule.methods or ())
    for endpoint, method in leader_scope.LEADER_ALLOWED:
        assert endpoint in rules, f'stale allowlist endpoint {endpoint}'
        assert method in rules[endpoint], f'{endpoint} does not accept {method}'
        assert endpoint.startswith('api_v1.')
