"""
Event access links: tokens are derived from SECRET_KEY + link id + version so the
QR can be re-shown; weak keys and legacy links fall back to non-retrievable.
"""
import pytest
from flask_jwt_extended import create_access_token

from app.api_v1 import events as events_mod
from app.models import (EventLinkModel, EventModel, EventSessionModel, EventTeamModel,
                        SystemSettingsModel, UserModel, UserRoleModel)

STRONG = 'a-strong-secret-key-for-tests-0123456789'


def _headers(db, username, role_name):
    role = UserRoleModel.query.filter_by(name=role_name).first() or \
        UserRoleModel.create_role(name=role_name, description=role_name)
    user = UserModel.query.filter_by(username=username).first() or \
        UserModel.create_user(username=username, email=f'{username}@example.com')
    if role not in user.roles:
        user.roles.append(role)
        db.session.commit()
    token = create_access_token(identity=str(user.id), additional_claims={'roles': [role_name]})
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def admin_headers(db):
    return _headers(db, 'evtlinkadmin', 'events_admin')


@pytest.fixture
def readonly_headers(db):
    return _headers(db, 'evtlinkreader', 'events_readonly')


@pytest.fixture
def strong_key(app):
    old = app.config.get('SECRET_KEY')
    app.config['SECRET_KEY'] = STRONG
    yield
    app.config['SECRET_KEY'] = old


@pytest.fixture(autouse=True)
def _clean_events(db):
    yield
    db.session.rollback()
    for model in (EventSessionModel, EventLinkModel, EventModel, EventTeamModel):
        model.query.delete()
    db.session.commit()


@pytest.fixture
def setup(db):
    ev = EventModel(name='e')
    team = EventTeamModel(name='t')
    db.session.add_all([ev, team])
    db.session.commit()
    SystemSettingsModel.set_setting('events_enabled', True)
    return ev, team


def _create(client, headers, ev, team, role='leader'):
    r = client.post(f'/api/v1/events/{ev.id}/links', headers=headers,
                    json={'teamId': team.id, 'role': role})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _token(url):
    return url.rsplit('/', 1)[1]


def test_derivation_is_deterministic_and_changes_on_regenerate(client, db, admin_headers, strong_key, setup):
    ev, team = setup
    data = _create(client, admin_headers, ev, team)
    link = EventLinkModel.query.get(data['id'])
    assert data['token'] == events_mod._derive_link_token(link)
    assert events_mod._derive_link_token(link) == events_mod._derive_link_token(link)

    r = client.post(f"/api/v1/events/links/{data['id']}/revoke", headers=admin_headers)
    assert r.status_code == 200
    assert r.get_json()['token'] != data['token']
    assert r.get_json()['tokenVersion'] == 2


def test_old_token_dies_new_works(client, db, admin_headers, strong_key, setup):
    ev, team = setup
    data = _create(client, admin_headers, ev, team)
    new = client.post(f"/api/v1/events/links/{data['id']}/revoke", headers=admin_headers).get_json()
    assert client.post(f"/api/v1/events/join/{data['token']}").status_code == 404
    assert client.post(f"/api/v1/events/join/{new['token']}").status_code != 404


def test_url_endpoint_returns_joinable_token(client, db, admin_headers, strong_key, setup):
    ev, team = setup
    data = _create(client, admin_headers, ev, team)
    r = client.get(f"/api/v1/events/links/{data['id']}/url", headers=admin_headers)
    assert r.status_code == 200
    url = r.get_json()['url']
    assert url == data['url']
    assert client.post(f'/api/v1/events/join/{_token(url)}').status_code != 404
    listed = client.get(f'/api/v1/events/{ev.id}/links', headers=admin_headers).get_json()['links']
    assert listed[0]['retrievable'] is True


