"""
Privilege escalation paths: only an administrator hands out or takes away full administrator
rights, roles come from the database (not the token), the portal's own OTS account and the last
administrator stay intact, and the administrator role is changed in OTS before it is changed locally.
"""
import io
from datetime import datetime, timedelta

import pytest
import requests_mock
from flask_jwt_extended import create_access_token

from app import jobs
from app.api_v1 import auth as auth_mod, events as events_mod, oidc as oidc_mod, users as users_mod
from app.exceptions import BadRequestError, OTSRoleUnconfirmed
from app.extensions import scheduler
from app.models import (EventLinkModel, EventModel, EventTeamModel, GroupUserAssociation, OIDCProviderModel,
                        OnboardingCodeModel, OneTimeTokenModel, OTSGroupModel, PendingRegistrationModel,
                        SystemSettingsModel, TakProfileModel, UserModel, UserRoleModel, db as _db)
from app.ots import OTSClient, set_ots_admin
from app.rbac import has_privileged_role, is_privileged_role, is_reserved_role_name, is_service_account

API = '/api/v1'
PRIVILEGED_NAMES = ['administrator', 'admin', 'Admin', ' ADMINISTRATOR ']
GOOD_PASSWORD = 'Str0ngpassword'
current_ots = None  # the FakeOTS of the running test


class FakeOTS:
    """Stand-in for OTSClient: records every write in order, can be made to fail, and has accounts.

    Accounts are found by exact name, like OTS. set_user_roles copies the OTS 1.7.13 loop that
    removes roles from the list it iterates, so an account holding two roles can keep one.
    """

    def __init__(self):
        self.calls = []
        self.accounts = {}  # exact username -> role names, in the order OTS loads them
        self.role_errors = []  # raised by consecutive set_user_roles calls
        self.reset_error = None
        self.read_back_error = None  # raised when reading one account back
        self.role_order = None  # roles reload in this order instead of as they were added
        self.ignore_role_changes = False
        self.group_errors = {}  # (method, group, direction) -> exceptions raised by consecutive such calls
        self.memberships = set()  # (username, group, direction) held in OTS

    def __call__(self, *args):
        return self

    def set_user_roles(self, username, roles):
        self.calls.append(('set_user_roles', username, roles))
        if self.role_errors:
            raise self.role_errors.pop(0)
        if username not in self.accounts:
            raise BadRequestError({'response': {'error': f'User {username} does not exist'}, 'status_code': 400})
        if self.ignore_role_changes:
            return
        held = self.accounts[username]
        for held_role in held:
            held.remove(held_role)
        held.extend(r for r in roles if r not in held)
        if self.role_order:
            held.sort(key=self.role_order.index)

    def get_users(self, page=None, per_page=None, username=None, email=None):
        if username and self.read_back_error:
            raise self.read_back_error
        return {'response': {'total_pages': 1, 'results': [
            {'username': name, 'roles': [{'name': r} for r in held]}
            for name, held in self.accounts.items() if username in (None, name)]}}

    get_all_pages = OTSClient.get_all_pages

    def reset_user_password(self, username, password):
        self.calls.append(('reset_user_password', username))
        if self.reset_error:
            raise self.reset_error

    def create_user(self, username, password, roles=('user',)):
        self.calls.append(('create_user', username, list(roles)))
        self.accounts[username] = list(roles)

    def delete_user(self, username):
        self.calls.append(('delete_user', username))

    def _group_call(self, method, username, group, direction):
        self.calls.append((method, username, group, direction))
        errors = self.group_errors.get((method, group, direction))
        if errors:
            raise errors.pop(0)

    def add_user_to_group(self, username, group, direction=None):
        self._group_call('add_user_to_group', username, group, direction)
        self.memberships.add((username, group, direction))

    def remove_user_from_group(self, username, group, direction=None):
        self._group_call('remove_user_from_group', username, group, direction)
        self.memberships.discard((username, group, direction))

    def login(self):
        self.calls.append(('login',))

    def role_calls(self):
        return [c for c in self.calls if c[0] == 'set_user_roles']


def primary_keys(model):
    return {_db.inspect(o).identity for o in model.query.all()}


@pytest.fixture(autouse=True)
def data(app):
    """Removes only what the test created and restores who holds the administrator role."""
    models = (EventLinkModel, EventModel, EventTeamModel, PendingRegistrationModel, OneTimeTokenModel,
              GroupUserAssociation, OIDCProviderModel, OnboardingCodeModel, TakProfileModel, OTSGroupModel,
              UserModel, UserRoleModel)
    with app.app_context():
        before = {m: primary_keys(m) for m in models}
        role = UserRoleModel.query.filter_by(name='administrator').first()
        holders = [u.id for u in role.users] if role else []
        yield _db
        _db.session.rollback()
        for _ in range(3):
            for m in models:
                for key in primary_keys(m) - before[m]:
                    try:
                        _db.session.delete(_db.session.get(m, key))
                        _db.session.commit()
                    except Exception:
                        _db.session.rollback()
        role = UserRoleModel.query.filter_by(name='administrator').first()
        if role:
            role.users = [_db.session.get(UserModel, i) for i in holders if _db.session.get(UserModel, i)]
            _db.session.commit()


@pytest.fixture(autouse=True)
def ots(monkeypatch, app):
    global current_ots
    fake = current_ots = FakeOTS()
    monkeypatch.setattr(users_mod, 'OTSClient', fake)
    monkeypatch.setattr(auth_mod, 'OTSClient', fake)
    monkeypatch.setattr('app.ots.OTSClient', fake)  # approve routes import it locally
    monkeypatch.setattr(auth_mod, 'assign_ots_groups', lambda *a, **k: None)
    monkeypatch.setattr('app.email.send_html_email', lambda *a, **k: True)
    monkeypatch.setattr('app.notifications.notify_admin_new_registration', lambda *a, **k: None, raising=False)
    monkeypatch.setattr('app.notifications.notify_admin_pending_registration', lambda *a, **k: None, raising=False)
    monkeypatch.setattr(jobs, 'otsClient', fake)
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    return fake


@pytest.fixture
def service_name(app, monkeypatch):
    """The configured OTS account, in another case than the local username."""
    monkeypatch.setitem(app.config, 'OTS_USERNAME', 'SvcAccount')
    return 'svcaccount'


def role(name):
    found = UserRoleModel.query.filter_by(name=name).first()
    return found or UserRoleModel.create_role(name=name, description='test role')


def add_user(name, *role_names, **fields):
    """A local user and, unless it has no password yet, its OTS account with the matching role."""
    user = UserModel(username=name, email=f'{name}@example.com', firstName='F', lastName='L', callsign=name,
                     roles=[role(r) for r in role_names], **fields)
    _db.session.add(user)
    _db.session.commit()
    if fields.get('has_password') is not False:
        current_ots.accounts[name] = ['administrator' if has_privileged_role(user) else 'user']
    return user


def headers(user, **claims):
    return {'Authorization': f'Bearer {create_access_token(identity=str(user.id), additional_claims=claims)}'}


def add_code(value, *role_names, **fields):
    code = OnboardingCodeModel(onboardingCode=value, name=f'name of {value}', uses=0, roles=[role(r) for r in role_names], **fields)
    _db.session.add(code)
    _db.session.commit()
    return code


def add_pending(name, code, **fields):
    pending = PendingRegistrationModel(
        username=name, email=f'{name}@example.com', password='Pendingpass1', firstName='F', lastName='L', callsign=name,
        onboarding_code_id=code.id, verification_token=f'verify-{name}', expires_at=datetime.now() + timedelta(days=1),
        **fields)
    _db.session.add(pending)
    _db.session.commit()
    return pending


def role_names(user):
    _db.session.expire_all()
    return sorted(r.name for r in user.roles)


def user_body(name):
    return {'username': name, 'password': GOOD_PASSWORD, 'email': f'{name}@example.com',
            'firstName': 'F', 'lastName': 'L', 'callsign': name}


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('name', PRIVILEGED_NAMES)
def test_privileged_names_ignore_case_and_padding(name):
    assert is_privileged_role(UserRoleModel(name=name))
    assert is_reserved_role_name(name)


@pytest.mark.parametrize('name', ['user', ' USER', 'Event_Leader', 'ROLE_ADMIN ', 'events_readonly'])
def test_reserved_names_are_not_all_privileged(name):
    assert is_reserved_role_name(name)
    assert not is_privileged_role(UserRoleModel(name=name))


# ---------------------------------------------------------------------------------------------
# A. Onboarding codes
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('name', PRIVILEGED_NAMES)
def test_code_create_with_privileged_role_is_refused(client, name):
    actor = add_user('zzonb', 'onboarding_admin')
    r = client.post(f'{API}/onboarding-codes', headers=headers(actor),
                    json={'name': 'c', 'onboardingCode': 'zz-new', 'roleIds': [role(name).id]})
    assert r.status_code == 403
    assert OnboardingCodeModel.get_onboarding_code_by_code('zz-new') is None


def test_code_create_with_roleids_that_cannot_be_vetted_is_refused(client):
    actor = add_user('zzonb', 'onboarding_admin')
    r = client.post(f'{API}/onboarding-codes', headers=headers(actor),
                    json={'name': 'c', 'onboardingCode': 'zz-new', 'roleIds': str(role('administrator').id)})
    assert r.status_code == 403


