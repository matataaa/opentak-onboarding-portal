"""
New passwords are limited to what OTS can actually log in with: every endpoint that
takes one rejects the rest with 400 before anything reaches OTS. Passwords already
stored on a pending registration and login itself are deliberately not validated.
"""
from datetime import datetime, timedelta
from itertools import count

import pytest
from flask_jwt_extended import create_access_token

from app import ots as ots_module
from app.api_v1 import auth as auth_module, events as events_module, users as users_module
from app.models import (EventTeamModel, OneTimeTokenModel, OnboardingCodeModel, OTSGroupModel,
                        PendingRegistrationModel, SystemSettingsModel, UserModel, UserRoleModel, db as _db)
from app.ots import new_password_error

PREFIX = '/api/v1'
BAD = 'abcd&efgh'
GOOD = 'abcd!efgh'
STALE = 'old&stored^password'
SHORT = 'abc!efg'  # 7 characters, OTS's minimum is 8
RULE_ERROR = new_password_error(BAD)
LENGTH_ERROR = 'Password must be at least 8 characters'
_ids = count(1)


def _name(prefix):
    return f'{prefix}{next(_ids)}'


class FakeOTS:
    """Stand-in for OTSClient and otsClient: records every method called on it."""

    def __init__(self, calls, username=None, password=None):
        self._calls = calls
        self._username = username
        self._password = password

    def __getattr__(self, method):
        def record(*args, **kwargs):
            self._calls.append((method, self._username, self._password, args, kwargs))
            return {'status_code': 200, 'response': {'email': 'pwrules@example.com', 'roles': [], 'groups': []}}
        return record


@pytest.fixture
def ots_calls(monkeypatch):
    calls = []
    factory = lambda url, username, password: FakeOTS(calls, username, password)  # noqa: E731
    monkeypatch.setattr(auth_module, 'OTSClient', factory)
    monkeypatch.setattr(users_module, 'OTSClient', factory)
    monkeypatch.setattr(ots_module, 'OTSClient', factory)  # function-level imports
    monkeypatch.setattr(ots_module, 'otsClient', FakeOTS(calls))
    monkeypatch.setattr(events_module, 'otsClient', FakeOTS(calls))
    monkeypatch.setattr(auth_module, 'send_html_email', lambda *a, **k: None)
    monkeypatch.setattr('app.email.send_html_email', lambda *a, **k: None)
    return calls


@pytest.fixture(autouse=True)
def _rollback_dirty_session(db):
    """Tests share one app context, so what a rejected request left dirty must not linger."""
    yield
    db.session.rollback()


def _headers(user, roles=()):
    token = create_access_token(identity=str(user.id), additional_claims={'roles': list(roles)})
    return {'Authorization': f'Bearer {token}'}


def _user(prefix='pwuser', **attrs):
    username = _name(prefix)
    user = UserModel.create_user(username=username, email=f'{username}@example.com')
    for key, value in attrs.items():
        setattr(user, key, value)
    _db.session.commit()
    return user