def test_legacy_random_link_not_retrievable(client, db, admin_headers, strong_key, setup):
    ev, team = setup
    link = EventLinkModel(eventId=ev.id, eventTeamId=team.id, role='player',
                          tokenHash=events_mod._hash_token('legacy-random'))
    db.session.add(link)
    db.session.commit()
    r = client.get(f'/api/v1/events/links/{link.id}/url', headers=admin_headers)
    assert r.status_code == 409
    listed = client.get(f'/api/v1/events/{ev.id}/links', headers=admin_headers).get_json()['links']
    assert listed[0]['retrievable'] is False
    # Regenerating makes it retrievable.
    client.post(f'/api/v1/events/links/{link.id}/revoke', headers=admin_headers)
    assert client.get(f'/api/v1/events/links/{link.id}/url', headers=admin_headers).status_code == 200


def test_unknown_link_404(client, db, admin_headers, strong_key):
    assert client.get('/api/v1/events/links/999999/url', headers=admin_headers).status_code == 404


@pytest.mark.parametrize('weak', [None, 'None', 'dev', '', 'short'])
def test_weak_key_falls_back_to_random(app, client, db, admin_headers, setup, weak):
    ev, team = setup
    old = app.config.get('SECRET_KEY')
    app.config['SECRET_KEY'] = weak
    try:
        data = _create(client, admin_headers, ev, team)
        assert data['retrievable'] is False
        assert client.get(f"/api/v1/events/links/{data['id']}/url", headers=admin_headers).status_code == 409
        assert client.post(f"/api/v1/events/join/{data['token']}").status_code != 404
        r = client.post(f"/api/v1/events/links/{data['id']}/revoke", headers=admin_headers)
        assert r.status_code == 200 and r.get_json()['retrievable'] is False
    finally:
        app.config['SECRET_KEY'] = old


def test_rbac(client, db, admin_headers, readonly_headers, strong_key, setup):
    ev, team = setup
    data = _create(client, admin_headers, ev, team)
    url = f"/api/v1/events/links/{data['id']}/url"
    assert client.get(url, headers=admin_headers).status_code == 200
    assert client.get(url, headers=readonly_headers).status_code == 403
    assert client.get(url).status_code == 401


def test_two_creates_keep_hashes_unique(client, db, admin_headers, strong_key, setup):
    ev, team = setup
    a = _create(client, admin_headers, ev, team, 'leader')
    b = _create(client, admin_headers, ev, team, 'player')
    assert a['token'] != b['token']
    hashes = [l.tokenHash for l in EventLinkModel.query.filter_by(eventId=ev.id)]
    assert len(set(hashes)) == 2


def test_deleted_link_id_reuse_does_not_revive_old_token(client, db, admin_headers, strong_key, setup):
    ev, team = setup
    old = _create(client, admin_headers, ev, team)
    assert client.delete(f"/api/v1/events/links/{old['id']}", headers=admin_headers).status_code in (200, 204)
    new = _create(client, admin_headers, ev, team)
    assert new['id'] == old['id'], 'test DB did not reuse the id; scenario not exercised'
    assert new['token'] != old['token']
    assert client.post(f"/api/v1/events/join/{old['token']}").status_code == 404
    assert client.post(f"/api/v1/events/join/{new['token']}").status_code != 404


def test_null_nonce_legacy_link_not_retrievable(client, db, admin_headers, strong_key, setup):
    ev, team = setup
    data = _create(client, admin_headers, ev, team)
    link = EventLinkModel.query.get(data['id'])
    link.tokenNonce = None
    db.session.commit()
    assert client.get(f"/api/v1/events/links/{link.id}/url", headers=admin_headers).status_code == 409
    listed = client.get(f'/api/v1/events/{ev.id}/links', headers=admin_headers).get_json()['links']
    assert listed[0]['retrievable'] is False


def test_regenerate_changes_nonce(client, db, admin_headers, strong_key, setup):
    ev, team = setup
    data = _create(client, admin_headers, ev, team)
    before = EventLinkModel.query.get(data['id']).tokenNonce
    assert before
    client.post(f"/api/v1/events/links/{data['id']}/revoke", headers=admin_headers)
    db.session.expire_all()
    assert EventLinkModel.query.get(data['id']).tokenNonce != before
