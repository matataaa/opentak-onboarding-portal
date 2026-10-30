"""
OIDC-created users must never take over an OTS account that already exists
under the same username (created outside the portal).
"""
import pytest
from flask_jwt_extended import create_access_token

from app.api_v1 import auth as auth_module, oidc
from app.exceptions import BadRequestError
from datetime import datetime, timedelta

from app.models import OIDCProviderModel, OneTimeTokenModel, SystemSettingsModel, UserModel
from app.ots import OTSClient


class FakeOTS:
    """Stand-in for OTSClient with a fixed set of existing OTS usernames."""

    def __init__(self, existing=(), create_error=None, lookup_error=None):
        self.existing = set(existing)
        self.create_error = create_error
        self.lookup_error = lookup_error
        self.created = []
        self.resets = []

    def user_exists(self, username):
        if self.lookup_error:
            raise self.lookup_error
        return username in self.existing

    def create_user(self, username, password, roles=None):
        if self.create_error:
            raise self.create_error
        self.created.append(username)

    def reset_user_password(self, username, password):
        self.resets.append(username)


# --- OIDC auto-create -------------------------------------------------------

@pytest.fixture
def oidc_login(client, db, monkeypatch):
    provider = OIDCProviderModel(name='collisiontest', display_name='Test', discovery_url='http://idp/.well-known',
                                 client_id='cid', client_secret='secret', enabled=True, sync_roles=False)
    db.session.add(provider)
    db.session.commit()

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

    monkeypatch.setattr(oidc.http_requests, 'post', lambda *a, **k: Resp({'access_token': 'tok'}))
    monkeypatch.setattr(oidc.http_requests, 'get', lambda *a, **k: Resp(
        {'sub': 'sub-collision', 'email': 'collision@example.com', 'preferred_username': 'stranger'}))

    def login():
        with client.session_transaction() as sess:
            sess['oidc_state'] = 'st'
            sess['oidc_nonce'] = 'n'
            sess['oidc_provider_id'] = provider.id
        return client.get(f'/api/v1/auth/oidc/{provider.id}/callback?state=st&code=abc')

    yield login
    UserModel.query.filter_by(email='collision@example.com').delete()
    OIDCProviderModel.query.filter_by(name='collisiontest').delete()
    db.session.commit()


def test_oidc_skips_username_that_exists_only_in_ots(oidc_login, monkeypatch):
    fake = FakeOTS(existing={'stranger'})
    monkeypatch.setattr(oidc, 'OTSClient', lambda *a, **k: fake)

    resp = oidc_login()

    assert resp.status_code == 302
    assert 'oidc_token=' in resp.headers['Location']
    user = UserModel.query.filter_by(email='collision@example.com').first()
    assert user.username == 'stranger1'
    assert user.has_password is False


def test_oidc_ots_unreachable_creates_no_user(oidc_login, monkeypatch):
    fake = FakeOTS(lookup_error=ConnectionError('Could not connect to OTS server'))
    monkeypatch.setattr(oidc, 'OTSClient', lambda *a, **k: fake)

    resp = oidc_login()

    assert resp.status_code == 302
    assert 'oidc_error=Cannot+reach+the+TAK+server' in resp.headers['Location']
    assert UserModel.query.filter_by(email='collision@example.com').first() is None


def test_oidc_legacy_user_without_flag_is_not_sent_to_set_password(oidc_login, db, monkeypatch):
    # Rows from before the has_password column have NULL: they already have a password.
    user = UserModel.create_user(username='legacyuser', email='collision@example.com')
    user.has_password = None
    db.session.commit()
    monkeypatch.setattr(oidc, 'OTSClient', lambda *a, **k: FakeOTS())

    resp = oidc_login()

    assert 'needs_password=false' in resp.headers['Location']


# --- set-password -----------------------------------------------------------

@pytest.fixture
def oidc_user(db):
    user = UserModel.create_user(username='oidcnopass', email='oidcnopass@example.com')
    user.has_password = False
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    yield user, {'Authorization': f'Bearer {token}'}
    UserModel.query.filter_by(username='oidcnopass').delete()
    db.session.commit()


def _use_ots(monkeypatch, fake):
    monkeypatch.setattr(auth_module, 'OTSClient', lambda url, username, password: fake)


