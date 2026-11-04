"""
A group change OTS does not accept fails the whole admin user create/update: everything already
done is put back (local rollback, OTS groups and role reverted, a new OTS account deleted) and
the admin gets a 502 naming the groups, with otsMayDiffer if putting OTS back failed too.
"""
from app.api_v1 import users as users_mod
from app.exceptions import BadRequestError
from app.models import GroupUserAssociation, UserModel, db as _db

from tests.test_rbac_escalation import (  # noqa: F401
    API, GOOD_PASSWORD, add_user, data, headers, ots, role, role_names, user_body)
from tests.test_rbac_escalation import add_group

OTS_ERROR = {'status_code': 500, 'response': {'error': 'SECRET-OTS-TEXT'}}
MAY_DIFFER = 'OpenTAK Server and the portal may differ: check this account in OpenTAK Server.'


def fail(ots, method, group, direction, status=500, times=1):
    ots.group_errors[(method, group, direction)] = [Exception({**OTS_ERROR, 'status_code': status})
                                                   for _ in range(times)]


def associate(user, group, direction):
    _db.session.add(GroupUserAssociation(group_id=group.id, user_id=user.id, direction=direction))
    _db.session.commit()


def associations(user):
    _db.session.expire_all()
    return sorted((a.group.name, a.direction) for a in GroupUserAssociation.query.filter_by(user_id=user.id))


def group_calls(ots):
    return [c for c in ots.calls if c[0] in ('add_user_to_group', 'remove_user_from_group')]


# ---------------------------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------------------------
def test_failing_second_add_reverts_the_first_and_changes_nothing(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    a, b, c = add_group('zz-a'), add_group('zz-b'), add_group('zz-c')
    associate(target, a, 'BOTH')
    ots.memberships = {('zztarget', 'zz-a', 'IN'), ('zztarget', 'zz-a', 'OUT')}
    fail(ots, 'add_user_to_group', 'zz-c', 'IN')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'callsign': 'CHANGED', 'groups': [
        {'id': a.id, 'direction': 'BOTH'}, {'id': b.id, 'direction': 'IN'}, {'id': c.id, 'direction': 'IN'}]})
    assert r.status_code == 502
    assert r.get_json() == {
        'error': 'Could not change these groups in OpenTAK Server: zz-c; nothing was changed',
        'failedGroups': ['zz-c'], 'otsMayDiffer': False, 'passwordChanged': False}
    assert group_calls(ots) == [
        ('add_user_to_group', 'zztarget', 'zz-b', 'IN'), ('add_user_to_group', 'zztarget', 'zz-c', 'IN'),
        ('remove_user_from_group', 'zztarget', 'zz-b', 'IN'), ('remove_user_from_group', 'zztarget', 'zz-c', 'IN')]
    assert ots.memberships == {('zztarget', 'zz-a', 'IN'), ('zztarget', 'zz-a', 'OUT')}
    assert associations(target) == [('zz-a', 'BOTH')]
    assert _db.session.get(UserModel, target.id).callsign == 'zztarget'


def test_role_change_with_failing_add_restores_the_role(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    fail(ots, 'add_user_to_group', 'zz-group', 'IN')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'roleIds': [role('administrator').id], 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is False
    assert ots.role_calls()[0] == ('set_user_roles', 'zztarget', ['administrator'])
    assert ots.role_calls()[-1] == ('set_user_roles', 'zztarget', ['user'])
    assert ots.accounts['zztarget'] == ['user']
    assert role_names(target) == []
    assert associations(target) == []


def test_failing_add_with_password_never_resets_the_password(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    fail(ots, 'add_user_to_group', 'zz-group', 'IN')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'password': GOOD_PASSWORD, 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 502
    assert r.get_json()['passwordChanged'] is False
    assert 'reset_user_password' not in [c[0] for c in ots.calls]
    assert associations(target) == []


def test_add_whose_revert_fails_says_ots_may_differ_and_logs(client, ots, caplog):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    b, c = add_group('zz-b'), add_group('zz-c')
    fail(ots, 'add_user_to_group', 'zz-c', 'IN')
    fail(ots, 'remove_user_from_group', 'zz-b', 'IN')
    with caplog.at_level('ERROR'):
        r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
            'groups': [{'id': b.id, 'direction': 'IN'}, {'id': c.id, 'direction': 'IN'}]})
    assert r.status_code == 502
    body = r.get_json()
    assert body['failedGroups'] == ['zz-c']
    assert body['otsMayDiffer'] is True
    assert body['error'] == 'Could not change these groups in OpenTAK Server: zz-c. ' + MAY_DIFFER
    assert 'SECRET-OTS-TEXT' not in r.get_data(as_text=True)
    assert any(rec.levelname == 'ERROR' and 'zz-b' in rec.getMessage() for rec in caplog.records)
    assert associations(target) == []