def test_code_create_with_normal_role_works(client):
    actor = add_user('zzonb', 'onboarding_admin')
    r = client.post(f'{API}/onboarding-codes', headers=headers(actor),
                    json={'name': 'c', 'onboardingCode': 'zz-new', 'roleIds': [role('zznormal').id]})
    assert r.status_code == 201
    assert [x.name for x in OnboardingCodeModel.get_onboarding_code_by_code('zz-new').roles] == ['zznormal']


def test_code_update_attaching_privileged_role_is_refused(client):
    actor = add_user('zzonb', 'onboarding_admin')
    code = add_code('zz-normal', 'zznormal')
    r = client.put(f'{API}/onboarding-codes/{code.id}', headers=headers(actor),
                   json={'name': 'renamed', 'roleIds': [role('zznormal').id, role('administrator').id]})
    assert r.status_code == 403
    _db.session.expire_all()
    assert code.name == 'name of zz-normal'
    assert [x.name for x in code.roles] == ['zznormal']


def test_administrator_can_attach_privileged_role_to_code(client):
    actor = add_user('zzadmin', 'administrator')
    code = add_code('zz-normal')
    r = client.put(f'{API}/onboarding-codes/{code.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]})
    assert r.status_code == 200
    assert [x.name for x in code.roles] == ['administrator']


@pytest.mark.parametrize('role_name', ['onboarding_admin', 'onboarding_readonly'])
def test_privileged_code_is_invisible_to_non_administrators(client, role_name):
    actor = add_user('zzonb', role_name)
    normal = add_code('zz-normal', 'zznormal')
    secret = add_code('zz-secret', 'Admin')
    h = headers(actor)

    body = client.get(f'{API}/onboarding-codes', headers=h, query_string={'search': 'zz-'}).get_json()
    assert [c['id'] for c in body['codes']] == [normal.id]
    assert body['total'] == 1
    assert client.get(f'{API}/onboarding-codes/{secret.id}', headers=h).status_code == 404
    assert client.get(f'{API}/onboarding-codes/{normal.id}', headers=h).status_code == 200


def test_privileged_code_is_read_only_for_onboarding_admin(client):
    actor = add_user('zzonb', 'onboarding_admin')
    secret = add_code('zz-secret', 'administrator')
    h = headers(actor)
    assert client.put(f'{API}/onboarding-codes/{secret.id}', headers=h, json={'name': 'x', 'roleIds': []}).status_code == 404
    assert client.delete(f'{API}/onboarding-codes/{secret.id}', headers=h).status_code == 404
    _db.session.expire_all()
    assert secret.name == 'name of zz-secret'
    assert [x.name for x in secret.roles] == ['administrator']


def test_administrator_sees_and_edits_privileged_code(client):
    actor = add_user('zzadmin', 'administrator')
    secret = add_code('zz-secret', 'administrator')
    h = headers(actor)
    body = client.get(f'{API}/onboarding-codes', headers=h, query_string={'search': 'zz-secret'}).get_json()
    assert [c['id'] for c in body['codes']] == [secret.id]
    assert client.get(f'{API}/onboarding-codes/{secret.id}', headers=h).get_json()['onboardingCode'] == 'zz-secret'
    assert client.put(f'{API}/onboarding-codes/{secret.id}', headers=h, json={'name': 'x'}).status_code == 200
    assert client.delete(f'{API}/onboarding-codes/{secret.id}', headers=h).status_code == 200


# ---------------------------------------------------------------------------------------------
# B. Creating users
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('name', PRIVILEGED_NAMES)
def test_create_user_with_privileged_role_is_refused_before_ots(client, ots, name):
    actor = add_user('zzuadm', 'user_admin')
    r = client.post(f'{API}/users', headers=headers(actor), json={**user_body('zznew'), 'roleIds': [role(name).id]})
    assert r.status_code == 403
    assert ots.calls == []
    assert UserModel.get_user_by_username('zznew') is None


def test_create_user_with_normal_role_works_for_user_admin(client, ots):
    actor = add_user('zzuadm', 'user_admin')
    r = client.post(f'{API}/users', headers=headers(actor), json={**user_body('zznew'), 'roleIds': [role('zznormal').id]})
    assert r.status_code == 201
    assert ots.calls == [('create_user', 'zznew', ['user'])]


@pytest.mark.parametrize('name', PRIVILEGED_NAMES)
def test_administrator_creates_privileged_user_in_ots_as_administrator(client, ots, name):
    actor = add_user('zzadmin', 'administrator')
    r = client.post(f'{API}/users', headers=headers(actor), json={**user_body('zznew'), 'roleIds': [role(name).id]})
    assert r.status_code == 201
    assert ots.calls == [('create_user', 'zznew', ['administrator'])]


# ---------------------------------------------------------------------------------------------
# C. Role names and reading roles
# ---------------------------------------------------------------------------------------------
RESERVED_VARIANTS = ['administrator', 'Administrator', ' admin ', 'ADMIN', 'user', 'User ', 'event_leader',
                     'Event_Leader', 'user_admin', ' SETTINGS_READONLY']


@pytest.mark.parametrize('name', RESERVED_VARIANTS)
def test_create_role_with_reserved_name_is_refused(client, name):
    actor = add_user('zzradm', 'role_admin')
    before = UserRoleModel.query.count()
    r = client.post(f'{API}/roles', headers=headers(actor), json={'name': name})
    assert r.status_code == 400
    assert UserRoleModel.query.count() == before


@pytest.mark.parametrize('name', RESERVED_VARIANTS)
def test_rename_role_to_reserved_name_is_refused(client, name):
    actor = add_user('zzradm', 'role_admin')
    target = role('zznormal')
    r = client.put(f'{API}/roles/{target.id}', headers=headers(actor), json={'name': name})
    assert r.status_code == 400
    _db.session.expire_all()
    assert target.name == 'zznormal'


def test_administrator_cannot_create_reserved_name_either(client):
    actor = add_user('zzadmin', 'administrator')
    assert client.post(f'{API}/roles', headers=headers(actor), json={'name': 'Admin'}).status_code == 400


def test_existing_row_with_reserved_name_keeps_working(client):
    """A legacy row named like a reserved role can still be saved under its own name."""
    actor = add_user('zzadmin', 'administrator')
    legacy = role(' UsEr')
    r = client.put(f'{API}/roles/{legacy.id}', headers=headers(actor), json={'name': ' UsEr', 'description': 'new'})
    assert r.status_code == 200
    assert legacy.description == 'new'


def test_ordinary_role_rename_and_create_still_work(client):
    actor = add_user('zzradm', 'role_admin')
    target = role('zznormal')
    assert client.put(f'{API}/roles/{target.id}', headers=headers(actor), json={'name': 'zznormal2'}).status_code == 200
    assert client.post(f'{API}/roles', headers=headers(actor), json={'name': 'zzadministrators'}).status_code == 201


def test_role_admin_cannot_change_or_delete_a_privileged_row(client):
    """Renaming or deleting an "admin" row would strip the role from whoever holds it."""
    actor = add_user('zzradm', 'role_admin')
    row = role('Admin')
    h = headers(actor)
    assert client.put(f'{API}/roles/{row.id}', headers=h, json={'name': 'zzfree'}).status_code == 403
    assert client.delete(f'{API}/roles/{row.id}', headers=h).status_code == 403
    _db.session.expire_all()
    assert row.name == 'Admin'


def test_get_roles_needs_an_admin_role(client):
    plain = add_user('zzplain', 'zznormal')
    assert client.get(f'{API}/roles', headers=headers(plain)).status_code == 403
    assert client.get(f'{API}/roles').status_code == 401


def test_get_roles_refuses_event_leader_session(client):
    user = add_user('zzleader', 'administrator')
    assert client.get(f'{API}/roles', headers=headers(user, event_leader_team=1)).status_code == 403


@pytest.mark.parametrize('role_name', ['tak_profile_readonly', 'settings_admin', 'announcement_admin', 'user_admin',
                                       'onboarding_admin', 'meshtastic_admin'])
def test_get_roles_as_picker_has_no_user_count(client, role_name):
    actor = add_user('zzpicker', role_name)
    r = client.get(f'{API}/roles', headers=headers(actor))
    assert r.status_code == 200
    assert r.get_json()['roles']
    assert all('userCount' not in row for row in r.get_json()['roles'])


@pytest.mark.parametrize('role_name', ['administrator', 'role_admin', 'role_readonly'])
def test_get_roles_has_user_count_with_roles_module_access(client, role_name):
    actor = add_user('zzreader', role_name)
    row = next(x for x in client.get(f'{API}/roles', headers=headers(actor)).get_json()['roles'] if x['name'] == role_name)
    assert row['userCount'] >= 1


def test_get_role_by_id_needs_view_role(client):
    target = role('zznormal')
    assert client.get(f'{API}/roles/{target.id}', headers=headers(add_user('zzother', 'tak_profile_admin'))).status_code == 403
    assert client.get(f'{API}/roles/{target.id}', headers=headers(add_user('zzreader', 'role_readonly'))).status_code == 200
    assert client.get(f'{API}/roles/{target.id}', headers=headers(add_user('zzradm', 'role_admin'))).status_code == 200


# ---------------------------------------------------------------------------------------------
# D. Updating users
# ---------------------------------------------------------------------------------------------
def test_user_admin_normal_edit_succeeds_without_ots_role_call(client, ots):
    actor = add_user('zzuadm', 'user_admin')
    target = add_user('zztarget', 'zznormal')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'email': 'zzchanged@example.com', 'expiryDate': '2099-01-01', 'password': GOOD_PASSWORD,
        'roleIds': [role('zznormal').id]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['passwordChanged'] is True
    _db.session.expire_all()
    assert target.email == 'zzchanged@example.com'
    assert target.expiryDate.year == 2099
    assert role_names(target) == ['zznormal']
    assert ots.role_calls() == []


def test_full_list_save_with_unchanged_normal_roles_may_change_normal_roles(client, ots):
    actor = add_user('zzuadm', 'user_admin')
    target = add_user('zztarget', 'zznormal', 'zzsecond')
    full_list = [role('zznormal').id, role('zzthird').id]
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': full_list})
    assert r.status_code == 200
    assert role_names(target) == ['zznormal', 'zzthird']
    assert ots.role_calls() == []


@pytest.mark.parametrize('name', PRIVILEGED_NAMES)
def test_user_admin_cannot_add_privileged_role(client, ots, name):
    actor = add_user('zzuadm', 'user_admin')
    target = add_user('zztarget', 'zznormal')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'email': 'zzchanged@example.com', 'roleIds': [role('zznormal').id, role(name).id]})
    assert r.status_code == 403
    _db.session.expire_all()
    assert target.email == 'zztarget@example.com'
    assert role_names(target) == ['zznormal']
    assert ots.calls == []