@pytest.fixture
def admin(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    user = _user('pwadmin')
    user.roles.append(role)
    db.session.commit()
    return user


def _code(**attrs):
    code = OnboardingCodeModel(onboardingCode=_name('PWRULES'), name=_name('pwrules'), **attrs)
    _db.session.add(code)
    _db.session.commit()
    return code


def _pending(code, password=STALE):
    username = _name('pwpending')
    return PendingRegistrationModel.create_pending_registration(
        username=username, email=f'{username}@example.com', password=password,
        first_name='P', last_name='W', callsign=username, onboarding_code_id=code.id,
        verification_token=_name('pwtoken'), expires_at=datetime.now() + timedelta(hours=24))


def _new_user_body(password=BAD):
    username = _name('pwnew')
    return {'username': username, 'password': password, 'email': f'{username}@example.com',
            'firstName': 'P', 'lastName': 'W', 'callsign': username}


def _assert_rejected(response, ots_calls, error=RULE_ERROR):
    assert response.status_code == 400
    assert response.get_json() == {'error': error}
    assert ots_calls == []


def _methods(ots_calls):
    return [call[0] for call in ots_calls]


# --- The helper -------------------------------------------------------------

@pytest.mark.parametrize('password', [
    *[f'abcd{c}efgh' for c in '&^$<>:@'],
    'abcdéefgh',
    'abcd€efgh',
    'abcdßefgh',
    'abcd\U0001F600efgh',
    'abcd\tefgh',
    'abcd\x7Fefgh',
    'abcd\x1Fefgh',
    'abcdefgh\n',
    ' abcdefgh',
    'abcdefgh ',
    ' ',
    '',
    12345678,
    None,
])
def test_helper_rejects(password):
    assert isinstance(new_password_error(password), str)


@pytest.mark.parametrize('password', [
    'abcd efgh',
    'abcd\x7Eefgh',
    'abcd!#%\'"\\=;efgh',
])
def test_helper_accepts(password):
    assert new_password_error(password) is None


def test_helper_accepts_all_other_printable_ascii():
    for code in range(0x21, 0x7F):
        char = chr(code)
        assert (new_password_error(f'abc{char}defg') is None) == (char not in '&^$<>:@')


def test_helper_rejects_seven_characters():
    assert new_password_error(SHORT) == LENGTH_ERROR


def test_helper_accepts_exactly_eight_characters():
    assert new_password_error('abc!efgh') is None


def test_helper_reports_character_rule_before_length():
    assert new_password_error('a&b') == RULE_ERROR


# --- The nine endpoints -----------------------------------------------------

@pytest.mark.parametrize('attrs', [
    {},
    {'autoApprove': True},
    {'requireApproval': True},
], ids=['standard', 'auto-approve', 'require-approval'])
def test_register(client, db, ots_calls, attrs):
    code = _code(**attrs)
    body = _new_user_body()
    r = client.post(f'{PREFIX}/auth/register', json={**body, 'onboardingCode': code.onboardingCode})
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert PendingRegistrationModel.query.filter_by(username=body['username']).first() is None
    assert UserModel.get_user_by_username(body['username']) is None
    assert db.session.get(OnboardingCodeModel, code.id).uses == 0


def test_change_password(client, db, ots_calls):
    user = _user()
    r = client.post(f'{PREFIX}/auth/change-password', headers=_headers(user),
                    json={'currentPassword': GOOD, 'newPassword': BAD})
    _assert_rejected(r, ots_calls)


def test_change_password_non_string(client, db, ots_calls):
    user = _user()
    r = client.post(f'{PREFIX}/auth/change-password', headers=_headers(user),
                    json={'currentPassword': GOOD, 'newPassword': 12345678})
    _assert_rejected(r, ots_calls)


def test_change_password_does_not_validate_current_password(client, db, ots_calls):
    user = _user()
    r = client.post(f'{PREFIX}/auth/change-password', headers=_headers(user),
                    json={'currentPassword': STALE, 'newPassword': GOOD})
    assert r.status_code == 200
    assert ots_calls[0][:3] == ('login', user.username, STALE)
    assert ots_calls[1][0] == 'reset_user_password'


def test_set_password(client, db, ots_calls):
    user = _user(has_password=False)
    r = client.post(f'{PREFIX}/auth/set-password', headers=_headers(user), json={'newPassword': BAD})
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert db.session.get(UserModel, user.id).has_password is False


def test_set_password_non_string(client, db, ots_calls):
    user = _user(has_password=False)
    r = client.post(f'{PREFIX}/auth/set-password', headers=_headers(user), json={'newPassword': 12345678})
    _assert_rejected(r, ots_calls)


def test_reset_password_non_string_keeps_token_unused(client, db, ots_calls):
    user = _user()
    token = _name('pwreset')
    OneTimeTokenModel.create_token(user.id, token, 'password_reset', datetime.now() + timedelta(hours=1))
    r = client.post(f'{PREFIX}/auth/reset-password', json={'token': token, 'new_password': 12345678})
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert OneTimeTokenModel.query.filter_by(token=token).one().is_used is False


def test_reset_password_keeps_token_usable(client, db, ots_calls):
    user = _user()
    token = _name('pwreset')
    OneTimeTokenModel.create_token(user.id, token, 'password_reset', datetime.now() + timedelta(hours=1))

    r = client.post(f'{PREFIX}/auth/reset-password', json={'token': token, 'new_password': BAD})
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    row = OneTimeTokenModel.query.filter_by(token=token).one()
    assert row.is_used is False

    r = client.post(f'{PREFIX}/auth/reset-password', json={'token': token, 'new_password': GOOD})
    assert r.status_code == 200
    assert ots_calls[0][0] == 'reset_user_password'
    assert ots_calls[0][3] == (user.username, GOOD)
    db.session.refresh(row)
    assert row.is_used is True


def test_create_user(client, db, ots_calls, admin):
    body = _new_user_body()
    r = client.post(f'{PREFIX}/users', headers=_headers(admin, ['administrator']), json=body)
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert UserModel.get_user_by_username(body['username']) is None


def _group_update_body(password):
    group = OTSGroupModel.create_group(name=_name('pwgroup'))
    return {'password': password, 'callsign': 'CHANGED', 'groups': [{'id': group.id, 'direction': 'BOTH'}]}


def test_update_user_admin_bad_password_pushes_nothing(client, db, ots_calls, admin):
    user = _user()
    r = client.put(f'{PREFIX}/users/{user.id}', headers=_headers(admin, ['administrator']),
                   json=_group_update_body(BAD))
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert db.session.get(UserModel, user.id).callsign != 'CHANGED'
    assert db.session.get(UserModel, user.id).group_associations == []


def test_update_user_admin_good_password_pushes_groups_and_password(client, db, ots_calls, admin):
    user = _user()
    r = client.put(f'{PREFIX}/users/{user.id}', headers=_headers(admin, ['administrator']),
                   json=_group_update_body(GOOD))
    assert r.status_code == 200
    assert 'add_user_to_group' in _methods(ots_calls)
    assert 'reset_user_password' in _methods(ots_calls)


def test_update_user_blank_password_means_keep_current(client, db, ots_calls, admin):
    user = _user()
    r = client.put(f'{PREFIX}/users/{user.id}', headers=_headers(admin, ['administrator']),
                   json={'password': '', 'callsign': 'KEPT'})
    assert r.status_code == 200
    assert r.get_json()['passwordChanged'] is False
    assert 'reset_user_password' not in _methods(ots_calls)


def test_update_user_non_admin_password_stays_ignored(client, db, ots_calls):
    user = _user()
    r = client.put(f'{PREFIX}/users/{user.id}', headers=_headers(user), json={'password': BAD, 'callsign': 'OWN'})
    assert r.status_code == 200
    assert r.get_json()['passwordChanged'] is False
    assert 'reset_user_password' not in _methods(ots_calls)


@pytest.mark.parametrize('attrs', [{}, {'autoApprove': True}], ids=['standard', 'auto-approve'])
def test_create_pending_registration(client, db, ots_calls, admin, attrs):
    code = _code(**attrs)
    body = _new_user_body()
    r = client.post(f'{PREFIX}/pending-registrations', headers=_headers(admin, ['administrator']),
                    json={**body, 'onboarding_code_id': code.id})
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert PendingRegistrationModel.query.filter_by(username=body['username']).first() is None
    assert db.session.get(OnboardingCodeModel, code.id).uses == 0


def test_update_pending_registration_keeps_stored_password(client, db, ots_calls, admin):
    pending = _pending(_code(), password=GOOD)
    r = client.put(f'{PREFIX}/pending-registrations/{pending.id}', headers=_headers(admin, ['administrator']),
                   json={'password': BAD})
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert db.session.get(PendingRegistrationModel, pending.id).password == GOOD


def test_update_pending_registration_without_password_keeps_stored_password(client, db, ots_calls, admin, monkeypatch):
    monkeypatch.setattr('app.email.send_html_email', lambda *a, **k: None)
    pending = _pending(_code(), password=STALE)
    r = client.put(f'{PREFIX}/pending-registrations/{pending.id}', headers=_headers(admin, ['administrator']),
                   json={'firstName': 'Renamed'})
    assert r.status_code == 200
    db.session.rollback()
    assert db.session.get(PendingRegistrationModel, pending.id).password == STALE


@pytest.fixture
def events_enabled(db):
    old_enabled = SystemSettingsModel.get_setting('events_enabled', False)
    SystemSettingsModel.set_setting('events_enabled', True)
    yield
    db.session.rollback()
    SystemSettingsModel.set_setting('events_enabled', old_enabled)


def test_create_event_team_leader(client, db, ots_calls, admin, events_enabled):
    team = EventTeamModel(name=_name('pwrulesteam'), color='Red')
    db.session.add(team)
    db.session.commit()
    r = client.post(f'{PREFIX}/events/teams/{team.id}/leader', headers=_headers(admin, ['administrator']),
                    json={'username': _name('pwleader'), 'password': BAD})
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert db.session.get(EventTeamModel, team.id).leaderUserId is None


def test_create_event_team_leader_non_string(client, db, ots_calls, admin, events_enabled):
    team = EventTeamModel(name=_name('pwrulesteam'), color='Red')
    db.session.add(team)
    db.session.commit()
    r = client.post(f'{PREFIX}/events/teams/{team.id}/leader', headers=_headers(admin, ['administrator']),
                    json={'username': _name('pwleader'), 'password': 12345678})
    _assert_rejected(r, ots_calls)
    db.session.rollback()
    assert db.session.get(EventTeamModel, team.id).leaderUserId is None


# --- Minimum length --------------------------------------------------------

def test_register_short_password(client, db, ots_calls):
    code = _code(autoApprove=True)
    body = _new_user_body(SHORT)
    r = client.post(f'{PREFIX}/auth/register', json={**body, 'onboardingCode': code.onboardingCode})
    _assert_rejected(r, ots_calls, LENGTH_ERROR)
    db.session.rollback()
    assert UserModel.get_user_by_username(body['username']) is None


def test_create_user_short_password(client, db, ots_calls, admin):
    body = _new_user_body(SHORT)
    r = client.post(f'{PREFIX}/users', headers=_headers(admin, ['administrator']), json=body)
    _assert_rejected(r, ots_calls, LENGTH_ERROR)
    db.session.rollback()
    assert UserModel.get_user_by_username(body['username']) is None


def test_update_user_admin_short_password(client, db, ots_calls, admin):
    user = _user()
    r = client.put(f'{PREFIX}/users/{user.id}', headers=_headers(admin, ['administrator']),
                   json=_group_update_body(SHORT))
    _assert_rejected(r, ots_calls, LENGTH_ERROR)
    db.session.rollback()
    assert db.session.get(UserModel, user.id).callsign != 'CHANGED'


def test_create_pending_registration_short_password(client, db, ots_calls, admin):
    code = _code(autoApprove=True)
    body = _new_user_body(SHORT)
    r = client.post(f'{PREFIX}/pending-registrations', headers=_headers(admin, ['administrator']),
                    json={**body, 'onboarding_code_id': code.id})
    _assert_rejected(r, ots_calls, LENGTH_ERROR)
    db.session.rollback()
    assert PendingRegistrationModel.query.filter_by(username=body['username']).first() is None


def test_update_pending_registration_short_password(client, db, ots_calls, admin):
    pending = _pending(_code(), password=GOOD)
    r = client.put(f'{PREFIX}/pending-registrations/{pending.id}', headers=_headers(admin, ['administrator']),
                   json={'password': SHORT})
    _assert_rejected(r, ots_calls, LENGTH_ERROR)
    db.session.rollback()
    assert db.session.get(PendingRegistrationModel, pending.id).password == GOOD


# --- Deliberately not validated ---------------------------------------------

def _verify_email(client, pending, admin):
    return client.post(f'{PREFIX}/auth/verify-email', json={'token': pending.verification_token})


def _admin_approve(client, pending, admin):
    return client.post(f'{PREFIX}/pending-registrations/{pending.id}/approve',
                       headers=_headers(admin, ['administrator']))


@pytest.mark.parametrize('flow', [_verify_email, _admin_approve], ids=['verify-email', 'admin-approve'])
@pytest.mark.parametrize('stored', [STALE, SHORT], ids=['bad-characters', 'too-short'])
def test_stored_pending_password_still_creates_account(client, db, ots_calls, admin, flow, stored):
    pending = _pending(_code(), password=stored)
    username = pending.username
    r = flow(client, pending, admin)
    assert r.status_code == 200
    created = [call for call in ots_calls if call[0] == 'create_user']
    assert len(created) == 1
    assert created[0][4]['username'] == username
    assert created[0][4]['password'] == stored
    assert UserModel.get_user_by_username(username) is not None


@pytest.mark.parametrize('password', [STALE, SHORT], ids=['bad-characters', 'too-short'])
def test_login_password_is_not_validated(client, db, ots_calls, password):
    user = _user()
    r = client.post(f'{PREFIX}/auth/login', json={'username': user.username, 'password': password})
    assert r.status_code == 200
    assert ots_calls[0][:3] == ('get_me', user.username, password)


def test_change_password_does_not_length_check_current_password(client, db, ots_calls):
    user = _user()
    r = client.post(f'{PREFIX}/auth/change-password', headers=_headers(user),
                    json={'currentPassword': SHORT, 'newPassword': GOOD})
    assert r.status_code == 200
    assert ots_calls[0][:3] == ('login', user.username, SHORT)
    assert ots_calls[1][0] == 'reset_user_password'