def test_failing_remove_reverts_the_add_and_re_adds_the_removed_group(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    a, b = add_group('zz-a'), add_group('zz-b')
    associate(target, a, 'IN')
    ots.memberships = {('zztarget', 'zz-a', 'IN')}
    fail(ots, 'remove_user_from_group', 'zz-a', 'IN')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'groups': [{'id': b.id, 'direction': 'IN'}]})
    assert r.status_code == 502
    assert r.get_json()['failedGroups'] == ['zz-a']
    assert r.get_json()['otsMayDiffer'] is False
    assert group_calls(ots) == [
        ('add_user_to_group', 'zztarget', 'zz-b', 'IN'), ('remove_user_from_group', 'zztarget', 'zz-a', 'IN'),
        ('add_user_to_group', 'zztarget', 'zz-a', 'IN'), ('remove_user_from_group', 'zztarget', 'zz-b', 'IN')]
    assert ots.memberships == {('zztarget', 'zz-a', 'IN')}
    assert associations(target) == [('zz-a', 'IN')]


def test_direction_change_whose_remove_fails_restores_the_old_direction(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    associate(target, group, 'IN')
    ots.memberships = {('zztarget', 'zz-group', 'IN')}
    fail(ots, 'remove_user_from_group', 'zz-group', 'IN')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'groups': [{'id': group.id, 'direction': 'OUT'}]})
    assert r.status_code == 502
    assert r.get_json()['failedGroups'] == ['zz-group']
    assert r.get_json()['otsMayDiffer'] is False
    assert group_calls(ots) == [
        ('add_user_to_group', 'zztarget', 'zz-group', 'OUT'), ('remove_user_from_group', 'zztarget', 'zz-group', 'IN'),
        ('add_user_to_group', 'zztarget', 'zz-group', 'IN'), ('remove_user_from_group', 'zztarget', 'zz-group', 'OUT')]
    assert ots.memberships == {('zztarget', 'zz-group', 'IN')}
    assert associations(target) == [('zz-group', 'IN')]


def test_password_reset_failure_whose_group_revert_fails_says_ots_may_differ(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    ots.reset_error = Exception('SECRET-OTS-TEXT')
    fail(ots, 'remove_user_from_group', 'zz-group', 'IN')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'password': GOOD_PASSWORD, 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 400
    assert r.get_json() == {'error': 'Failed to reset password', 'passwordChanged': False, 'otsMayDiffer': True}
    assert associations(target) == []


def test_remove_answering_404_counts_as_done(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    a = add_group('zz-a')
    associate(target, a, 'IN')
    fail(ots, 'remove_user_from_group', 'zz-a', 'IN', status=404)
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'groups': []})
    assert r.status_code == 200, r.get_json()
    assert group_calls(ots) == [('remove_user_from_group', 'zztarget', 'zz-a', 'IN')]
    assert associations(target) == []