def test_user_admin_cannot_remove_own_privileged_role_but_can_save_it_unchanged(client, ots):
    actor = add_user('zzuadm', 'user_admin', 'admin')
    h = headers(actor)
    both = [role('user_admin').id, role('admin').id]
    assert client.put(f'{API}/users/{actor.id}', headers=h, json={'roleIds': [role('user_admin').id]}).status_code == 403
    assert role_names(actor) == ['admin', 'user_admin']
    assert client.put(f'{API}/users/{actor.id}', headers=h, json={'callsign': 'ZZ', 'roleIds': both}).status_code == 200
    assert ots.role_calls() == []


def test_user_admin_cannot_make_self_administrator(client, ots):
    actor = add_user('zzuadm', 'user_admin')
    r = client.put(f'{API}/users/{actor.id}', headers=headers(actor),
                   json={'roleIds': [role('user_admin').id, role('administrator').id]})
    assert r.status_code == 403
    assert role_names(actor) == ['user_admin']


def test_ordinary_user_editing_self_cannot_change_roles_or_admin_fields(client, ots):
    user = add_user('zzplain', 'zznormal')
    r = client.put(f'{API}/users/{user.id}', headers=headers(user), json={
        'callsign': 'NEW', 'roleIds': [role('administrator').id], 'expiryDate': '2099-01-01', 'password': GOOD_PASSWORD})
    assert r.status_code == 200
    assert r.get_json()['passwordChanged'] is False
    _db.session.expire_all()
    assert user.callsign == 'NEW'
    assert user.expiryDate is None
    assert role_names(user) == ['zznormal']
    assert ots.calls == []


def test_ordinary_user_cannot_edit_or_read_another_user(client):
    user = add_user('zzplain')
    other = add_user('zzother')
    assert client.put(f'{API}/users/{other.id}', headers=headers(user), json={'callsign': 'X'}).status_code == 403
    assert client.get(f'{API}/users/{other.id}', headers=headers(user)).status_code == 403
    assert client.get(f'{API}/users/{user.id}', headers=headers(user)).status_code == 200


@pytest.mark.parametrize('name', PRIVILEGED_NAMES)
@pytest.mark.parametrize('body', [{'email': 'zzhack@example.com'}, {'password': GOOD_PASSWORD}, {'expiryDate': '2000-01-01'},
                                  {'callsign': 'X'}, {'groups': []}, {'roleIds': []}])
def test_target_guard_refuses_any_field_on_privileged_user(client, ots, name, body):
    actor = add_user('zzuadm', 'user_admin')
    target = add_user('zztarget', name)
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json=body)
    assert r.status_code == 403
    _db.session.expire_all()
    assert target.email == 'zztarget@example.com'
    assert target.expiryDate is None
    assert ots.calls == []


@pytest.mark.parametrize('name', PRIVILEGED_NAMES)
def test_target_guard_refuses_delete_of_privileged_user(client, ots, name):
    actor = add_user('zzuadm', 'user_admin')
    target = add_user('zztarget', name)
    r = client.delete(f'{API}/users/{target.id}', headers=headers(actor))
    assert r.status_code == 403
    assert UserModel.get_user_by_username('zztarget') is not None
    assert ots.calls == []


def test_user_admin_can_delete_normal_user(client, ots):
    actor = add_user('zzuadm', 'user_admin')
    target = add_user('zztarget', 'zznormal')
    assert client.delete(f'{API}/users/{target.id}', headers=headers(actor)).status_code == 200
    assert ots.calls == [('delete_user', 'zztarget')]


def test_demoted_admin_with_old_token_loses_access_immediately(client, ots):
    """The token still says administrator; the database no longer does."""
    demoted = add_user('zzdemoted')
    other = add_user('zzother')
    stale = headers(demoted, roles=['administrator'])
    assert client.put(f'{API}/users/{other.id}', headers=stale, json={'callsign': 'X'}).status_code == 403
    assert client.get(f'{API}/users/{other.id}', headers=stale).status_code == 403
    r = client.put(f'{API}/users/{demoted.id}', headers=stale, json={'roleIds': [role('administrator').id]})
    assert r.status_code == 200
    assert role_names(demoted) == []
    assert ots.calls == []


def test_user_admin_role_counts_without_a_claim_and_a_claim_alone_does_not(client):
    actor = add_user('zzuadm', 'user_admin')
    other = add_user('zzother')
    assert client.put(f'{API}/users/{other.id}', headers=headers(actor), json={'callsign': 'X'}).status_code == 200
    assert client.put(f'{API}/users/{other.id}', headers=headers(add_user('zzclaim'), roles=['user_admin']),
                      json={'callsign': 'Y'}).status_code == 403


def test_tak_profiles_use_database_roles_not_claims(client):
    private = TakProfileModel(name='zz-private', description='d', isPublic=False)
    _db.session.add(private)
    _db.session.commit()
    stale = headers(add_user('zzdemoted'), roles=['administrator'])
    real = headers(add_user('zzadmin', 'administrator'))
    search = {'search': 'zz-private'}

    assert [p['id'] for p in client.get(f'{API}/tak-profiles', headers=real, query_string=search).get_json()['profiles']] == [private.id]
    assert client.get(f'{API}/tak-profiles', headers=stale, query_string=search).get_json()['profiles'] == []
    assert client.get(f'{API}/tak-profiles/{private.id}', headers=real).status_code == 200
    assert client.get(f'{API}/tak-profiles/{private.id}', headers=stale).status_code == 403
    assert client.post(f'{API}/tak-profiles/{private.id}/download-token', headers=real).status_code != 403
    assert client.post(f'{API}/tak-profiles/{private.id}/download-token', headers=stale).status_code == 403


# ---------------------------------------------------------------------------------------------
# E. Service account
# ---------------------------------------------------------------------------------------------
def test_service_account_cannot_be_deleted(client, ots, service_name):
    add_user('zzadmin', 'administrator')
    actor = add_user('zzadmin2', 'administrator')
    target = add_user(service_name, 'administrator')
    r = client.delete(f'{API}/users/{target.id}', headers=headers(actor))
    assert r.status_code == 403
    assert 'service account' in r.get_json()['error']
    assert UserModel.get_user_by_username(service_name) is not None
    assert ots.calls == []


def test_service_account_cannot_be_demoted(client, ots, service_name):
    actor = add_user('zzadmin', 'administrator')
    target = add_user(service_name, 'administrator')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': []})
    assert r.status_code == 403
    assert role_names(target) == ['administrator']
    assert ots.calls == []


def test_service_account_cannot_get_expiry_but_can_be_cleared_and_edited(client, ots, service_name):
    actor = add_user('zzadmin', 'administrator')
    target = add_user(service_name, 'administrator')
    h = headers(actor)
    assert client.put(f'{API}/users/{target.id}', headers=h, json={'expiryDate': '2099-01-01'}).status_code == 403
    _db.session.expire_all()
    assert target.expiryDate is None
    r = client.put(f'{API}/users/{target.id}', headers=h, json={
        'callsign': 'NEW', 'expiryDate': None, 'roleIds': [role('administrator').id]})
    assert r.status_code == 200
    assert target.callsign == 'NEW'
    assert ots.role_calls() == []


def test_service_account_password_via_put_is_refused(client, ots, service_name):
    actor = add_user('zzadmin', 'administrator')
    target = add_user(service_name, 'administrator')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'password': GOOD_PASSWORD})
    assert r.status_code == 403
    assert 'OTS_PASSWORD' in r.get_json()['error']
    assert ots.calls == []


def test_service_account_change_password_is_refused(client, ots, service_name):
    target = add_user(service_name, 'administrator')
    r = client.post(f'{API}/auth/change-password', headers=headers(target),
                    json={'currentPassword': 'whatever1', 'newPassword': GOOD_PASSWORD})
    assert r.status_code == 403
    assert 'OTS_PASSWORD' in r.get_json()['error']
    assert ots.calls == []


