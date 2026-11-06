"""assign_ots_groups only stores the memberships OTS accepted and reports the groups it refused."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from flask_jwt_extended import create_access_token

from app.api_v1.auth import assign_ots_groups
from app.models import (GroupOnboardingCodeAssociation, GroupUserAssociation, OnboardingCodeModel,
                        OTSGroupModel, PendingRegistrationModel, UserModel, UserRoleModel)

API = '/api/v1'


@pytest.fixture
def ots(monkeypatch):
    """Shared and per-use OTS clients as one mock; add_user_to_group fails for the pairs in mock.refuse."""
    mock = MagicMock()
    mock.refuse = set()

    def add(username, group, direction='BOTH'):
        if (group, direction) in mock.refuse:
            raise Exception({'response': {}, 'status_code': 500})
    mock.add_user_to_group.side_effect = add
    monkeypatch.setattr('app.ots.otsClient', mock)
    monkeypatch.setattr('app.ots.OTSClient', lambda *a, **k: mock)
    monkeypatch.setattr('app.api_v1.auth.OTSClient', lambda *a, **k: mock)
    monkeypatch.setattr('app.email.send_html_email', lambda *a, **k: None)
    monkeypatch.setattr('app.api_v1.auth.send_html_email', lambda *a, **k: None)
    monkeypatch.setattr('app.notifications.notify_admin_new_registration', lambda *a, **k: None)
    return mock


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    GroupUserAssociation.query.delete()
    GroupOnboardingCodeAssociation.query.delete()
    PendingRegistrationModel.query.filter(PendingRegistrationModel.username.like('agf%')).delete(
        synchronize_session=False)
    OTSGroupModel.query.filter(OTSGroupModel.name.like('agf_%')).delete(synchronize_session=False)
    OnboardingCodeModel.query.filter(OnboardingCodeModel.onboardingCode.like('AGF%')).delete(
        synchronize_session=False)
    for u in UserModel.query.filter(UserModel.username.like('agf%')).all():
        db.session.delete(u)
    UserRoleModel.query.filter(UserRoleModel.name.like('agf%')).delete(synchronize_session=False)
    db.session.commit()


def _code(db, *groups, **attrs):
    """An onboarding code with (name, direction) groups; returns (code, {name: group})."""
    code = OnboardingCodeModel(onboardingCode='AGFCODE', name='agf code', uses=0, **attrs)
    db.session.add(code)
    db.session.flush()
    made = {}
    for name, direction in groups:
        made[name] = OTSGroupModel(name=name, active=True)
        db.session.add(made[name])
        db.session.flush()
        db.session.add(GroupOnboardingCodeAssociation(group_id=made[name].id, onboardingcode_id=code.id,
                                                      direction=direction))
    db.session.commit()
    return code, made


def _user(db, name='agfuser'):
    user = UserModel.create_user(username=name, email=f'{name}@example.com')
    db.session.commit()
    return user


def _rows(user):
    return {a.group.name: a.direction for a in GroupUserAssociation.query.filter_by(user_id=user.id)}


def test_failed_group_gets_no_local_row(db, ots):
    code, _ = _code(db, ('agf_a', 'IN'), ('agf_b', 'IN'))
    user = _user(db)
    ots.refuse = {('agf_a', 'IN')}
    assert assign_ots_groups('agfuser', code, user) == ['agf_a']
    db.session.commit()
    assert _rows(user) == {'agf_b': 'IN'}


def test_both_with_failed_out_stores_in(db, ots):
    code, _ = _code(db, ('agf_a', 'BOTH'))
    user = _user(db)
    ots.refuse = {('agf_a', 'OUT')}
    assert assign_ots_groups('agfuser', code, user) == ['agf_a']
    db.session.commit()
    assert _rows(user) == {'agf_a': 'IN'}


def test_existing_in_and_failed_out_stays_in(db, ots):
    code, groups = _code(db, ('agf_a', 'OUT'))
    user = _user(db)
    db.session.add(GroupUserAssociation(group_id=groups['agf_a'].id, user_id=user.id, direction='IN'))
    db.session.commit()
    ots.refuse = {('agf_a', 'OUT')}
    assert assign_ots_groups('agfuser', code, user) == ['agf_a']
    db.session.commit()
    assert _rows(user) == {'agf_a': 'IN'}


def test_all_failed_stores_nothing(db, ots):
    code, _ = _code(db, ('agf_a', 'BOTH'), ('agf_b', 'OUT'))
    user = _user(db)
    ots.refuse = {('agf_a', 'IN'), ('agf_a', 'OUT'), ('agf_b', 'OUT')}
    assert sorted(assign_ots_groups('agfuser', code, user)) == ['agf_a', 'agf_b']
    db.session.commit()
    assert _rows(user) == {}


def test_all_accepted_returns_no_failures(db, ots):
    code, _ = _code(db, ('agf_a', 'BOTH'))
    user = _user(db)
    assert assign_ots_groups('agfuser', code, user) == []
    db.session.commit()
    assert _rows(user) == {'agf_a': 'BOTH'}


def _admin_headers(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    admin = _user(db, 'agfadmin')
    admin.roles.append(role)
    db.session.commit()
    token = create_access_token(identity=str(admin.id), additional_claims={'roles': ['administrator']})
    return admin, {'Authorization': f'Bearer {token}'}


def _pending(db, code, **attrs):
    pending = PendingRegistrationModel.create_pending_registration(
        username='agfnew', email='agfnew@example.com', password='abcd!efgh', first_name='A', last_name='G',
        callsign='agfnew', onboarding_code_id=code.id, verification_token='agf-verify',
        expires_at=datetime.now() + timedelta(hours=24))
    for key, value in attrs.items():
        setattr(pending, key, value)
    db.session.commit()
    return pending


@pytest.mark.parametrize('refuse, expected', [({('agf_a', 'IN')}, ['agf_a']), (set(), None)])
def test_pending_registration_approve_reports_failed_groups(client, db, ots, refuse, expected):
    _, headers = _admin_headers(db)
    code, _ = _code(db, ('agf_a', 'IN'), ('agf_b', 'IN'))
    pending = _pending(db, code)
    ots.refuse = refuse
    r = client.post(f'{API}/pending-registrations/{pending.id}/approve', headers=headers)
    assert r.status_code == 200, r.get_json()
    assert r.get_json().get('failedGroups') == expected
    user = UserModel.get_user_by_username('agfnew')
    assert set(_rows(user)) == {'agf_a', 'agf_b'} - {name for name, _ in refuse}


def test_approvals_approve_reports_failed_groups(client, db, ots):
    admin, headers = _admin_headers(db)
    approver = UserRoleModel.create_role(name='agfapprover', description='Approver')
    admin.roles.append(approver)
    db.session.commit()
    code, _ = _code(db, ('agf_a', 'BOTH'), requireApproval=True, approverRoleId=approver.id)
    pending = _pending(db, code, approval_status='pending_approval')
    ots.refuse = {('agf_a', 'OUT')}
    r = client.post(f'{API}/approvals/{pending.id}/approve', headers=headers)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['failedGroups'] == ['agf_a']
    assert _rows(UserModel.get_user_by_username('agfnew')) == {'agf_a': 'IN'}


def test_approval_link_shows_failed_groups(client, db, ots):
    code, _ = _code(db, ('agf_a', 'IN'))
    pending = _pending(db, code, approval_status='pending_approval', approval_token='agf-approve')
    ots.refuse = {('agf_a', 'IN')}
    r = client.get(f'{API}/registration/approve/agf-approve')
    assert r.status_code == 200, r.get_data(as_text=True)
    page = r.get_data(as_text=True)
    assert 'OpenTAK Server refused these groups' in page and 'agf_a' in page
    assert _rows(UserModel.get_user_by_username('agfnew')) == {}


def test_register_with_failed_group_still_succeeds(client, db, ots):
    code, _ = _code(db, ('agf_a', 'IN'), autoApprove=True)
    ots.refuse = {('agf_a', 'IN')}
    r = client.post(f'{API}/auth/register', json={
        'username': 'agfreg', 'password': 'abcd!efgh', 'email': 'agfreg@example.com', 'firstName': 'A',
        'lastName': 'G', 'callsign': 'agfreg', 'onboardingCode': code.onboardingCode})
    assert r.status_code in (200, 201), r.get_json()
    assert 'failedGroups' not in r.get_json()
    assert _rows(UserModel.get_user_by_username('agfreg')) == {}
