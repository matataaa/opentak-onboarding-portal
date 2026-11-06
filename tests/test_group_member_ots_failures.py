"""Group member add/remove: a change OTS refuses is reverted there and leaves the portal unchanged (502)."""
from unittest.mock import MagicMock, call

import pytest
from flask_jwt_extended import create_access_token

from app.models import GroupUserAssociation, OTSGroupModel, UserModel, UserRoleModel

API = '/api/v1'


class OtsHttpError(Exception):
    """Mirrors OTSClient.request_handler: Exception(response_dict)."""
    def __init__(self, status):
        super().__init__({'response': {}, 'status_code': status})


@pytest.fixture
def ots(monkeypatch):
    """The shared otsClient; mock.fail maps (action, direction) to the HTTP status the call fails with.

    mock.members(*rows) sets what OTS lists for the group: rows of (username, direction).
    """
    mock = MagicMock()
    mock.fail = {}
    mock.members = lambda *rows: setattr(mock.get_group_members, 'return_value', {
        'response': [{'username': u, 'direction': d, 'active': True} for u, d in rows], 'status_code': 200})
    mock.members()

    def fails(action):
        def handler(username, group, direction='BOTH'):
            if (action, direction) in mock.fail:
                raise OtsHttpError(mock.fail[(action, direction)])
        return handler
    mock.add_user_to_group.side_effect = fails('add')
    mock.remove_user_from_group.side_effect = fails('remove')
    monkeypatch.setattr('app.ots.otsClient', mock)
    return mock


@pytest.fixture
def admin(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    user = UserModel.create_user(username='gmfadmin', email='gmfadmin@example.com')
    user.roles.append(role)
    db.session.commit()
    token = create_access_token(identity=str(user.id), additional_claims={'roles': ['administrator']})
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def setup(db):
    """A group and a user, optionally with a local membership: setup(direction) -> (group, user)."""
    def make(direction=None):
        group = OTSGroupModel(name='gmf_group', active=True)
        user = UserModel.create_user(username='gmfuser', email='gmfuser@example.com')
        db.session.add(group)
        db.session.flush()
        if direction:
            db.session.add(GroupUserAssociation(group_id=group.id, user_id=user.id, direction=direction))
        db.session.commit()
        return group, user
    return make


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    GroupUserAssociation.query.delete()
    OTSGroupModel.query.filter(OTSGroupModel.name.like('gmf_%')).delete(synchronize_session=False)
    for u in UserModel.query.filter(UserModel.username.like('gmf%')).all():
        db.session.delete(u)
    db.session.commit()


def _row(group, user):
    GroupUserAssociation.query.session.expire_all()
    assoc = GroupUserAssociation.query.filter_by(group_id=group.id, user_id=user.id).first()
    return assoc.direction if assoc else None


def test_remove_refused_is_502_and_keeps_row(client, admin, ots, setup):
    group, user = setup('IN')
    ots.members(('gmfuser', 'IN'))
    ots.fail = {('remove', 'IN'): 500}
    r = client.delete(f'{API}/groups/{group.id}/members/gmfuser?direction=IN', headers=admin)
    assert r.status_code == 502
    body = r.get_json()
    assert body['failedGroups'] == ['gmf_group'] and body['otsMayDiffer'] is False
    assert _row(group, user) == 'IN'


def test_remove_404_counts_as_done(client, admin, ots, setup):
    group, user = setup('BOTH')
    ots.members(('gmfuser', 'IN'), ('gmfuser', 'OUT'))
    ots.fail = {('remove', 'IN'): 404, ('remove', 'OUT'): 404}
    r = client.delete(f'{API}/groups/{group.id}/members/gmfuser?direction=BOTH', headers=admin)
    assert r.status_code == 200, r.get_json()
    assert _row(group, user) is None


def test_remove_of_untracked_member_still_reaches_ots(client, admin, ots, setup):
    group, _ = setup()
    ots.members(('otsonly', 'OUT'))
    r = client.delete(f'{API}/groups/{group.id}/members/otsonly?direction=OUT', headers=admin)
    assert r.status_code == 200, r.get_json()
    ots.remove_user_from_group.assert_called_once_with('otsonly', 'gmf_group', 'OUT')


def test_both_add_with_refused_out_reverts_in(client, admin, ots, setup):
    group, user = setup()
    ots.fail = {('add', 'OUT'): 500}
    r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'userId': user.id, 'direction': 'BOTH'})
    assert r.status_code == 502
    assert r.get_json()['failedGroups'] == ['gmf_group']
    assert ots.add_user_to_group.call_args_list == [call('gmfuser', 'gmf_group', 'IN'), call('gmfuser', 'gmf_group', 'OUT')]
    # IN was added and is removed again; OUT may have taken effect despite the error, so it is removed too
    assert sorted(c.args[2] for c in ots.remove_user_from_group.call_args_list) == ['IN', 'OUT']
    assert _row(group, user) is None