def test_service_account_set_password_is_refused(client, ots, service_name):
    target = add_user(service_name, 'administrator', has_password=False)
    r = client.post(f'{API}/auth/set-password', headers=headers(target), json={'newPassword': GOOD_PASSWORD})
    assert r.status_code == 403
    assert 'OTS_PASSWORD' in r.get_json()['error']
    assert ots.calls == []


def test_service_account_forgot_password_sends_nothing_and_looks_the_same(client, monkeypatch, service_name):
    sent = []
    monkeypatch.setattr(auth_mod, 'send_html_email', lambda **k: sent.append(k))
    target = add_user(service_name, 'administrator')
    add_user('zzplain')
    r = client.post(f'{API}/auth/forgot-password', json={'email': target.email})
    normal = client.post(f'{API}/auth/forgot-password', json={'email': 'zzplain@example.com'})
    assert r.status_code == normal.status_code == 200
    assert r.get_json() == normal.get_json()
    assert OneTimeTokenModel.query.filter_by(user_id=target.id).count() == 0
    assert [m['recipients'] for m in sent] == [['zzplain@example.com']]


def test_service_account_reset_password_with_existing_token_is_refused(client, ots, service_name):
    target = add_user(service_name, 'administrator')
    _db.session.add(OneTimeTokenModel(user_id=target.id, token='zz-reset', token_type='password_reset',
                                      expires_at=datetime.now() + timedelta(hours=1)))
    _db.session.commit()
    r = client.post(f'{API}/auth/reset-password', json={'token': 'zz-reset', 'new_password': GOOD_PASSWORD})
    assert r.status_code == 403
    assert 'OTS_PASSWORD' in r.get_json()['error']
    assert ots.calls == []


def test_other_user_password_paths_still_work(client, ots, service_name):
    user = add_user('zzplain')
    _db.session.add(OneTimeTokenModel(user_id=user.id, token='zz-reset', token_type='password_reset',
                                      expires_at=datetime.now() + timedelta(hours=1)))
    _db.session.commit()
    assert client.post(f'{API}/auth/reset-password', json={'token': 'zz-reset', 'new_password': GOOD_PASSWORD}).status_code == 200
    assert ('reset_user_password', 'zzplain') in ots.calls


def test_expiry_job_skips_service_account(ots, service_name):
    SystemSettingsModel.set_setting('remove_expired_accounts_enabled', True, 'registration', 'test')
    past = datetime.now() - timedelta(days=1)
    try:
        add_user(service_name, expiryDate=past)  # no privileged role: only the name protects it
        add_user('zzexpired', expiryDate=past)
        jobs.remove_expired_accounts()
    finally:
        SystemSettingsModel.query.filter_by(key='remove_expired_accounts_enabled').delete()
        _db.session.commit()
    assert UserModel.get_user_by_username(service_name) is not None
    assert UserModel.get_user_by_username('zzexpired') is None
    assert ('delete_user', service_name) not in ots.calls


# ---------------------------------------------------------------------------------------------
# E. Last administrator
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def sole_admin(data):
    """A user who is the only holder of the administrator role (the data fixture restores the others)."""
    admin_role = role('administrator')
    admin_role.users = []
    _db.session.commit()
    return add_user('zzsole', 'administrator')


def test_last_administrator_cannot_be_deleted(client, ots, sole_admin):
    r = client.delete(f'{API}/users/{sole_admin.id}', headers=headers(sole_admin))
    assert r.status_code == 409
    assert 'last administrator' in r.get_json()['error']
    assert UserModel.get_user_by_username('zzsole') is not None
    assert ots.calls == []


def test_last_administrator_cannot_be_demoted(client, ots, sole_admin):
    r = client.put(f'{API}/users/{sole_admin.id}', headers=headers(sole_admin), json={'roleIds': [role('zznormal').id]})
    assert r.status_code == 409
    assert role_names(sole_admin) == ['administrator']
    assert ots.calls == []


def test_second_administrator_makes_demotion_and_delete_possible(client, ots, sole_admin):
    second = add_user('zzsecond', 'administrator')
    h = headers(sole_admin)
    assert client.put(f'{API}/users/{second.id}', headers=h, json={'roleIds': []}).status_code == 200
    assert client.delete(f'{API}/users/{second.id}', headers=h).status_code == 200
    # and now the first one is alone again
    assert client.delete(f'{API}/users/{sole_admin.id}', headers=h).status_code == 409


def test_privileged_role_by_another_name_does_not_count_as_administrator(client, sole_admin):
    """An 'admin' row is privileged but does not keep the portal administrable."""
    other = add_user('zzother', 'admin')
    r = client.put(f'{API}/users/{sole_admin.id}', headers=headers(sole_admin), json={'roleIds': []})
    assert r.status_code == 409
    assert role_names(other) == ['admin']


# ---------------------------------------------------------------------------------------------
# F. Administrator role in OTS
# ---------------------------------------------------------------------------------------------
def test_grant_calls_ots_with_administrator(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', 'zznormal')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor),
                   json={'roleIds': [role('zznormal').id, role('administrator').id]})
    assert r.status_code == 200, r.get_json()
    assert ots.calls == [('set_user_roles', 'zztarget', ['administrator'])]
    assert role_names(target) == ['administrator', 'zznormal']


def test_demotion_calls_ots_with_user(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', 'administrator', 'zznormal')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('zznormal').id]})
    assert r.status_code == 200, r.get_json()
    assert ots.calls == [('set_user_roles', 'zztarget', ['user'])]
    assert role_names(target) == ['zznormal']


def test_role_is_changed_in_ots_before_group_sync_and_password(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = OTSGroupModel(name='zz-group')
    _db.session.add(group)
    _db.session.commit()
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'roleIds': [role('administrator').id], 'password': GOOD_PASSWORD, 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 200, r.get_json()
    assert [c[0] for c in ots.calls] == ['set_user_roles', 'add_user_to_group', 'reset_user_password']


@pytest.mark.parametrize('old, new', [(['admin'], ['administrator']), (['administrator', 'admin'], ['admin']), (['zznormal'], ['zzother'])])
def test_no_ots_call_when_privileged_state_stays_the_same(client, ots, old, new):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', *old)
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role(n).id for n in new]})
    assert r.status_code == 200, r.get_json()
    assert ots.role_calls() == []
    assert role_names(target) == sorted(new)


def _assert_nothing_changed(target, group):
    _db.session.expire_all()
    assert role_names(target) == ['zznormal']
    assert target.email == 'zztarget@example.com'
    assert target.expiryDate is None
    assert GroupUserAssociation.query.filter_by(user_id=target.id, group_id=group.id).count() == 0


@pytest.mark.parametrize('error, status, ots_roles', [
    # OTS may have applied these, so the old role is put back and confirmed
    (Exception('ots failure'), 502, [['administrator'], ['user']]),
    (ConnectionError('unreachable'), 502, [['administrator'], ['user']]),
    # A definite refusal: nothing to put back
    (BadRequestError({'response': {'error': 'LDAP is enabled, please use your LDAP server to assign roles'}, 'status_code': 400}), 400, [['administrator']]),
])
def test_ots_failure_leaves_everything_unchanged(client, ots, error, status, ots_roles):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', 'zznormal')
    group = OTSGroupModel(name='zz-group')
    _db.session.add(group)
    _db.session.commit()
    ots.role_errors = [error]
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'email': 'zzchanged@example.com', 'expiryDate': '2099-01-01', 'password': GOOD_PASSWORD,
        'groups': [{'id': group.id, 'direction': 'BOTH'}], 'roleIds': [role('administrator').id]})
    assert r.status_code == status
    assert 'nothing was changed' in r.get_json()['error']
    _assert_nothing_changed(target, group)
    assert [c[0] for c in ots.calls] == ['set_user_roles'] * len(ots_roles)
    assert [c[2] for c in ots.role_calls()] == ots_roles
    assert ots.accounts['zztarget'] == ['user']


def test_lost_ots_response_after_applying_the_role_is_put_back(client, ots, monkeypatch):
    """OTS did the grant but the answer never arrived."""
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    real_set = ots.set_user_roles

    def lost_first_response(username, roles):
        real_set(username, roles)
        if len(ots.role_calls()) == 1:
            raise ConnectionError('connection reset')
    monkeypatch.setattr(ots, 'set_user_roles', lost_first_response)
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]})
    assert r.status_code == 502
    assert r.get_json() == {'error': 'Could not change the role in OpenTAK Server; nothing was changed', 'otsMayDiffer': False}
    assert ots.accounts['zztarget'] == ['user']
    assert role_names(target) == []


def test_restore_that_fails_tells_the_admin_to_check_ots(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    ots.role_errors = [ConnectionError('connection reset'), ConnectionError('connection reset')]
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]})
    assert r.status_code == 502
    assert r.get_json() == {'error': 'Could not confirm the role change in OpenTAK Server, so the portal was not changed.',
                            'otsMayDiffer': True}
    assert role_names(target) == []


@pytest.mark.parametrize('body', [{}, {'results': []}, {'results': [{'username': 'someoneelse', 'roles': []}]}])
def test_read_back_without_the_account_is_unconfirmed(client, ots, monkeypatch, body):
    """No row is no evidence: it must not count as 'not an administrator', which a demotion wants to see."""
    list_users = ots.get_users

    def no_row(page=None, per_page=None, username=None, email=None):
        return {'response': dict(body)} if username else list_users(page, per_page)
    monkeypatch.setattr(ots, 'get_users', no_row)

    target = add_user('zztarget', 'administrator')
    actor = add_user('zzadmin', 'administrator')
    with pytest.raises(OTSRoleUnconfirmed):
        set_ots_admin(ots, 'zztarget', False)

    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': []})
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is True
    assert role_names(target) == ['administrator']


