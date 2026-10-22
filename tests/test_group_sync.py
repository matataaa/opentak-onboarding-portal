"""OTS group sync / local removal helpers (SQLite runs with foreign_keys=ON)."""
import datetime

import pytest

from app.models import (EventTeamModel, GroupOnboardingCodeAssociation,
                        GroupUserAssociation, OnboardingCodeModel, OTSGroupModel,
                        UserModel)


def test_missing_in_ots_property(db):
    group = OTSGroupModel(name='sync_prop_group')
    assert group.missing_in_ots is False
    group.ots_missing_since = datetime.datetime.utcnow()
    assert group.missing_in_ots is True


def test_remove_local_leaves_no_orphans(db):
    user = UserModel.create_user(username='rl_user', email='rl_user@example.com')
    code = OnboardingCodeModel(onboardingCode='RLCODE')
    group = OTSGroupModel(name='rl_group')
    team = EventTeamModel(name='rl_team')
    db.session.add_all([code, group, team])
    db.session.flush()
    db.session.add(GroupUserAssociation(group_id=group.id, user_id=user.id))
    db.session.add(GroupOnboardingCodeAssociation(group_id=group.id, onboardingcode_id=code.id))
    team.otsGroupId = group.id
    db.session.commit()
    gid, uid, cid, tid = group.id, user.id, code.id, team.id

    OTSGroupModel.remove_local(group)
    db.session.commit()
    db.session.expire_all()

    assert OTSGroupModel.query.get(gid) is None
    assert GroupUserAssociation.query.filter_by(group_id=gid).count() == 0
    assert GroupOnboardingCodeAssociation.query.filter_by(group_id=gid).count() == 0
    assert EventTeamModel.query.get(tid).otsGroupId is None
    # unrelated rows survive
    assert UserModel.query.get(uid) is not None
    assert OnboardingCodeModel.query.get(cid) is not None

    db.session.delete(EventTeamModel.query.get(tid))
    db.session.delete(OnboardingCodeModel.query.get(cid))
    db.session.delete(UserModel.query.get(uid))
    db.session.commit()


# --- sync / missing-in-OTS handling -------------------------------------------------

from unittest.mock import MagicMock

from flask_jwt_extended import create_access_token

from app.exceptions import BadRequestError
from app.models import EventModel, EventSessionModel, UserRoleModel

API = '/api/v1'


class OtsHttpError(Exception):
    """Mirrors OTSClient.request_handler: Exception(response_dict)."""
    def __init__(self, status):
        super().__init__({'response': {}, 'status_code': status})


def _page(names, total=None, extra=None):
    results = [{'name': n} for n in names]
    body = {'results': results, 'total': len(results) if total is None else total,
            'total_pages': 1, 'current_page': 1, 'per_page': 100}
    body.update(extra or {})
    return {'response': body, 'status_code': 200}


@pytest.fixture
def ots(monkeypatch):
    """Replace the shared otsClient (and the per-use OTSClient in users.py) with a mock."""
    mock = MagicMock()
    mock.get_all_pages.side_effect = lambda fetch, **kw: mock.groups_response
    mock.groups_response = _page([])
    monkeypatch.setattr('app.ots.otsClient', mock)
    monkeypatch.setattr('app.api_v1.events.otsClient', mock)
    monkeypatch.setattr('app.api_v1.users.OTSClient', lambda *a, **k: mock)
    return mock


def _role_headers(db, username, role_name):
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
def admin(db):
    return _role_headers(db, 'gsadmin', 'administrator')


@pytest.fixture
def readonly(db):
    return _role_headers(db, 'gsreadonly', 'group_readonly')


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    db.session.rollback()
    for model in (GroupUserAssociation, GroupOnboardingCodeAssociation):
        model.query.delete()
    EventSessionModel.query.delete()
    EventTeamModel.query.filter(EventTeamModel.name.like('gs_%')).delete(synchronize_session=False)
    EventModel.query.filter(EventModel.name.like('gs_%')).delete(synchronize_session=False)
    OTSGroupModel.query.delete()
    OnboardingCodeModel.query.filter(OnboardingCodeModel.onboardingCode.like('GSC%')).delete(
        synchronize_session=False)
    for u in UserModel.query.filter(UserModel.username.like('gsu%')).all():
        db.session.delete(u)
    db.session.commit()