def test_both_to_in_with_refused_out_remove_keeps_both(client, admin, ots, setup):
    group, user = setup('BOTH')
    ots.members(('gmfuser', 'IN'), ('gmfuser', 'OUT'))
    ots.fail = {('remove', 'OUT'): 500}
    r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'userId': user.id, 'direction': 'IN'})
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is False
    ots.add_user_to_group.assert_called_once_with('gmfuser', 'gmf_group', 'OUT')  # put back
    assert _row(group, user) == 'BOTH'


def test_failed_revert_sets_ots_may_differ(client, admin, ots, setup):
    group, user = setup('BOTH')
    ots.members(('gmfuser', 'IN'), ('gmfuser', 'OUT'))
    ots.fail = {('remove', 'OUT'): 500, ('add', 'OUT'): 500}
    r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'userId': user.id, 'direction': 'IN'})
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is True
    assert _row(group, user) == 'BOTH'


def test_direction_change_succeeds(client, admin, ots, setup):
    group, user = setup('IN')
    ots.members(('gmfuser', 'IN'))
    r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'userId': user.id, 'direction': 'OUT'})
    assert r.status_code == 200, r.get_json()
    ots.add_user_to_group.assert_called_once_with('gmfuser', 'gmf_group', 'OUT')
    ots.remove_user_from_group.assert_called_once_with('gmfuser', 'gmf_group', 'IN')
    assert _row(group, user) == 'OUT'


def test_remove_both_reverts_only_what_ots_had(client, admin, ots, setup):
    group, user = setup('OUT')
    ots.members(('gmfuser', 'OUT'))
    ots.fail = {('remove', 'OUT'): 500}
    r = client.delete(f'{API}/groups/{group.id}/members/gmfuser?direction=BOTH', headers=admin)
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is False
    # IN was never in OTS: it is neither removed nor re-added; only the failed OUT remove is put back
    ots.remove_user_from_group.assert_called_once_with('gmfuser', 'gmf_group', 'OUT')
    ots.add_user_to_group.assert_called_once_with('gmfuser', 'gmf_group', 'OUT')
    assert _row(group, user) == 'OUT'


def test_add_reverts_onto_untracked_ots_direction(client, admin, ots, setup):
    group, user = setup()  # no local row, but OTS has IN (matched case-insensitively)
    ots.members(('GmfUser', 'IN'), ('someoneelse', 'OUT'))
    ots.fail = {('remove', 'IN'): 500}
    r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'userId': user.id, 'direction': 'OUT'})
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is False
    # OUT added, IN remove failed: IN is put back (it was real) and the new OUT is removed again,
    # all under the name OTS knows the member by
    assert ots.add_user_to_group.call_args_list == [call('GmfUser', 'gmf_group', 'OUT'), call('GmfUser', 'gmf_group', 'IN')]
    assert ots.remove_user_from_group.call_args_list == [call('GmfUser', 'gmf_group', 'IN'),
                                                          call('GmfUser', 'gmf_group', 'OUT')]
    assert _row(group, user) is None


def test_add_keeps_untracked_ots_direction_it_asks_for(client, admin, ots, setup):
    group, user = setup()  # no local row, OTS has IN; BOTH only needs OUT added
    ots.members(('gmfuser', 'IN'))
    ots.fail = {('add', 'OUT'): 500}
    r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'userId': user.id, 'direction': 'BOTH'})
    assert r.status_code == 502
    # the revert removes only the OUT that may have been added, never the IN OTS already had
    ots.add_user_to_group.assert_called_once_with('gmfuser', 'gmf_group', 'OUT')
    ots.remove_user_from_group.assert_called_once_with('gmfuser', 'gmf_group', 'OUT')
    assert _row(group, user) is None


