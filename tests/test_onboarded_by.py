"""
users.onboardedBy is a foreign key to users.id: every path that creates a user from an
onboarding code stores the code's onboard contact as that contact's id, not its username
(PostgreSQL rejects a username there; SQLite would silently store it, so assert the type).
"""
from datetime import datetime, timedelta
from itertools import count

import pytest
from flask_jwt_extended import create_access_token

from app import ots as ots_module
from app.api_v1 import auth as auth_module, events as events_module, users as users_module
from app.models import OnboardingCodeModel, PendingRegistrationModel, UserModel, UserRoleModel, db as _db

PREFIX = '/api/v1'
PASSWORD = 'abcd!efgh'
_ids = count(1)


def _name(prefix):
    return f'{prefix}{next(_ids)}'


class FakeOTS:
    def __getattr__(self, method):
        return lambda *a, **k: {'status_code': 200, 'response': {'roles': [], 'groups': []}}


@pytest.fixture(autouse=True)
def fake_ots(monkeypatch):
    factory = lambda url, username, password: FakeOTS()  # noqa: E731
    monkeypatch.setattr(auth_module, 'OTSClient', factory)
    monkeypatch.setattr(users_module, 'OTSClient', factory)
    monkeypatch.setattr(ots_module, 'OTSClient', factory)  # function-level imports
    monkeypatch.setattr(ots_module, 'otsClient', FakeOTS())
    monkeypatch.setattr(events_module, 'otsClient', FakeOTS())
    monkeypatch.setattr(auth_module, 'send_html_email', lambda *a, **k: None)
    monkeypatch.setattr('app.email.send_html_email', lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _rollback_dirty_session(db):
    yield
    db.session.rollback()


def _user(prefix):
    username = _name(prefix)
    user = UserModel.create_user(username=username, email=f'{username}@example.com')
    _db.session.commit()
    return user


@pytest.fixture
def contact(db):
    return _user('obcontact')


@pytest.fixture
def admin(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    user = _user('obadmin')
    user.roles.append(role)
    db.session.commit()
    return user


def _headers(user, roles=('administrator',)):
    token = create_access_token(identity=str(user.id), additional_claims={'roles': list(roles)})
    return {'Authorization': f'Bearer {token}'}


def _code(contact, **attrs):
    code = OnboardingCodeModel(onboardingCode=_name('OBCODE'), name=_name('obcode'),
                               onboardContactId=contact.id, **attrs)
    _db.session.add(code)
    _db.session.commit()
    return code


def _pending(code, **attrs):
    username = _name('obpending')
    pending = PendingRegistrationModel.create_pending_registration(
        username=username, email=f'{username}@example.com', password=PASSWORD,
        first_name='O', last_name='B', callsign=username, onboarding_code_id=code.id,
        verification_token=_name('obtoken'), expires_at=datetime.now() + timedelta(hours=24))
    for key, value in attrs.items():
        setattr(pending, key, value)
    _db.session.commit()
    return pending


def _body():
    username = _name('obnew')
    return {'username': username, 'password': PASSWORD, 'email': f'{username}@example.com',
            'firstName': 'O', 'lastName': 'B', 'callsign': username}


def _assert_onboarded_by(username, contact):
    user = UserModel.get_user_by_username(username)
    assert user is not None
    assert isinstance(user.onboardedBy, int)
    assert user.onboardedBy == contact.id


def test_register_auto_approve(client, db, contact):
    code = _code(contact, autoApprove=True)
    body = _body()
    r = client.post(f'{PREFIX}/auth/register', json={**body, 'onboardingCode': code.onboardingCode})
    assert r.status_code in (200, 201), r.get_json()
    _assert_onboarded_by(body['username'], contact)


def test_verify_email(client, db, contact):
    pending = _pending(_code(contact))
    r = client.post(f'{PREFIX}/auth/verify-email', json={'token': pending.verification_token})
    assert r.status_code == 200, r.get_json()
    _assert_onboarded_by(pending.username, contact)


def test_approval_link(client, db, contact):
    pending = _pending(_code(contact), approval_status='pending_approval', approval_token=_name('obapprove'))
    username = pending.username
    r = client.get(f'{PREFIX}/registration/approve/{pending.approval_token}')
    assert r.status_code == 200, r.get_data(as_text=True)
    _assert_onboarded_by(username, contact)


def test_approvals_approve(client, db, contact, admin):
    approver_role = UserRoleModel.create_role(name=_name('obapprover'), description='Approver')
    admin.roles.append(approver_role)
    db.session.commit()
    code = _code(contact, requireApproval=True, approverRoleId=approver_role.id)
    pending = _pending(code, approval_status='pending_approval')
    username = pending.username
    r = client.post(f'{PREFIX}/approvals/{pending.id}/approve', headers=_headers(admin))
    assert r.status_code == 200, r.get_json()
    _assert_onboarded_by(username, contact)


def test_pending_registration_create_auto_approve(client, db, contact, admin):
    code = _code(contact, autoApprove=True)
    body = _body()
    r = client.post(f'{PREFIX}/pending-registrations', headers=_headers(admin),
                    json={**body, 'onboarding_code_id': code.id})
    assert r.status_code in (200, 201), r.get_json()
    _assert_onboarded_by(body['username'], contact)


def test_pending_registration_approve(client, db, contact, admin):
    pending = _pending(_code(contact))
    username = pending.username
    r = client.post(f'{PREFIX}/pending-registrations/{pending.id}/approve', headers=_headers(admin))
    assert r.status_code == 200, r.get_json()
    _assert_onboarded_by(username, contact)