def _group(db, name, missing=False, active=True):
    g = OTSGroupModel(name=name, active=active)
    if missing:
        g.ots_missing_since = datetime.datetime(2026, 1, 1)
    db.session.add(g)
    db.session.commit()
    return g


def _sync(client, headers):
    return client.post(f'{API}/groups/sync', headers=headers)


def test_sync_flags_missing_and_keeps_first_date(client, db, admin, ots):
    keep, gone = _group(db, 'gs_keep'), _group(db, 'gs_gone')
    ots.groups_response = _page(['gs_keep'])
    body = _sync(client, admin).get_json()
    assert body['markedMissing'] == 1 and body['markedMissingGroups'] == ['gs_gone']
    assert body['missingCheckSkipped'] is None and body['missingTotal'] == 1
    db.session.expire_all()
    first = OTSGroupModel.get_by_name('gs_gone').ots_missing_since
    assert first is not None and OTSGroupModel.get_by_name('gs_keep').ots_missing_since is None

    body = _sync(client, admin).get_json()
    assert body['markedMissing'] == 0 and body['missingTotal'] == 1
    db.session.expire_all()
    assert OTSGroupModel.get_by_name('gs_gone').ots_missing_since == first  # nothing deleted, date kept


def test_sync_restores_reappearing_group(client, db, admin, ots):
    _group(db, 'gs_back', missing=True)
    ots.groups_response = _page(['gs_back'])
    body = _sync(client, admin).get_json()
    assert body['restored'] == 1 and body['restoredGroups'] == ['gs_back']
    db.session.expire_all()
    assert OTSGroupModel.get_by_name('gs_back').ots_missing_since is None


def test_sync_keeps_admin_active_false_and_creates_new_active(client, db, admin, ots):
    _group(db, 'gs_off', active=False)
    ots.groups_response = _page(['gs_off', 'gs_new'])
    body = _sync(client, admin).get_json()
    assert body['created'] == 1 and body['updated'] == 1
    db.session.expire_all()
    assert OTSGroupModel.get_by_name('gs_off').active is False
    assert OTSGroupModel.get_by_name('gs_new').active is True


def test_sync_anon_is_a_normal_group(client, db, admin, ots):
    _group(db, '__ANON__')
    _group(db, 'gs_other')
    ots.groups_response = _page(['__ANON__'])
    body = _sync(client, admin).get_json()
    assert body['markedMissingGroups'] == ['gs_other']
    assert OTSGroupModel.get_by_name('__ANON__').ots_missing_since is None


def test_sync_duplicate_names_skip_check_but_upsert_once(client, db, admin, ots):
    _group(db, 'gs_flagged', missing=True)
    ots.groups_response = _page(['gs_dup', 'gs_dup', 'gs_x'])
    body = _sync(client, admin).get_json()
    assert body['missingCheckSkipped'] == 'invalid'
    assert OTSGroupModel.query.filter_by(name='gs_dup').count() == 1
    db.session.expire_all()
    assert OTSGroupModel.get_by_name('gs_flagged').ots_missing_since is not None