def test_group_deleted_in_ots_is_not_reported_as_drift(client, ots):
    # Seen on OTS 1.7.13: adding to a deleted group answers 400, removing from it 404.
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    a, ghost = add_group('zz-a'), add_group('zz-ghost')
    fail(ots, 'add_user_to_group', 'zz-ghost', 'IN', status=400)
    fail(ots, 'remove_user_from_group', 'zz-ghost', 'IN', status=404)
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'groups': [
        {'id': a.id, 'direction': 'IN'}, {'id': ghost.id, 'direction': 'IN'}]})
    assert r.status_code == 502
    assert r.get_json()['failedGroups'] == ['zz-ghost']
    assert r.get_json()['otsMayDiffer'] is False
    assert ('remove_user_from_group', 'zztarget', 'zz-a', 'IN') in group_calls(ots)
    assert associations(target) == []


def test_password_reset_failure_after_group_push_reverts_groups_and_role(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    ots.reset_error = Exception('SECRET-OTS-TEXT')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'roleIds': [role('administrator').id], 'password': GOOD_PASSWORD, 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 400
    assert r.get_json() == {'error': 'Failed to reset password', 'passwordChanged': False}
    names = [c[0] for c in ots.calls]
    assert names.index('add_user_to_group') < names.index('reset_user_password') < names.index('remove_user_from_group')
    assert ots.role_calls()[-1] == ('set_user_roles', 'zztarget', ['user'])
    assert ots.memberships == set()
    assert role_names(target) == []
    assert associations(target) == []


def test_user_without_password_gets_groups_locally_only(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zzoidc', has_password=False)
    group = add_group()
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'groups': [{'id': group.id, 'direction': 'BOTH'}]})
    assert r.status_code == 200, r.get_json()
    assert ots.calls == []
    assert associations(target) == [('zz-group', 'BOTH')]


# ---------------------------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------------------------
def test_create_pushes_groups_and_stores_them(client, ots):
    actor = add_user('zzadmin', 'administrator')
    group = add_group()
    r = client.post(f'{API}/users', headers=headers(actor), json={
        **user_body('zznew'), 'roleIds': [role('zznormal').id], 'groups': [{'id': group.id, 'direction': 'BOTH'}]})
    assert r.status_code == 201, r.get_json()
    assert ots.memberships == {('zznew', 'zz-group', 'IN'), ('zznew', 'zz-group', 'OUT')}
    user = UserModel.get_user_by_username('zznew')
    assert associations(user) == [('zz-group', 'BOTH')]
    assert role_names(user) == ['zznormal']


def test_create_with_failing_group_add_deletes_the_ots_account(client, ots):
    actor = add_user('zzadmin', 'administrator')
    group = add_group()
    fail(ots, 'add_user_to_group', 'zz-group', 'OUT')
    r = client.post(f'{API}/users', headers=headers(actor), json={
        **user_body('zznew'), 'groups': [{'id': group.id, 'direction': 'BOTH'}]})
    assert r.status_code == 502
    assert r.get_json() == {
        'error': 'Could not change these groups in OpenTAK Server: zz-group; the user was not created',
        'failedGroups': ['zz-group'], 'otsMayDiffer': False}
    assert ots.calls[0][0] == 'create_user'
    assert ots.calls[-1] == ('delete_user', 'zznew')
    assert UserModel.get_user_by_username('zznew') is None


