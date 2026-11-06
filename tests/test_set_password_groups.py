"""set-password creates the OTS account in the groups the portal already assigned, or not at all."""
import datetime
from unittest.mock import MagicMock, call

import pytest
from flask_jwt_extended import create_access_token

from app.api_v1 import auth as auth_module
from app.models import GroupUserAssociation, OTSGroupModel, UserModel

URL = '/api/v1/auth/set-password'
PASSWORD = {'newPassword': 'longenough'}


@pytest.fixture
def ots(monkeypatch):
    """OTS mock; add_user_to_group fails for the (group, direction) pairs in mock.refuse."""
    mock = MagicMock()
    mock.refuse = set()

    def add(username, group, direction='IN'):
        if (group, direction) in mock.refuse:
            raise Exception({'response': {}, 'status_code': 500})
    mock.add_user_to_group.side_effect = add
    monkeypatch.setattr(auth_module, 'OTSClient', lambda *a, **k: mock)
    return mock


@pytest.fixture
def oidc_user(db):
    """A user without a password yet, with local memberships: oidc_user({name: direction}, missing=())."""
    def make(groups, missing=()):
        user = UserModel.create_user(username='spguser', email='spguser@example.com')
        user.has_password = False
        for name, direction in groups.items():
            group = OTSGroupModel(name=name, active=True)
            if name in missing:
                group.ots_missing_since = datetime.datetime(2026, 1, 1)
            db.session.add(group)
            db.session.flush()
            db.session.add(GroupUserAssociation(group_id=group.id, user_id=user.id, direction=direction))
        db.session.commit()
        return user, {'Authorization': f'Bearer {create_access_token(identity=str(user.id))}'}
    return make


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    GroupUserAssociation.query.delete()
    OTSGroupModel.query.filter(OTSGroupModel.name.like('spg_%')).delete(synchronize_session=False)
    UserModel.query.filter_by(username='spguser').delete()
    db.session.commit()


def _has_password(user):
    UserModel.query.session.expire_all()
    return UserModel.get_user_by_id(user.id).has_password


def test_groups_are_pushed_with_their_directions(client, ots, oidc_user):
    user, headers = oidc_user({'spg_both': 'BOTH', 'spg_out': 'OUT'})
    r = client.post(URL, headers=headers, json=PASSWORD)
    assert r.status_code == 200, r.get_json()
    assert sorted(ots.add_user_to_group.call_args_list) == sorted([
        call('spguser', 'spg_both', 'IN'), call('spguser', 'spg_both', 'OUT'), call('spguser', 'spg_out', 'OUT')])
    assert _has_password(user) is True


def test_group_missing_in_ots_is_skipped(client, ots, oidc_user):
    user, headers = oidc_user({'spg_gone': 'IN', 'spg_ok': 'IN'}, missing={'spg_gone'})
    r = client.post(URL, headers=headers, json=PASSWORD)
    assert r.status_code == 200, r.get_json()
    ots.add_user_to_group.assert_called_once_with('spguser', 'spg_ok', 'IN')
    assert _has_password(user) is True


def test_refused_group_deletes_ots_account(client, ots, oidc_user):
    user, headers = oidc_user({'spg_a': 'IN', 'spg_b': 'IN'})
    ots.refuse = {('spg_b', 'IN')}
    r = client.post(URL, headers=headers, json=PASSWORD)
    assert r.status_code == 502
    body = r.get_json()
    assert body['failedGroups'] == ['spg_b'] and body['otsMayDiffer'] is False
    assert body['code'] == 'ots_groups_refused'
    ots.delete_user.assert_called_once_with('spguser')
    assert _has_password(user) is False


def test_refused_group_and_failed_delete_sets_ots_may_differ(client, ots, oidc_user):
    user, headers = oidc_user({'spg_a': 'OUT'})
    ots.refuse = {('spg_a', 'OUT')}
    ots.delete_user.side_effect = Exception({'response': {}, 'status_code': 500})
    r = client.post(URL, headers=headers, json=PASSWORD)
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is True
    assert _has_password(user) is False