@pytest.mark.parametrize('response,reason', [
    (_page([]), 'empty'),
    (_page(['gs_a'], total=5), 'incomplete'),
    ({'response': ['gs_a'], 'status_code': 200}, 'malformed'),
    ({'response': {'results': [{'name': 'x'}]}, 'status_code': 200}, 'malformed'),   # no total
    ({'response': {'total': 1}, 'status_code': 200}, 'malformed'),                   # no results
    ({'response': {'results': [{'name': 'x'}], 'total': True}, 'status_code': 200}, 'malformed'),  # bool total
    ({'response': {'results': [1, 2, 3], 'total': 3}, 'status_code': 200}, 'malformed'),  # non-dict entries
    (_page(['gs_a', 'gs_a', 'gs_b']), 'invalid'),  # duplicate names
    (_page([''], total=1), 'invalid'),             # empty name
    ({'response': {'results': [{'name': 5}], 'total': 1}, 'status_code': 200}, 'invalid'),  # non-str name
])
def test_sync_guards_leave_flags_unchanged(client, db, admin, ots, response, reason):
    _group(db, 'gs_present')
    _group(db, 'gs_flagged', missing=True)
    ots.groups_response = response
    r = _sync(client, admin)
    assert r.status_code == 200
    body = r.get_json()
    assert body['missingCheckSkipped'] == reason
    assert body['markedMissing'] == 0 and body['restored'] == 0
    db.session.expire_all()
    assert OTSGroupModel.get_by_name('gs_present').ots_missing_since is None
    assert OTSGroupModel.get_by_name('gs_flagged').ots_missing_since is not None


@pytest.mark.parametrize('exc,status', [
    (ConnectionError('down'), 503), (OtsHttpError(500), 400), (BadRequestError({'status_code': 400}), 400)])
def test_sync_ots_failure_changes_nothing(client, db, admin, ots, exc, status):
    _group(db, 'gs_present')
    ots.get_all_pages.side_effect = exc
    assert _sync(client, admin).status_code == status
    db.session.expire_all()
    assert OTSGroupModel.get_by_name('gs_present').ots_missing_since is None


# --- RBAC -------------------------------------------------------------------------------

def test_readonly_can_see_flag_but_not_sync_or_delete(client, db, readonly, ots):
    g = _group(db, 'gs_ro', missing=True)
    assert _sync(client, readonly).status_code == 403
    assert client.delete(f'{API}/groups/{g.id}', headers=readonly).status_code == 403
    ots.delete_group.assert_not_called()
    listed = client.get(f'{API}/groups', headers=readonly).get_json()['groups']
    row = [x for x in listed if x['name'] == 'gs_ro'][0]
    assert row['missingInOts'] is True and row['missingSince'].startswith('2026-01-01')
    one = client.get(f'{API}/groups/{g.id}', headers=readonly).get_json()
    assert one['missingInOts'] is True and one['eventTeams'] == []


# --- DELETE / force removal ------------------------------------------------------------

def _wire(db, group):
    user = UserModel.create_user(username='gsu_del', email='gsu_del@example.com')
    code = OnboardingCodeModel(onboardingCode='GSC_DEL', name='Del code')
    team = EventTeamModel(name='gs_team')
    db.session.add_all([code, team])
    db.session.flush()
    db.session.add(GroupUserAssociation(group_id=group.id, user_id=user.id))
    db.session.add(GroupOnboardingCodeAssociation(group_id=group.id, onboardingcode_id=code.id))
    team.otsGroupId = group.id
    db.session.commit()
    return user, code, team


def test_delete_flagged_group_skips_ots_and_cascades(client, db, admin, ots):
    g = _group(db, 'gs_flagdel', missing=True)
    user, code, team = _wire(db, g)
    gid, tid, cid = g.id, team.id, code.id
    r = client.delete(f'{API}/groups/{gid}', headers=admin)
    assert r.status_code == 200
    body = r.get_json()
    assert body['removedFromOts'] is False
    assert body['affected'] == {'memberCount': 1, 'onboardingCodes': [{'id': cid, 'name': 'Del code'}],
                                'eventTeams': [{'id': tid, 'name': 'gs_team'}]}
    ots.delete_group.assert_not_called()
    db.session.expire_all()
    assert OTSGroupModel.query.get(gid) is None
    assert GroupUserAssociation.query.filter_by(group_id=gid).count() == 0
    assert GroupOnboardingCodeAssociation.query.filter_by(group_id=gid).count() == 0
    assert EventTeamModel.query.get(tid).otsGroupId is None