def test_create_where_the_delete_also_fails_says_ots_may_differ(client, ots, monkeypatch, caplog):
    actor = add_user('zzadmin', 'administrator')
    group = add_group()
    fail(ots, 'add_user_to_group', 'zz-group', 'IN')

    def refuse(username):
        ots.calls.append(('delete_user', username))
        raise Exception(OTS_ERROR)
    monkeypatch.setattr(ots, 'delete_user', refuse)
    with caplog.at_level('ERROR'):
        r = client.post(f'{API}/users', headers=headers(actor), json={
            **user_body('zznew'), 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is True
    assert r.get_json()['error'].endswith(MAY_DIFFER)
    assert ('delete_user', 'zznew') in ots.calls
    assert any(rec.levelname == 'ERROR' and 'zznew' in rec.getMessage() for rec in caplog.records)
    assert UserModel.get_user_by_username('zznew') is None


def test_create_with_bad_expiry_date_never_reaches_ots(client, ots):
    actor = add_user('zzadmin', 'administrator')
    r = client.post(f'{API}/users', headers=headers(actor), json={**user_body('zznew'), 'expiryDate': 'not a date'})
    assert r.status_code == 400
    assert r.get_json() == {'error': 'Invalid expiryDate'}
    assert ots.calls == []
    assert UserModel.get_user_by_username('zznew') is None


def test_create_where_the_local_commit_fails_deletes_the_ots_account(client, ots, monkeypatch):
    actor = add_user('zzadmin', 'administrator')
    h = headers(actor)

    def failing_commit():
        raise RuntimeError('SECRET-SQL-TEXT')
    with monkeypatch.context() as m:
        m.setattr(users_mod.db.session, 'commit', failing_commit)
        r = client.post(f'{API}/users', headers=h, json=user_body('zznew'))
    assert r.status_code == 400
    assert r.get_json() == {'error': 'Failed to create user'}
    assert 'SECRET-SQL-TEXT' not in r.get_data(as_text=True)
    assert ots.calls[-1] == ('delete_user', 'zznew')
    assert UserModel.get_user_by_username('zznew') is None


def test_create_where_storing_groups_fails_removes_the_committed_user(client, ots, monkeypatch):
    actor = add_user('zzadmin', 'administrator')
    group = add_group()
    h = headers(actor)
    original = users_mod.db.session.commit
    commits = []

    def second_commit_fails():
        commits.append(1)
        if len(commits) == 2:
            raise RuntimeError('SECRET-SQL-TEXT')
        return original()
    with monkeypatch.context() as m:
        m.setattr(users_mod.db.session, 'commit', second_commit_fails)
        r = client.post(f'{API}/users', headers=h, json={
            **user_body('zznew'), 'roleIds': [role('zznormal').id], 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 400
    assert r.get_json() == {'error': 'Failed to create user'}
    assert ots.calls[-1] == ('delete_user', 'zznew')
    _db.session.expire_all()
    assert UserModel.get_user_by_username('zznew') is None
    assert GroupUserAssociation.query.filter_by(group_id=group.id).count() == 0


def test_create_refused_by_ots_does_not_leak_ots_text(client, ots, monkeypatch):
    actor = add_user('zzadmin', 'administrator')

    def refuse(username, password, roles=('user',)):
        ots.calls.append(('create_user', username, list(roles)))
        raise BadRequestError({'status_code': 400, 'response': {'error': 'SECRET-OTS-TEXT'}})
    monkeypatch.setattr(ots, 'create_user', refuse)
    r = client.post(f'{API}/users', headers=headers(actor), json=user_body('zznew'))
    assert r.status_code == 400
    assert r.get_json() == {
        'error': 'OpenTAK Server refused the new user (for example because the username already exists there)'}
    assert 'SECRET-OTS-TEXT' not in r.get_data(as_text=True)
    assert [c[0] for c in ots.calls] == ['create_user']
    assert UserModel.get_user_by_username('zznew') is None


def test_create_with_unknown_ots_outcome_says_ots_may_differ_and_deletes_nothing(client, ots, monkeypatch):
    actor = add_user('zzadmin', 'administrator')

    def lost(username, password, roles=('user',)):
        ots.calls.append(('create_user', username, list(roles)))
        raise ConnectionError('SECRET-OTS-TEXT')
    monkeypatch.setattr(ots, 'create_user', lost)
    r = client.post(f'{API}/users', headers=headers(actor), json=user_body('zznew'))
    assert r.status_code == 502
    assert r.get_json() == {'error': 'Could not create the user in OpenTAK Server', 'otsMayDiffer': True}
    assert 'SECRET-OTS-TEXT' not in r.get_data(as_text=True)
    assert [c[0] for c in ots.calls] == ['create_user']
    assert UserModel.get_user_by_username('zznew') is None