def test_remove_syncs_row_to_ots(client, admin, ots, setup):
    group, user = setup('BOTH')
    ots.members(('gmfuser', 'IN'))
    r = client.delete(f'{API}/groups/{group.id}/members/gmfuser?direction=IN', headers=admin)
    assert r.status_code == 200, r.get_json()
    ots.remove_user_from_group.assert_called_once_with('gmfuser', 'gmf_group', 'IN')
    assert _row(group, user) is None


@pytest.mark.parametrize('method', ['post', 'delete'])
def test_failed_member_read_changes_nothing(client, admin, ots, setup, method):
    group, user = setup('IN')
    ots.get_group_members.side_effect = OtsHttpError(500)
    if method == 'post':
        r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'userId': user.id, 'direction': 'OUT'})
    else:
        r = client.delete(f'{API}/groups/{group.id}/members/gmfuser?direction=IN', headers=admin)
    assert r.status_code == 502
    body = r.get_json()
    assert body['failedGroups'] == ['gmf_group'] and body['otsMayDiffer'] is False
    ots.add_user_to_group.assert_not_called()
    ots.remove_user_from_group.assert_not_called()
    assert _row(group, user) == 'IN'


def test_remove_uses_the_name_ots_has(client, admin, ots, setup):
    group, user = setup('BOTH')
    ots.members(('GmfUser', 'IN'), ('GmfUser', 'OUT'))
    r = client.delete(f'{API}/groups/{group.id}/members/gmfuser?direction=IN', headers=admin)
    assert r.status_code == 200, r.get_json()
    ots.remove_user_from_group.assert_called_once_with('GmfUser', 'gmf_group', 'IN')
    assert _row(group, user) == 'OUT'


def test_remove_by_ots_name_finds_local_row(client, admin, ots, setup):
    group, user = setup('IN')
    ots.members(('GmfUser', 'IN'))
    r = client.delete(f'{API}/groups/{group.id}/members/GmfUser?direction=IN', headers=admin)
    assert r.status_code == 200, r.get_json()
    ots.remove_user_from_group.assert_called_once_with('GmfUser', 'gmf_group', 'IN')
    assert _row(group, user) is None


def test_both_to_in_uses_the_name_ots_has(client, admin, ots, setup):
    group, user = setup('BOTH')
    ots.members(('GmfUser', 'IN'), ('GmfUser', 'OUT'))
    r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'userId': user.id, 'direction': 'IN'})
    assert r.status_code == 200, r.get_json()
    ots.remove_user_from_group.assert_called_once_with('GmfUser', 'gmf_group', 'OUT')
    ots.add_user_to_group.assert_not_called()
    assert _row(group, user) == 'IN'


@pytest.mark.parametrize('method', ['post', 'delete'])
def test_several_case_variants_in_ots_change_nothing(client, admin, ots, setup, method):
    group, user = setup('IN')
    ots.members(('gmfuser', 'IN'), ('GmfUser', 'OUT'))
    if method == 'post':
        r = client.post(f'{API}/groups/{group.id}/members', headers=admin, json={'username': 'GMFUSER', 'direction': 'OUT'})
    else:
        r = client.delete(f'{API}/groups/{group.id}/members/GMFUSER?direction=IN', headers=admin)
    assert r.status_code == 502
    body = r.get_json()
    assert body['failedGroups'] == ['gmf_group'] and body['otsMayDiffer'] is False
    ots.add_user_to_group.assert_not_called()
    ots.remove_user_from_group.assert_not_called()
    assert _row(group, user) == 'IN'


def test_exact_name_wins_over_case_variant(client, admin, ots, setup):
    group, user = setup('IN')
    ots.members(('gmfuser', 'IN'), ('GmfUser', 'OUT'))
    r = client.delete(f'{API}/groups/{group.id}/members/gmfuser?direction=IN', headers=admin)
    assert r.status_code == 200, r.get_json()
    ots.remove_user_from_group.assert_called_once_with('gmfuser', 'gmf_group', 'IN')
    assert _row(group, user) is None