def test_delete_unflagged_group_removes_in_ots_then_locally(client, db, admin, ots):
    g = _group(db, 'gs_live')
    r = client.delete(f'{API}/groups/{g.id}', headers=admin)
    assert r.status_code == 200 and r.get_json()['removedFromOts'] is True
    ots.delete_group.assert_called_once_with('gs_live')
    assert OTSGroupModel.get_by_name('gs_live') is None


def test_delete_unflagged_group_ots_404_still_removes(client, db, admin, ots):
    g = _group(db, 'gs_gone404')
    ots.delete_group.side_effect = OtsHttpError(404)
    r = client.delete(f'{API}/groups/{g.id}', headers=admin)
    assert r.status_code == 200 and r.get_json()['removedFromOts'] is False
    assert OTSGroupModel.get_by_name('gs_gone404') is None


@pytest.mark.parametrize('exc', [OtsHttpError(500), ConnectionError('down'), BadRequestError({'status_code': 400})])
def test_delete_unflagged_group_ots_failure_keeps_row(client, db, admin, ots, exc):
    g = _group(db, 'gs_keepme')
    ots.delete_group.side_effect = exc
    r = client.delete(f'{API}/groups/{g.id}', headers=admin)
    assert r.status_code == 502 and 'error' in r.get_json()
    db.session.expire_all()
    assert OTSGroupModel.get_by_name('gs_keepme') is not None


# --- create_group restore ---------------------------------------------------------------

def test_create_group_on_flagged_name_restores(client, db, admin, ots):
    g = _group(db, 'gs_restore', missing=True)
    r = client.post(f'{API}/groups', json={'name': 'gs_restore'}, headers=admin)
    assert r.status_code == 200
    ots.create_group.assert_called_once()
    db.session.expire_all()
    assert OTSGroupModel.query.get(g.id).ots_missing_since is None


def test_create_group_on_unflagged_name_is_409(client, db, admin, ots):
    _group(db, 'gs_exists')
    assert client.post(f'{API}/groups', json={'name': 'gs_exists'}, headers=admin).status_code == 409
    ots.create_group.assert_not_called()


# --- assignment paths -------------------------------------------------------------------

def test_assign_ots_groups_skips_flagged(db, app, ots):
    from app.api_v1.auth import assign_ots_groups
    flagged, ok = _group(db, 'gs_fl', missing=True), _group(db, 'gs_ok')
    code = OnboardingCodeModel(onboardingCode='GSC_ASSIGN', name='assign')
    user = UserModel.create_user(username='gsu_assign', email='gsu_assign@example.com')
    db.session.add(code)
    db.session.flush()
    for g in (flagged, ok):
        db.session.add(GroupOnboardingCodeAssociation(group_id=g.id, onboardingcode_id=code.id, direction='IN'))
    db.session.commit()
    assign_ots_groups('gsu_assign', code, user)
    assert [c.args[1] for c in ots.add_user_to_group.call_args_list] == ['gs_ok']
    assert [a.group_id for a in GroupUserAssociation.query.filter_by(user_id=user.id)] == [ok.id]


def test_login_clears_flag_when_ots_reports_group(client, db, monkeypatch):
    g = _group(db, 'gs_login', missing=True)
    UserModel.create_user(username='gsu_login', email='gsu_login@example.com')
    fake = MagicMock()
    fake.get_me.return_value = {'response': {'groups': [{'name': 'gs_login'}], 'roles': [], 'email': 'x@example.com'}}
    monkeypatch.setattr('app.api_v1.auth.OTSClient', lambda *a, **k: fake)
    r = client.post(f'{API}/auth/login', json={'username': 'gsu_login', 'password': 'pw'})
    assert r.status_code == 200, r.get_json()
    db.session.expire_all()
    assert OTSGroupModel.query.get(g.id).ots_missing_since is None