def test_refused_role_change_never_reaches_ots(client, ots):
    """Validation comes first: a bad value later in the body must not leave OTS changed."""
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    h = headers(actor)
    admin_id = role('administrator').id
    for body in ({'expiryDate': 'not-a-date'}, {'password': 'bad password '}, {'groups': [{'direction': 'IN'}]},
                 {'groups': [{'id': 1, 'direction': 'SIDEWAYS'}]}, {'groups': 'x'}):
        r = client.put(f'{API}/users/{target.id}', headers=h, json={'roleIds': [admin_id], **body})
        assert r.status_code == 400, body
    assert client.put(f'{API}/users/{target.id}', headers=h, json={'roleIds': admin_id}).status_code == 400
    assert ots.calls == []
    assert role_names(target) == []


def test_local_commit_failure_restores_ots_role(client, ots, monkeypatch):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    h = headers(actor)
    admin_id = role('administrator').id

    def failing_commit():
        raise RuntimeError('database gone')
    monkeypatch.setattr(users_mod.db.session, 'commit', failing_commit)
    r = client.put(f'{API}/users/{target.id}', headers=h, json={'roleIds': [admin_id]})
    monkeypatch.undo()

    assert r.status_code == 400
    assert ots.calls == [('set_user_roles', 'zztarget', ['administrator']), ('set_user_roles', 'zztarget', ['user'])]
    assert role_names(target) == []


def test_failed_restore_is_logged_and_still_returns_the_error(client, ots, monkeypatch, caplog):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    h = headers(actor)
    admin_id = role('administrator').id
    grant = ots.set_user_roles

    def fail_second(username, roles):
        if ots.role_calls():
            raise Exception('ots failure')
        grant(username, roles)
    monkeypatch.setattr(ots, 'set_user_roles', fail_second)
    monkeypatch.setattr(users_mod.db.session, 'commit', lambda: (_ for _ in ()).throw(RuntimeError('database gone')))
    r = client.put(f'{API}/users/{target.id}', headers=h, json={'roleIds': [admin_id]})
    monkeypatch.undo()

    assert r.status_code == 400
    assert 'OTS and the portal now differ' in caplog.text


def test_password_failure_after_role_change_restores_ots_role(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    ots.reset_error = Exception('ots failure')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor),
                   json={'roleIds': [role('administrator').id], 'password': GOOD_PASSWORD})
    assert r.status_code == 400
    assert ots.calls == [('set_user_roles', 'zztarget', ['administrator']), ('reset_user_password', 'zztarget'),
                         ('set_user_roles', 'zztarget', ['user'])]
    assert role_names(target) == []


def test_ots_client_set_user_roles_payload_and_errors():
    client = OTSClient('http://ots.test', 'svc', 'pw')
    client.auth_token = 'token'
    with requests_mock.Mocker() as m:
        m.post('http://ots.test/api/user/role', json={'success': True})
        client.set_user_roles('alice', ['administrator'])
        assert m.last_request.json() == {'username': 'alice', 'roles': ['administrator']}

        m.post('http://ots.test/api/user/role', status_code=400, json={'success': False, 'error': 'LDAP is enabled'})
        with pytest.raises(BadRequestError):
            client.set_user_roles('alice', ['user'])


# ---------------------------------------------------------------------------------------------
# G. OIDC providers
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('method, path', [
    ('get', '/admin/oidc/providers'),
    ('post', '/admin/oidc/providers'),
    ('put', '/admin/oidc/providers/1'),
    ('delete', '/admin/oidc/providers/1'),
    ('post', '/admin/oidc/providers/1/icon'),
    ('delete', '/admin/oidc/providers/1/icon'),
])
@pytest.mark.parametrize('role_name', ['settings_admin', 'settings_readonly', 'user_admin'])
def test_oidc_routes_refuse_non_administrators(client, method, path, role_name):
    actor = add_user('zzsettings', role_name)
    assert getattr(client, method)(f'{API}{path}', headers=headers(actor), json={}).status_code == 403


def test_oidc_routes_work_for_administrator(client, monkeypatch):
    monkeypatch.setattr(oidc_mod, '_fetch_discovery', lambda url: {'issuer': url})
    h = headers(add_user('zzadmin', 'administrator'))
    assert client.get(f'{API}/admin/oidc/providers', headers=h).status_code == 200

    created = client.post(f'{API}/admin/oidc/providers', headers=h, json={
        'name': 'zzidp', 'display_name': 'IdP', 'discovery_url': 'https://idp.test/.well-known', 'client_id': 'c', 'client_secret': 's'})
    assert created.status_code == 201
    provider = f"{API}/admin/oidc/providers/{created.get_json()['id']}"

    assert client.put(provider, headers=h, json={'display_name': 'Renamed'}).status_code == 200
    upload = client.post(f'{provider}/icon', headers=h, content_type='multipart/form-data',
                         data={'icon': (io.BytesIO(b'x'), 'icon.png')})
    assert upload.status_code == 200
    assert client.delete(f'{provider}/icon', headers=h).status_code == 200
    assert client.delete(provider, headers=h).status_code == 200


# ---------------------------------------------------------------------------------------------
# H. Pending registrations
# ---------------------------------------------------------------------------------------------
def pending_body(name, code):
    return {**user_body(name), 'onboarding_code_id': code.id}


@pytest.mark.parametrize('auto_approve', [False, True])
def test_registration_admin_cannot_select_privileged_code_on_create(client, ots, auto_approve):
    actor = add_user('zzreg', 'registration_admin')
    secret = add_code('zz-secret', 'administrator', autoApprove=auto_approve)
    r = client.post(f'{API}/pending-registrations', headers=headers(actor), json=pending_body('zznew', secret))
    assert r.status_code == 403
    assert ots.calls == []
    assert PendingRegistrationModel.query.filter_by(username='zznew').first() is None
    assert UserModel.get_user_by_username('zznew') is None


def test_administrator_auto_approve_with_privileged_code_still_works(client, ots):
    actor = add_user('zzadmin', 'administrator')
    secret = add_code('zz-secret', 'administrator', autoApprove=True)
    r = client.post(f'{API}/pending-registrations', headers=headers(actor), json=pending_body('zznew', secret))
    assert r.status_code == 201, r.get_json()
    assert ots.calls == [('create_user', 'zznew', ['administrator'])]


def test_registration_admin_can_use_normal_code(client, ots):
    actor = add_user('zzreg', 'registration_admin')
    normal = add_code('zz-normal', 'zznormal', autoApprove=True)
    r = client.post(f'{API}/pending-registrations', headers=headers(actor), json=pending_body('zznew', normal))
    assert r.status_code == 201, r.get_json()
    assert ots.calls == [('create_user', 'zznew', ['user'])]


def test_registration_admin_cannot_select_privileged_code_on_update(client):
    actor = add_user('zzreg', 'registration_admin')
    pending = add_pending('zzpending', add_code('zz-normal', 'zznormal'))
    normal_id = pending.onboarding_code_id
    secret = add_code('zz-secret', 'administrator')
    r = client.put(f'{API}/pending-registrations/{pending.id}', headers=headers(actor),
                   json={'firstName': 'Changed', 'onboarding_code_id': secret.id})
    assert r.status_code == 403
    _db.session.expire_all()
    assert pending.onboarding_code_id == normal_id
    assert pending.firstName == 'F'


def test_registration_admin_cannot_take_over_registration_for_privileged_code(client):
    """Changing the email or password here would make the editor the owner of the admin account to come."""
    actor = add_user('zzreg', 'registration_admin')
    pending = add_pending('zzpending', add_code('zz-secret', 'administrator'))
    r = client.put(f'{API}/pending-registrations/{pending.id}', headers=headers(actor),
                   json={'email': 'zzhack@example.com', 'password': GOOD_PASSWORD})
    assert r.status_code == 403
    _db.session.expire_all()
    assert pending.email == 'zzpending@example.com'
    assert pending.password == 'Pendingpass1'


def test_registration_admin_cannot_approve_for_privileged_code(client, ots):
    actor = add_user('zzreg', 'registration_admin')
    pending = add_pending('zzpending', add_code('zz-secret', 'Admin'))
    r = client.post(f'{API}/pending-registrations/{pending.id}/approve', headers=headers(actor))
    assert r.status_code == 403
    assert ots.calls == []
    assert UserModel.get_user_by_username('zzpending') is None
    assert PendingRegistrationModel.query.get(pending.id) is not None


def test_administrator_can_approve_for_privileged_code_and_registration_admin_for_normal(client, ots):
    secret_pending = add_pending('zzsecret', add_code('zz-secret', 'administrator'))
    normal_pending = add_pending('zznormal', add_code('zz-normal', 'zznormal'))
    secret_id, normal_id = secret_pending.id, normal_pending.id
    assert client.post(f'{API}/pending-registrations/{secret_id}/approve',
                       headers=headers(add_user('zzadmin', 'administrator'))).status_code == 200
    assert client.post(f'{API}/pending-registrations/{normal_id}/approve',
                       headers=headers(add_user('zzreg', 'registration_admin'))).status_code == 200
    assert ('create_user', 'zzsecret', ['administrator']) in ots.calls
    assert ('create_user', 'zznormal', ['user']) in ots.calls


