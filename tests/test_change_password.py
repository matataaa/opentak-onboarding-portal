"""
POST /auth/change-password must verify the current password against OTS before the
password is reset with the service account, so an open session alone can't take over
the account.
"""
import pytest

from app.api_v1 import auth as auth_module
from app.exceptions import AuthenticationError
from app.models import UserModel
from tests.test_kiosk_session import _bearer, _kiosk_tokens, kiosk  # noqa: F401

PREFIX = '/api/v1'
URL = PREFIX + '/auth/change-password'
CURRENT = 'oldpassword'
NEW = 'newpassword'


class FakeOTS:
    """Stand-in for OTSClient: user logins check the password, the service client records resets."""

    def __init__(self, calls, username, password):
        self.calls = calls
        self.username = username
        self.password = password

    def login(self):
        self.calls['logins'].append((self.username, self.password))
        if self.calls['login_error']:
            raise self.calls['login_error']
        if self.password != CURRENT:
            raise AuthenticationError({'status_code': 400})
        return {'status_code': 200}

    def reset_user_password(self, username, password):
        self.calls['resets'].append((self.username, self.password, username, password))
        return {'status_code': 200}


@pytest.fixture
def fake_ots(monkeypatch):
    calls = {'logins': [], 'resets': [], 'login_error': None}
    monkeypatch.setattr(auth_module, 'OTSClient',
                        lambda url, username, password: FakeOTS(calls, username, password))
    return calls


def test_missing_current_password_is_rejected(client, auth_headers, fake_ots):
    assert auth_headers
    r = client.post(URL, headers=auth_headers, json={'newPassword': NEW})
    assert r.status_code == 400
    assert fake_ots['resets'] == []


def test_short_new_password_is_rejected(client, auth_headers, fake_ots):
    r = client.post(URL, headers=auth_headers, json={'currentPassword': CURRENT, 'newPassword': 'short'})
    assert r.status_code == 400
    assert fake_ots['resets'] == []


def test_wrong_current_password_is_forbidden(client, auth_headers, fake_ots):
    r = client.post(URL, headers=auth_headers, json={'currentPassword': 'wrongpassword', 'newPassword': NEW})
    assert r.status_code == 403
    assert r.get_json() == {'error': 'Current password is incorrect'}
    assert fake_ots['logins'] == [('testuser', 'wrongpassword')]
    assert fake_ots['resets'] == []


def test_correct_current_password_resets_with_service_account(app, client, auth_headers, fake_ots):
    r = client.post(URL, headers=auth_headers, json={'currentPassword': CURRENT, 'newPassword': NEW})
    assert r.status_code == 200
    assert fake_ots['logins'] == [('testuser', CURRENT)]
    assert fake_ots['resets'] == [(app.config['OTS_USERNAME'], app.config['OTS_PASSWORD'], 'testuser', NEW)]


def test_ots_unreachable_returns_503(client, auth_headers, fake_ots):
    fake_ots['login_error'] = ConnectionError('Could not connect to OTS server')
    r = client.post(URL, headers=auth_headers, json={'currentPassword': CURRENT, 'newPassword': NEW})
    assert r.status_code == 503
    assert fake_ots['resets'] == []


def test_unexpected_ots_error_returns_502(client, auth_headers, fake_ots):
    fake_ots['login_error'] = Exception({'status_code': 500})
    r = client.post(URL, headers=auth_headers, json={'currentPassword': CURRENT, 'newPassword': NEW})
    assert r.status_code == 502
    assert fake_ots['resets'] == []


def test_user_without_password_must_use_set_password(client, db, auth_headers, fake_ots):
    user = UserModel.get_user_by_username('testuser')
    user.has_password = False
    db.session.commit()
    try:
        r = client.post(URL, headers=auth_headers, json={'currentPassword': CURRENT, 'newPassword': NEW})
        assert r.status_code == 400
        assert fake_ots['logins'] == []
        assert fake_ots['resets'] == []
    finally:
        user.has_password = True
        db.session.commit()


def test_kiosk_token_cannot_change_password(client, db, kiosk, fake_ots):  # noqa: F811
    user = UserModel.query.filter_by(username='kioskpwuser').first() or \
        UserModel.create_user(username='kioskpwuser', email='kioskpwuser@example.com')
    access, _ = _kiosk_tokens(client, user)
    r = client.post(URL, headers=_bearer(access), json={'currentPassword': CURRENT, 'newPassword': NEW})
    assert r.status_code == 403
    assert fake_ots['logins'] == []
    assert fake_ots['resets'] == []