# --- flagged groups are rejected for NEW assignment -------------------------------------

def test_users_create_rejects_flagged_group(client, db, admin, ots):
    g = _group(db, 'gs_ufl', missing=True)
    r = client.post(f'{API}/users', headers=admin, json={
        'username': 'gsunew', 'password': 'Passw0rd!x', 'email': 'gsunew@example.com',
        'firstName': 'A', 'lastName': 'B', 'callsign': 'CS', 'groupIds': [g.id]})
    assert r.status_code == 400 and 'gs_ufl' in r.get_json()['error']
    ots.create_user.assert_not_called()
    assert UserModel.get_user_by_username('gsunew') is None


def test_users_update_rejects_new_flagged_but_keeps_existing(client, db, admin, ots):
    flagged, other, ok = _group(db, 'gs_ufl2', missing=True), _group(db, 'gs_ufl3', missing=True), _group(db, 'gs_uok')
    user = UserModel.create_user(username='gsu_upd', email='gsu_upd@example.com')
    db.session.add(GroupUserAssociation(group_id=flagged.id, user_id=user.id, direction='BOTH'))
    db.session.commit()
    url = f'{API}/users/{user.id}'
    # newly adding a flagged group: rejected
    r = client.put(url, headers=admin, json={'groups': [{'id': flagged.id}, {'id': other.id}]})
    assert r.status_code == 400
    # keeping the already associated flagged group while adding a healthy one: ok
    r = client.put(url, headers=admin, json={'groups': [{'id': flagged.id}, {'id': ok.id}]})
    assert r.status_code == 200, r.get_json()
    db.session.expire_all()
    assert {a.group_id for a in GroupUserAssociation.query.filter_by(user_id=user.id)} == {flagged.id, ok.id}


def test_onboarding_codes_reject_new_flagged_group(client, db, admin, ots):
    flagged, ok = _group(db, 'gs_cfl', missing=True), _group(db, 'gs_cok')
    r = client.post(f'{API}/onboarding-codes', headers=admin, json={
        'onboardingCode': 'GSC_NEW', 'name': 'n', 'groups': [{'id': flagged.id}]})
    assert r.status_code == 400
    assert OnboardingCodeModel.query.filter_by(onboardingCode='GSC_NEW').count() == 0

    r = client.post(f'{API}/onboarding-codes', headers=admin, json={
        'onboardingCode': 'GSC_OK', 'name': 'n', 'groups': [{'id': ok.id}]})
    assert r.status_code == 201, r.get_json()
    code = OnboardingCodeModel.query.filter_by(onboardingCode='GSC_OK').first()
    url = f'{API}/onboarding-codes/{code.id}'
    assert client.put(url, headers=admin, json={'groups': [{'id': ok.id}, {'id': flagged.id}]}).status_code == 400
    # existing flagged association is kept on update
    db.session.add(GroupOnboardingCodeAssociation(group_id=flagged.id, onboardingcode_id=code.id))
    db.session.commit()
    r = client.put(url, headers=admin, json={'groups': [{'id': ok.id}, {'id': flagged.id}]})
    assert r.status_code == 200, r.get_json()
    assert GroupOnboardingCodeAssociation.query.filter_by(onboardingcode_id=code.id).count() == 2


def test_event_team_rejects_new_flagged_group(client, db, admin, ots):
    flagged = _group(db, 'gs_efl', missing=True)
    r = client.post(f'{API}/events/teams', headers=admin, json={'name': 'gs_t1', 'otsGroupId': flagged.id})
    assert r.status_code == 400
    r = client.post(f'{API}/events/teams', headers=admin, json={'name': 'gs_t2'})
    assert r.status_code == 201
    tid = r.get_json()['id']
    assert client.put(f'{API}/events/teams/{tid}', headers=admin,
                      json={'otsGroupId': flagged.id}).status_code == 400
    # already assigned flagged group may stay
    EventTeamModel.query.get(tid).otsGroupId = flagged.id
    db.session.commit()
    r = client.put(f'{API}/events/teams/{tid}', headers=admin, json={'otsGroupId': flagged.id, 'color': 'Red'})
    assert r.status_code == 200 and r.get_json()['otsGroup']['missingInOts'] is True