@pytest.mark.parametrize('role_name', ['registration_admin', 'registration_readonly'])
def test_pending_views_mask_code_value_and_token_of_privileged_code(client, role_name):
    h = headers(add_user('zzreg', role_name))
    secret_pending = add_pending('zzsecret', add_code('zz-secret', 'administrator'))
    normal_pending = add_pending('zznormal', add_code('zz-normal', 'zznormal'))

    rows = {p['id']: p for p in client.get(f'{API}/pending-registrations', headers=h).get_json()['pending_registrations']}
    assert rows[secret_pending.id]['onboarding_code']['code'] is None
    assert rows[normal_pending.id]['onboarding_code']['code'] == 'zz-normal'

    secret = client.get(f'{API}/pending-registrations/{secret_pending.id}', headers=h).get_json()
    assert secret['onboarding_code']['code'] is None
    assert secret['verification_token'] is None
    normal = client.get(f'{API}/pending-registrations/{normal_pending.id}', headers=h).get_json()
    assert normal['verification_token'] == 'verify-zznormal'


def test_pending_view_shows_code_value_to_administrator(client):
    h = headers(add_user('zzadmin', 'administrator'))
    pending = add_pending('zzsecret', add_code('zz-secret', 'administrator'))
    body = client.get(f'{API}/pending-registrations/{pending.id}', headers=h).get_json()
    assert body['onboarding_code']['code'] == 'zz-secret'
    assert body['verification_token'] == 'verify-zzsecret'


# ---------------------------------------------------------------------------------------------
# H. Approvals
# ---------------------------------------------------------------------------------------------
def add_approval(name, code_value, *code_roles):
    approver_role = role('zzapprover')
    code = add_code(code_value, *code_roles, requireApproval=True, approverRoleId=approver_role.id)
    return add_pending(name, code, approval_status='pending_approval')


def test_approver_cannot_approve_for_privileged_code(client, ots):
    pending = add_approval('zzpending', 'zz-secret', 'administrator')
    actor = add_user('zzapp', 'zzapprover')
    r = client.post(f'{API}/approvals/{pending.id}/approve', headers=headers(actor))
    assert r.status_code == 403
    assert ots.calls == []
    assert UserModel.get_user_by_username('zzpending') is None


def test_administrator_in_approver_role_can_approve_for_privileged_code(client, ots):
    pending = add_approval('zzpending', 'zz-secret', 'administrator')
    actor = add_user('zzapp', 'zzapprover', 'administrator')
    r = client.post(f'{API}/approvals/{pending.id}/approve', headers=headers(actor))
    assert r.status_code == 200, r.get_json()
    assert ('create_user', 'zzpending', ['administrator']) in ots.calls


def test_approver_can_approve_for_normal_code(client, ots):
    pending = add_approval('zzpending', 'zz-normal', 'zznormal')
    actor = add_user('zzapp', 'zzapprover')
    assert client.post(f'{API}/approvals/{pending.id}/approve', headers=headers(actor)).status_code == 200
    assert ('create_user', 'zzpending', ['user']) in ots.calls


def test_approvals_list_masks_code_value_of_privileged_code(client):
    secret = add_approval('zzsecret', 'zz-secret', 'administrator')
    normal = add_approval('zznormal', 'zz-normal', 'zznormal')
    actor = add_user('zzapp', 'zzapprover')
    rows = {p['id']: p for p in client.get(f'{API}/approvals', headers=headers(actor)).get_json()['pending_approvals']}
    assert rows[secret.id]['onboarding_code']['code'] is None
    assert rows[normal.id]['onboarding_code']['code'] == 'zz-normal'


# ---------------------------------------------------------------------------------------------
# H. Event links
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def event():
    ev, team = EventModel(name='zz-event'), EventTeamModel(name='zz-team')
    _db.session.add_all([ev, team])
    _db.session.commit()
    previous = SystemSettingsModel.get_setting('events_enabled', None)
    SystemSettingsModel.set_setting('events_enabled', True)
    yield ev, team
    _db.session.rollback()
    if previous is None:
        SystemSettingsModel.query.filter_by(key='events_enabled').delete()
        _db.session.commit()
    else:
        SystemSettingsModel.set_setting('events_enabled', previous)


def add_link(event, code, role='player'):
    ev, team = event
    link = EventLinkModel(eventId=ev.id, eventTeamId=team.id, role=role, tokenHash=f'zz-hash-{role}', tokenVersion=1,
                          tokenNonce='zz-nonce', onboardingCodeId=code.id, direction='BOTH')
    _db.session.add(link)
    _db.session.commit()
    return link


def test_events_admin_cannot_bind_privileged_code(client, event):
    ev, team = event
    secret = add_code('zz-secret', 'administrator')
    r = client.post(f'{API}/events/{ev.id}/links', headers=headers(add_user('zzevt', 'events_admin')),
                    json={'teamId': team.id, 'role': 'player', 'onboardingCodeId': secret.id})
    assert r.status_code == 403
    assert 'Only an administrator' in r.get_json()['error']
    assert EventLinkModel.query.filter_by(eventId=ev.id).count() == 0


def test_events_admin_can_bind_normal_code_and_administrator_privileged_code(client, event):
    ev, team = event
    normal = add_code('zz-normal', 'zznormal')
    r = client.post(f'{API}/events/{ev.id}/links', headers=headers(add_user('zzevt', 'events_admin')),
                    json={'teamId': team.id, 'role': 'player', 'onboardingCodeId': normal.id})
    assert r.status_code == 201
    assert r.get_json()['onboardingCode']['onboardingCode'] == 'zz-normal'

    secret = add_code('zz-secret', 'administrator')
    r = client.post(f'{API}/events/{ev.id}/links', headers=headers(add_user('zzadmin', 'administrator')),
                    json={'teamId': team.id, 'role': 'leader', 'onboardingCodeId': secret.id})
    assert r.status_code == 201
    assert r.get_json()['onboardingCode']['onboardingCode'] == 'zz-secret'


@pytest.mark.parametrize('role_name', ['events_admin', 'events_readonly'])
def test_link_list_masks_code_value_of_privileged_code(client, event, role_name):
    link = add_link(event, add_code('zz-secret', 'administrator'))
    body = client.get(f'{API}/events/{link.eventId}/links', headers=headers(add_user('zzevt', role_name))).get_json()
    row = next(x for x in body['links'] if x['id'] == link.id)
    assert row['onboardingCode']['onboardingCode'] is None
    assert row['onboardingCode']['name'] == 'name of zz-secret'
    assert ': \'zz-secret\'' not in str(body)


def test_link_list_shows_code_value_to_administrator(client, event):
    link = add_link(event, add_code('zz-secret', 'administrator'))
    body = client.get(f'{API}/events/{link.eventId}/links', headers=headers(add_user('zzadmin', 'administrator'))).get_json()
    assert body['links'][0]['onboardingCode']['onboardingCode'] == 'zz-secret'


def test_link_put_cannot_rebind_and_masks_the_code(client, event):
    link = add_link(event, add_code('zz-secret', 'administrator'))
    code_id = link.onboardingCodeId
    other = add_code('zz-normal', 'zznormal')
    r = client.put(f'{API}/events/links/{link.id}', headers=headers(add_user('zzevt', 'events_admin')),
                   json={'direction': 'IN', 'onboardingCodeId': other.id})
    assert r.status_code == 200
    assert r.get_json()['onboardingCode']['onboardingCode'] is None
    assert r.get_json()['onboardingCodeId'] == code_id


def test_events_admin_cannot_get_token_of_link_with_privileged_code(client, event):
    """The token leads to the code value through the public join route."""
    link = add_link(event, add_code('zz-secret', 'administrator'))
    h = headers(add_user('zzevt', 'events_admin'))
    for r in (client.get(f'{API}/events/links/{link.id}/url', headers=h),
              client.post(f'{API}/events/links/{link.id}/revoke', headers=h)):
        assert r.status_code == 403
        assert 'Only an administrator' in r.get_json()['error']
    _db.session.expire_all()
    assert link.tokenVersion == 1


# ---------------------------------------------------------------------------------------------
# Approve-by-link for a code carrying a privileged role
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(auth_mod, 'send_html_email', lambda **k: sent.append(k))
    return sent


def register(client, name, code):
    return client.post(f'{API}/auth/register', json={**user_body(name), 'onboardingCode': code.onboardingCode})


def approval_recipients(mails):
    return sorted(m['recipients'][0] for m in mails if m['subject'].startswith('Approval Required'))


def test_approve_link_of_privileged_code_only_goes_to_administrators(client, mails):
    add_user('zzapp', 'zzapprover')
    add_user('zzadminrow', 'Admin')  # privileged, but not an administrator
    add_user('zzadmin', 'administrator')
    code = add_code('zz-secret', 'administrator', requireApproval=True, approverRoleId=role('zzapprover').id)
    assert register(client, 'zznew', code).status_code in (200, 201)
    recipients = approval_recipients(mails)
    assert 'zzadmin@example.com' in recipients
    assert 'zzapp@example.com' not in recipients
    assert 'zzadminrow@example.com' not in recipients


