"""
Tests for the portal username convention: ^[a-z0-9]{3,32}$
"""
import pytest
from flask_jwt_extended import create_access_token

from app.api_v1 import events, oidc, users as users_api
from app.api_v1.oidc import _sanitize_ots_username
from app.models import (EventTeamModel, OIDCProviderModel, SystemSettingsModel,
                        UserModel, UserRoleModel)


class FakeOTS:
    """Stand-in for OTSClient that records created users."""

    def __init__(self, *args, **kwargs):
        self.created = []

    def create_user(self, username, password, roles=None):
        self.created.append(username)

    def add_user_to_group(self, *args, **kwargs):
        pass

    def user_exists(self, username):
        return False


@pytest.fixture
def admin_headers(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    admin = UserModel.query.filter_by(username='unameadmin').first() or \
        UserModel.create_user(username='unameadmin', email='unameadmin@example.com')
    if role not in admin.roles:
        admin.roles.append(role)
        db.session.commit()
    token = create_access_token(identity=str(admin.id), additional_claims={'roles': ['administrator']})
    return {'Authorization': f'Bearer {token}'}


@pytest.mark.parametrize('raw,expected', [
    ('ots-dev', 'otsdev'),
    ('John.Doe', 'johndoe'),
    ('a_b', 'userab'),
    ('', 'user'),
    (None, 'user'),
    ('---', 'user'),
    ('ab', 'userab'),
    ('éè中文', 'user'),
])
def test_sanitize_username(raw, expected):
    assert _sanitize_ots_username(raw) == expected


def test_sanitize_truncates_to_32():
    result = _sanitize_ots_username('x' * 40)
    assert result == 'x' * 32


def test_oidc_collision_suffix_stays_within_32(app, client, db, monkeypatch):
    provider = OIDCProviderModel(name='unametest', display_name='Test', discovery_url='http://idp/.well-known',
                                 client_id='cid', client_secret='secret', enabled=True, sync_roles=False)
    db.session.add(provider)
    db.session.commit()
    UserModel.create_user(username='a' * 32, email='base@example.com')

    monkeypatch.setattr(oidc, 'OTSClient', lambda *a, **k: FakeOTS())
    monkeypatch.setattr(oidc, '_fetch_discovery', lambda url: {
        'token_endpoint': 'http://idp/token', 'userinfo_endpoint': 'http://idp/userinfo'})
    monkeypatch.setattr(SystemSettingsModel, 'get_setting',
                        staticmethod(lambda key, default=None: True if key == 'oidc_auto_create_user' else default))

    class Resp:
        status_code = 200
        headers = {}
        text = ''

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    state = {'email': None}
    monkeypatch.setattr(oidc.http_requests, 'post',
                        lambda *a, **k: Resp({'access_token': 'tok'}))
    monkeypatch.setattr(oidc.http_requests, 'get',
                        lambda *a, **k: Resp({'sub': 's-' + state['email'], 'email': state['email'],
                                              'preferred_username': 'a' * 40}))

    for email in ('c1@example.com', 'c2@example.com'):
        state['email'] = email
        with client.session_transaction() as sess:
            sess['oidc_state'] = 'st'
            sess['oidc_nonce'] = 'n'
            sess['oidc_provider_id'] = provider.id
        client.get(f'/api/v1/auth/oidc/{provider.id}/callback?state=st&code=abc')

    try:
        names = [UserModel.query.filter_by(email=e).first().username for e in ('c1@example.com', 'c2@example.com')]
        assert names == ['a' * 31 + '1', 'a' * 31 + '2']
        assert all(len(n) <= 32 for n in names)
    finally:
        UserModel.query.filter(UserModel.email.in_(('base@example.com', 'c1@example.com', 'c2@example.com'))).delete()
        OIDCProviderModel.query.filter_by(name='unametest').delete()
        db.session.commit()


@pytest.fixture
def leader_team(db, monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(events, 'otsClient', fake)
    team = EventTeamModel(name='unametest_team', color='Red')
    db.session.add(team)
    db.session.commit()
    yield team, fake
    EventTeamModel.query.filter_by(id=team.id).delete()  # before the user it references
    UserModel.query.filter_by(username='abc').delete()
    db.session.commit()


@pytest.mark.parametrize('username', ['ab', 'a_b', 'a.b', 'x' * 33])
def test_event_leader_rejects_invalid_username(client, admin_headers, leader_team, username):
    team, fake = leader_team
    resp = client.post(f'/api/v1/events/teams/{team.id}/leader', headers=admin_headers,
                       json={'username': username, 'password': 'longenough'})

    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Username must be 3-32 characters, letters and numbers only'
    assert fake.created == []


def test_event_leader_accepts_valid_username(client, admin_headers, leader_team):
    team, fake = leader_team
    resp = client.post(f'/api/v1/events/teams/{team.id}/leader', headers=admin_headers,
                       json={'username': 'abc', 'password': 'longenough'})

    assert resp.status_code == 201
    assert fake.created == ['abc']


PAYLOAD = {'password': 'longenough', 'email': 'foobar@example.com',
           'firstName': 'Foo', 'lastName': 'Bar', 'callsign': 'FB'}


def test_create_user_rejects_underscore(client, admin_headers, monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(users_api, 'OTSClient', lambda *a, **k: fake)
    resp = client.post('/api/v1/users', headers=admin_headers, json={**PAYLOAD, 'username': 'Foo_Bar'})

    assert resp.status_code == 400
    assert fake.created == []


def test_create_user_rejects_bad_length(client, admin_headers, monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(users_api, 'OTSClient', lambda *a, **k: fake)
    short = client.post('/api/v1/users', headers=admin_headers, json={**PAYLOAD, 'username': 'ab'})
    long = client.post('/api/v1/users', headers=admin_headers, json={**PAYLOAD, 'username': 'a' * 33})

    assert short.status_code == long.status_code == 400
    assert fake.created == []


def test_create_user_normalizes_username(client, admin_headers, monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(users_api, 'OTSClient', lambda *a, **k: fake)
    try:
        resp = client.post('/api/v1/users', headers=admin_headers, json={**PAYLOAD, 'username': 'FooBar'})

        assert resp.status_code == 201
        assert fake.created == ['foobar']
        assert UserModel.query.filter_by(username='foobar').first() is not None
    finally:
        UserModel.query.filter_by(username='foobar').delete()
        UserModel.query.session.commit()


def test_create_user_rejects_non_string_username(client, admin_headers, monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(users_api, 'OTSClient', lambda *a, **k: fake)
    resp = client.post('/api/v1/users', headers=admin_headers, json={**PAYLOAD, 'username': 12345})

    assert resp.status_code == 400
    assert fake.created == []