def test_add_member_to_flagged_group_is_409(client, db, admin, ots):
    g = _group(db, 'gs_mfl', missing=True)
    r = client.post(f'{API}/groups/{g.id}/members', headers=admin, json={'username': 'someone'})
    assert r.status_code == 409
    ots.add_user_to_group.assert_not_called()


def test_event_join_skips_flagged_team_group(db, app, ots):
    from app.api_v1.events import _ensure_shared_player_account
    g = _group(db, 'gs_jfl', missing=True)
    team = EventTeamModel(name='gs_jteam', otsGroupId=g.id)
    event = EventModel(name='gs_jevent')
    db.session.add_all([team, event])
    db.session.commit()
    username, created = _ensure_shared_player_account(event, team)
    assert created
    ots.create_user.assert_called_once()
    ots.add_user_to_group.assert_not_called()


def test_delete_local_commit_failure_rolls_back(client, db, admin, ots, monkeypatch):
    g = _group(db, 'gs_rollback', missing=True)
    user, code, team = _wire(db, g)
    gid, tid = g.id, team.id

    def boom():
        raise RuntimeError('commit failed')

    monkeypatch.setattr(db.session, 'commit', boom)
    r = client.delete(f'{API}/groups/{gid}', headers=admin)
    assert r.status_code == 400
    monkeypatch.undo()
    db.session.expire_all()
    assert OTSGroupModel.query.get(gid) is not None
    assert GroupUserAssociation.query.filter_by(group_id=gid).count() == 1
    assert EventTeamModel.query.get(tid).otsGroupId == gid


@pytest.mark.parametrize('name', ['a<b', 'a>b', 'a&b', 'a"b', "a'b"])
def test_create_group_rejects_names_ots_would_rewrite(client, db, admin, ots, name):
    r = client.post(f'{API}/groups', json={'name': name}, headers=admin)
    assert r.status_code == 400
    ots.create_group.assert_not_called()


def test_string_group_id_cannot_bypass_flag_check(client, db, admin, ots):
    g = _group(db, 'gs_strid', missing=True)
    r = client.post(f'{API}/users', headers=admin, json={
        'username': 'gsustr', 'password': 'Passw0rd!x', 'email': 'gsustr@example.com',
        'firstName': 'A', 'lastName': 'B', 'callsign': 'CS', 'groupIds': [str(g.id)]})
    assert r.status_code == 400
    r = client.post(f'{API}/onboarding-codes', headers=admin, json={
        'onboardingCode': 'GSC_STR', 'name': 'n', 'groups': [{'id': str(g.id)}]})
    assert r.status_code == 400


def test_users_update_flagged_group_direction_change_and_uncheck_make_no_ots_call(client, db, admin, ots):
    flagged = _group(db, 'gs_udir', missing=True)
    user = UserModel.create_user(username='gsu_dir', email='gsu_dir@example.com')
    db.session.add(GroupUserAssociation(group_id=flagged.id, user_id=user.id, direction='BOTH'))
    db.session.commit()
    url = f'{API}/users/{user.id}'
    r = client.put(url, headers=admin, json={'groups': [{'id': flagged.id, 'direction': 'IN'}]})
    assert r.status_code == 200
    db.session.expire_all()
    assert GroupUserAssociation.query.filter_by(user_id=user.id).one().direction == 'IN'
    r = client.put(url, headers=admin, json={'groups': []})
    assert r.status_code == 200
    assert GroupUserAssociation.query.filter_by(user_id=user.id).count() == 0
    ots.add_user_to_group.assert_not_called()
    ots.remove_user_from_group.assert_not_called()