def test_approve_link_of_normal_code_still_goes_to_approver_role(client, ots, mails):
    add_user('zzapp', 'zzapprover')
    code = add_code('zz-normal', 'zznormal', requireApproval=True, approverRoleId=role('zzapprover').id)
    assert register(client, 'zznew', code).status_code in (200, 201)
    assert approval_recipients(mails) == ['zzapp@example.com']
    token = PendingRegistrationModel.query.filter_by(username='zznew').one().approval_token
    assert client.get(f'{API}/registration/approve/{token}').status_code == 200
    assert ('create_user', 'zznew', ['user']) in ots.calls


def test_privileged_code_without_any_administrator_mails_no_approver(client, mails):
    role('administrator').users = []
    _db.session.commit()
    add_user('zzapp', 'zzapprover')
    code = add_code('zz-secret', 'administrator', requireApproval=True, approverRoleId=role('zzapprover').id)
    assert register(client, 'zznew', code).status_code == 500
    assert approval_recipients(mails) == []
    assert PendingRegistrationModel.query.filter_by(username='zznew').first() is None


def test_new_approve_link_of_privileged_code_works(client, ots, mails):
    add_user('zzadmin', 'administrator')
    code = add_code('zz-secret', 'administrator', requireApproval=True, approverRoleId=role('zzapprover').id)
    assert register(client, 'zznew', code).status_code in (200, 201)
    token = PendingRegistrationModel.query.filter_by(username='zznew').one().approval_token
    assert client.get(f'{API}/registration/approve/{token}').status_code == 200
    assert ('create_user', 'zznew', ['administrator']) in ots.calls
    assert role_names(UserModel.get_user_by_username('zznew')) == ['administrator']


def test_old_approve_link_of_privileged_code_is_refused_and_row_stays(client, ots, mails):
    """A row from before this rule: its link may be in a non-administrator's mailbox."""
    pending = add_approval('zzold', 'zz-secret', 'administrator')
    pending.approval_token = 'old-style-token'
    _db.session.commit()
    r = client.get(f'{API}/registration/approve/old-style-token')
    assert r.status_code == 403
    assert b'administrator' in r.data
    assert ots.calls == []
    assert UserModel.get_user_by_username('zzold') is None
    assert PendingRegistrationModel.query.get(pending.id).approval_status == 'pending_approval'


def test_old_row_can_still_be_approved_by_an_administrator_in_the_portal(client, ots):
    pending = add_approval('zzold', 'zz-secret', 'administrator')
    pending.approval_token = 'old-style-token'
    _db.session.commit()
    r = client.post(f'{API}/pending-registrations/{pending.id}/approve', headers=headers(add_user('zzadmin', 'administrator')))
    assert r.status_code == 200
    assert ('create_user', 'zzold', ['administrator']) in ots.calls


def test_link_is_refused_when_the_code_became_privileged_after_it_was_mailed(client, ots, mails):
    add_user('zzapp', 'zzapprover')
    code = add_code('zz-normal', requireApproval=True, approverRoleId=role('zzapprover').id)
    assert register(client, 'zznew', code).status_code in (200, 201)
    token = PendingRegistrationModel.query.filter_by(username='zznew').one().approval_token
    admin_row = role('Admin')
    code.roles.append(admin_row)
    _db.session.commit()
    assert client.get(f'{API}/registration/approve/{token}').status_code == 403
    assert ots.calls == []


def test_new_token_is_not_needed_to_reject(client, mails):
    pending = add_approval('zzold', 'zz-secret', 'administrator')
    pending.approval_token = 'old-style-token'
    _db.session.commit()
    assert client.get(f'{API}/registration/reject/old-style-token').status_code == 200
    assert PendingRegistrationModel.query.filter_by(username='zzold').first() is None


# ---------------------------------------------------------------------------------------------
# Reading the OTS role back
# ---------------------------------------------------------------------------------------------
def test_demotion_of_account_holding_two_roles_is_retried_until_it_shows(client, ots):
    """OTS leaves ['user', 'administrator'] an administrator on the first call and says success."""
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', 'administrator')
    ots.accounts['zztarget'] = ['user', 'administrator']
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': []})
    assert r.status_code == 200, r.get_json()
    assert ots.role_calls() == [('set_user_roles', 'zztarget', ['user'])] * 2
    assert ots.accounts['zztarget'] == ['user']
    assert role_names(target) == []


def test_demotion_that_does_not_show_after_a_retry_fails_with_nothing_changed(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', 'administrator')
    ots.accounts['zztarget'] = ['user', 'administrator']
    ots.role_order = ['user', 'administrator']
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'email': 'zzchanged@example.com', 'roleIds': []})
    assert r.status_code == 502
    assert 'nothing was changed' in r.get_json()['error']
    assert len(ots.role_calls()) == 2
    assert role_names(target) == ['administrator']
    assert target.email == 'zztarget@example.com'


def test_grant_that_does_not_show_fails_with_nothing_changed(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    ots.ignore_role_changes = True
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]})
    assert r.status_code == 502
    assert len(ots.role_calls()) == 2
    assert role_names(target) == []


def test_role_that_cannot_be_read_back_fails_and_is_put_back(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    ots.read_back_error = ConnectionError('unreachable')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]})
    assert r.status_code == 502
    assert r.get_json()['otsMayDiffer'] is True
    assert role_names(target) == []
    assert ots.role_calls() == [('set_user_roles', 'zztarget', ['administrator']), ('set_user_roles', 'zztarget', ['user'])]
    assert ots.accounts['zztarget'] == ['user']


def test_ots_client_reads_the_role_back_over_http():
    client = OTSClient('http://ots.test', 'svc', 'pw')
    client.auth_token = 'token'
    held = {'roles': [{'name': 'user'}]}

    def users(request, context):
        return {'results': [{'username': 'alice', **held}], 'total_pages': 1}

    def set_role(request, context):
        held['roles'] = [{'name': r} for r in request.json()['roles']]
        return {'success': True}

    with requests_mock.Mocker() as m:
        m.get('http://ots.test/api/users', json=users)
        m.post('http://ots.test/api/user/role', json=set_role)
        set_ots_admin(client, 'alice', True)
    assert held['roles'] == [{'name': 'administrator'}]


# ---------------------------------------------------------------------------------------------
# PUT /users: no partial effects
# ---------------------------------------------------------------------------------------------
def add_group(name='zz-group'):
    group = OTSGroupModel(name=name)
    _db.session.add(group)
    _db.session.commit()
    return group


def test_forced_failure_in_local_group_rewrite_never_reaches_ots(client, ots, monkeypatch):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    h, admin_id = headers(actor), role('administrator').id
    with monkeypatch.context() as m:
        def explode(group_id):
            raise RuntimeError('SECRET-SQL-TEXT')
        m.setattr(users_mod.OTSGroupModel, 'get_by_id', explode)
        r = client.put(f'{API}/users/{target.id}', headers=h, json={
            'roleIds': [admin_id], 'password': GOOD_PASSWORD, 'groups': [{'id': group.id}]})
    assert r.status_code == 400
    assert 'SECRET-SQL-TEXT' not in r.get_data(as_text=True)
    assert ots.calls == []
    assert role_names(target) == []


def test_failing_group_push_fails_the_update_and_restores_the_role(client, ots, monkeypatch):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()

    def refuse(*args, **kwargs):
        raise Exception('ots failure')
    monkeypatch.setattr(ots, 'add_user_to_group', refuse)
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'roleIds': [role('administrator').id], 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 502
    assert r.get_json()['failedGroups'] == ['zz-group']
    assert r.get_json()['otsMayDiffer'] is False
    assert role_names(target) == []
    assert ots.role_calls()[-1] == ('set_user_roles', 'zztarget', ['user'])
    assert ots.accounts['zztarget'] == ['user']


@pytest.mark.parametrize('field', ['groups', 'groupIds'])
def test_duplicate_group_ids_with_password_change_nothing(client, ots, field):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    ids = [group.id, group.id]
    body = {'groups': [{'id': i} for i in ids]} if field == 'groups' else {'groupIds': ids}
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'password': GOOD_PASSWORD, **body})
    assert r.status_code == 400
    assert ots.calls == []


def test_string_id_of_existing_group_changes_nothing(client, ots):
    """'5' is not 5: it would have been removed from the group in OTS but kept locally."""
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    _db.session.add(GroupUserAssociation(group_id=group.id, user_id=target.id, direction='BOTH'))
    _db.session.commit()
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'groups': [{'id': str(group.id), 'direction': 'BOTH'}]})
    assert r.status_code == 400
    assert ots.calls == []
    assert GroupUserAssociation.query.filter_by(user_id=target.id, group_id=group.id).count() == 1


@pytest.mark.parametrize('body', [
    {'groups': [{'id': True}]}, {'groups': [{'id': '1'}]}, {'groups': [{}]}, {'groups': [1]}, {'groups': None}, {'groups': {'id': 1}},
    {'groupIds': 1}, {'groupIds': '12'}, {'groupIds': None}, {'groupIds': [True]}, {'groupIds': ['1']}, {'groupIds': {'1': 1}},
    {'groups': [], 'groupIds': 'x'},
])
def test_malformed_groups_are_refused_before_any_ots_call(client, ots, body):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id], **body})
    assert r.status_code == 400
    assert ots.calls == []