def test_set_password_refused_when_password_already_set(client, db, oidc_user, monkeypatch):
    user, headers = oidc_user
    user.has_password = True
    db.session.commit()
    fake = FakeOTS(existing={'oidcnopass'}, create_error=BadRequestError('User oidcnopass already exists'))
    _use_ots(monkeypatch, fake)

    resp = client.post('/api/v1/auth/set-password', headers=headers, json={'newPassword': 'longenough'})

    assert resp.status_code == 409
    assert resp.get_json()['code'] == 'password_already_set'
    assert fake.created == [] and fake.resets == []


def test_set_password_never_resets_existing_ots_account(client, db, oidc_user, monkeypatch):
    user, headers = oidc_user
    fake = FakeOTS(existing={'oidcnopass'}, create_error=BadRequestError('User oidcnopass already exists'))
    _use_ots(monkeypatch, fake)

    resp = client.post('/api/v1/auth/set-password', headers=headers, json={'newPassword': 'longenough'})

    assert resp.status_code == 409
    assert resp.get_json()['code'] == 'username_taken_on_server'
    assert fake.resets == []
    assert UserModel.get_user_by_id(user.id).has_password is False


def test_set_password_ots_unreachable(client, db, oidc_user, monkeypatch):
    user, headers = oidc_user
    fake = FakeOTS(create_error=ConnectionError('Could not connect to OTS server'))
    _use_ots(monkeypatch, fake)

    resp = client.post('/api/v1/auth/set-password', headers=headers, json={'newPassword': 'longenough'})

    assert resp.status_code == 503
    assert fake.resets == []
    assert UserModel.get_user_by_id(user.id).has_password is False


def test_set_password_creates_ots_account(client, db, oidc_user, monkeypatch):
    user, headers = oidc_user
    fake = FakeOTS()
    _use_ots(monkeypatch, fake)

    resp = client.post('/api/v1/auth/set-password', headers=headers, json={'newPassword': 'longenough'})

    assert resp.status_code == 200
    assert fake.created == ['oidcnopass']
    assert fake.resets == []
    assert UserModel.get_user_by_id(user.id).has_password is True


def test_set_password_ots_rejects_unknown_reason(client, db, oidc_user, monkeypatch):
    user, headers = oidc_user
    fake = FakeOTS(create_error=BadRequestError('Password too weak'))
    _use_ots(monkeypatch, fake)

    resp = client.post('/api/v1/auth/set-password', headers=headers, json={'newPassword': 'longenough'})

    assert resp.status_code == 400
    assert fake.resets == []
    assert UserModel.get_user_by_id(user.id).has_password is False


def test_set_password_lookup_failure_does_not_leak_host(client, db, oidc_user, monkeypatch):
    user, headers = oidc_user
    fake = FakeOTS(create_error=BadRequestError('User oidcnopass already exists'),
                   lookup_error=ConnectionError('Could not connect to OTS server: https://ots.example:8443'))
    _use_ots(monkeypatch, fake)

    resp = client.post('/api/v1/auth/set-password', headers=headers, json={'newPassword': 'longenough'})

    assert resp.status_code == 503
    assert 'ots.example' not in resp.get_data(as_text=True)
    assert fake.resets == []
    assert UserModel.get_user_by_id(user.id).has_password is False


@pytest.mark.parametrize('username,expected', [('bob', True), ('bobby', False), ('Bob', False)])
def test_ots_user_exists_matches_exactly(monkeypatch, username, expected):
    ots = OTSClient('http://ots', 'svc', 'pw')
    monkeypatch.setattr(ots, 'get_users', lambda **k: {'response': {'results': [{'username': 'bob'}]}})

    assert ots.user_exists(username) is expected


def test_magic_link_legacy_user_without_flag_needs_no_password(client, db, monkeypatch):
    monkeypatch.setattr(SystemSettingsModel, 'get_setting',
                        staticmethod(lambda key, default=None: True if key == 'magic_link_login_enabled' else default))
    user = UserModel.create_user(username='legacymagic', email='legacymagic@example.com')
    user.has_password = None
    db.session.commit()
    OneTimeTokenModel.create_token(user.id, 'magic-legacy-token', 'magic_login', datetime.now() + timedelta(minutes=10))
    try:
        resp = client.post('/api/v1/auth/magic-link/verify', json={'token': 'magic-legacy-token'})

        assert resp.status_code == 200
        assert resp.get_json()['needs_password'] is False
    finally:
        OneTimeTokenModel.query.filter_by(user_id=user.id).delete()
        UserModel.query.filter_by(username='legacymagic').delete()
        db.session.commit()