def test_commit_failure_after_password_reset_says_the_password_changed(client, ots, monkeypatch):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    h, admin_id = headers(actor), role('administrator').id

    def failing_commit():
        raise RuntimeError('SECRET-SQL-TEXT')
    with monkeypatch.context() as m:
        m.setattr(users_mod.db.session, 'commit', failing_commit)
        r = client.put(f'{API}/users/{target.id}', headers=h, json={'roleIds': [admin_id], 'password': GOOD_PASSWORD})
    assert r.status_code == 400
    assert 'password was already changed' in r.get_json()['error']
    assert r.get_json()['passwordChanged'] is True
    assert 'SECRET-SQL-TEXT' not in r.get_data(as_text=True)
    assert ots.calls[-1] == ('set_user_roles', 'zztarget', ['user'])
    assert role_names(target) == []


def test_password_reset_failure_does_not_return_ots_text_and_reverts_groups(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    group = add_group()
    ots.reset_error = Exception('SECRET-OTS-TEXT')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'password': GOOD_PASSWORD, 'groups': [{'id': group.id, 'direction': 'IN'}]})
    assert r.status_code == 400
    assert r.get_json() == {'error': 'Failed to reset password', 'passwordChanged': False}
    assert ots.calls == [('add_user_to_group', 'zztarget', 'zz-group', 'IN'), ('reset_user_password', 'zztarget'),
                         ('remove_user_from_group', 'zztarget', 'zz-group', 'IN')]
    assert GroupUserAssociation.query.filter_by(user_id=target.id).count() == 0


def test_local_change_that_cannot_be_stored_never_reaches_ots(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    add_user('zztaken')
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={
        'email': 'zztaken@example.com', 'password': GOOD_PASSWORD, 'roleIds': [role('administrator').id]})
    assert r.status_code == 400
    assert r.get_json() == {'error': 'Failed to update user'}
    assert ots.calls == []
    assert role_names(target) == []


# ---------------------------------------------------------------------------------------------
# Users without an OTS account yet
# ---------------------------------------------------------------------------------------------
def test_user_without_password_gets_the_role_locally_and_no_ots_call(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zzoidc', has_password=False)
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]})
    assert r.status_code == 200
    assert role_names(target) == ['administrator']
    assert ots.calls == []


def test_user_without_password_is_demoted_locally_with_no_ots_call(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zzoidc', 'administrator', has_password=False)
    assert client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': []}).status_code == 200
    assert role_names(target) == []
    assert ots.calls == []


@pytest.mark.parametrize('role_name, ots_roles', [('administrator', ['administrator']), ('Admin', ['administrator']), ('zznormal', ['user'])])
def test_set_password_creates_the_ots_account_with_the_right_role(client, ots, role_name, ots_roles):
    target = add_user('zzoidc', role_name, has_password=False)
    r = client.post(f'{API}/auth/set-password', headers=headers(target), json={'newPassword': GOOD_PASSWORD})
    assert r.status_code == 200, r.get_json()
    assert ots.calls == [('create_user', 'zzoidc', ots_roles)]


# ---------------------------------------------------------------------------------------------
# Mixed-case OTS usernames
# ---------------------------------------------------------------------------------------------
def test_role_change_uses_the_real_ots_username(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    del ots.accounts['zztarget']
    ots.accounts['ZzTarget'] = ['user']
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]})
    assert r.status_code == 200, r.get_json()
    assert ots.role_calls() == [('set_user_roles', 'ZzTarget', ['administrator'])]
    assert ots.accounts['ZzTarget'] == ['administrator']


def test_exact_ots_username_wins_over_case_variant(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    ots.accounts['ZzTarget'] = ['user']
    assert client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]}).status_code == 200
    assert ots.accounts['zztarget'] == ['administrator']
    assert ots.accounts['ZzTarget'] == ['user']


@pytest.mark.parametrize('names', [[], ['ZzTarget', 'ZZTARGET']])
def test_grant_with_missing_or_ambiguous_ots_account_refuses_with_nothing_changed(client, ots, names):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget')
    del ots.accounts['zztarget']
    ots.accounts.update({n: ['user'] for n in names})
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': [role('administrator').id]})
    assert r.status_code == 400
    assert ots.calls == []
    assert role_names(target) == []


def test_demotion_with_ambiguous_ots_account_refuses_with_nothing_changed(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', 'administrator')
    del ots.accounts['zztarget']
    ots.accounts.update({'ZzTarget': ['administrator'], 'ZZTARGET': ['user']})
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': []})
    assert r.status_code == 400
    assert ots.calls == []
    assert role_names(target) == ['administrator']


def test_demotion_with_no_ots_account_succeeds(client, ots):
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', 'administrator')
    del ots.accounts['zztarget']
    r = client.put(f'{API}/users/{target.id}', headers=headers(actor), json={'roleIds': []})
    assert r.status_code == 200, r.get_json()
    assert ots.calls == []
    assert role_names(target) == []


def test_demotion_is_refused_when_ots_returns_no_user_list(client, ots, monkeypatch):
    """An empty answer does not show the account is gone: it may still be administrator in OTS."""
    actor = add_user('zzadmin', 'administrator')
    target = add_user('zztarget', 'administrator')
    h = headers(actor)
    monkeypatch.setattr(ots, 'get_users', lambda *a, **k: {'response': {}})
    r = client.put(f'{API}/users/{target.id}', headers=h, json={'roleIds': []})
    assert r.status_code == 502
    assert ots.calls == []
    assert role_names(target) == ['administrator']


# ---------------------------------------------------------------------------------------------
# Remaining checks
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('value', ['dict', 'int'])
def test_role_ids_that_are_no_list_are_refused(client, ots, value):
    admin_id = role('administrator').id
    role_ids = {str(admin_id): 1} if value == 'dict' else admin_id
    onb = headers(add_user('zzonb', 'onboarding_admin'))
    uadm = headers(add_user('zzuadm', 'user_admin'))
    target = add_user('zztarget')

    assert client.post(f'{API}/onboarding-codes', headers=onb,
                       json={'name': 'c', 'onboardingCode': 'zz-new', 'roleIds': role_ids}).status_code == 403
    assert client.post(f'{API}/users', headers=uadm, json={**user_body('zznew'), 'roleIds': role_ids}).status_code == 403
    assert client.put(f'{API}/users/{target.id}', headers=uadm, json={'roleIds': role_ids}).status_code == 400
    assert OnboardingCodeModel.get_onboarding_code_by_code('zz-new') is None
    assert ots.calls == []


def test_unset_ots_username_means_no_service_account(app, monkeypatch):
    monkeypatch.setitem(app.config, 'OTS_USERNAME', 'None')
    assert not is_service_account('none')
    monkeypatch.setitem(app.config, 'OTS_USERNAME', 'Svc')
    assert is_service_account(' svc ')


def test_expiry_job_skips_any_privileged_role_name(ots):
    SystemSettingsModel.set_setting('remove_expired_accounts_enabled', True, 'registration', 'test')
    try:
        add_user('zzexpired', ' ADMIN ', expiryDate=datetime.now() - timedelta(days=1))
        jobs.remove_expired_accounts()
    finally:
        SystemSettingsModel.query.filter_by(key='remove_expired_accounts_enabled').delete()
        _db.session.commit()
    assert UserModel.get_user_by_username('zzexpired') is not None


@pytest.mark.parametrize('name', PRIVILEGED_NAMES)
def test_every_code_flow_maps_a_privileged_name_to_ots_administrator(client, ots, name):
    actor = headers(add_user('zzadmin', 'administrator', 'zzapprover'))
    code = add_code('zz-secret', name, requireApproval=True, approverRoleId=role('zzapprover').id)
    manual = add_pending('zzmanual', code)
    approval = add_pending('zzappr', code, approval_status='pending_approval')
    verify = add_pending('zzverify', code)
    assert client.post(f'{API}/pending-registrations/{manual.id}/approve', headers=actor).status_code == 200
    assert client.post(f'{API}/approvals/{approval.id}/approve', headers=actor).status_code == 200
    assert client.post(f'{API}/auth/verify-email', json={'token': verify.verification_token}).status_code == 200
    for username in ('zzmanual', 'zzappr', 'zzverify'):
        assert ('create_user', username, ['administrator']) in ots.calls


def test_retrievable_is_false_for_a_viewer_who_may_not_get_the_token(client, app, event, monkeypatch):
    """Print-all and the QR button go by this flag, so they skip the link instead of hitting a 403."""
    monkeypatch.setitem(app.config, 'SECRET_KEY', 'a-strong-secret-key-for-tests-0123456789')
    secret_link = add_link(event, add_code('zz-secret', 'administrator'))
    normal_link = add_link(event, add_code('zz-normal', 'zznormal'), role='leader')
    for link in (secret_link, normal_link):
        link.tokenHash = events_mod._hash_token(events_mod._derive_link_token(link))
    _db.session.commit()
    links_url = f'{API}/events/{secret_link.eventId}/links'

    h = headers(add_user('zzevt', 'events_admin'))
    flags = {l['id']: l['retrievable'] for l in client.get(links_url, headers=h).get_json()['links']}
    assert flags == {secret_link.id: False, normal_link.id: True}
    # what print-all does: only the retrievable ones, and those all answer
    for link_id in [i for i, retrievable in flags.items() if retrievable]:
        assert client.get(f'{API}/events/links/{link_id}/url', headers=h).status_code == 200

    h = headers(add_user('zzadmin', 'administrator'))
    flags = {l['id']: l['retrievable'] for l in client.get(links_url, headers=h).get_json()['links']}
    assert flags == {secret_link.id: True, normal_link.id: True}
    assert client.get(f'{API}/events/links/{secret_link.id}/url', headers=h).status_code == 200

