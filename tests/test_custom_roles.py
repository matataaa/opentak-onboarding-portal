"""
Custom roles: the editor's API, and the subset rule behind it: nobody hands out what they do not hold.
Who may reach each route as a built-in role is frozen in tests/test_rbac_golden.py; this file is about
what the rule refuses within those routes, one path per test.
"""
from datetime import datetime
from itertools import count

import pytest

from app import rbac
from app.api_v1 import auth as auth_mod
from app.models import (EventLinkModel, MeshtasticChannelGroup, OnboardingCodeModel, PendingRegistrationModel, RolePermissionModel,
                        UserModel, UserRoleModel, db)
from app.permissions import ALL_PERMISSIONS, ASSIGNABLE_PERMISSIONS, BUILTIN_ROLE_PERMISSIONS, CATALOGUE, requirements_of
from tests.rbac_matrix import API, GOOD_PASSWORD, Case, FakeState, Matrix

unique = count()


@pytest.fixture(scope='module')
def matrix():
    with Matrix() as m:
        yield m


# ---------------------------------------------------------------- helpers: rows join the clean state

def seeded(matrix, make):
    with matrix.app.app_context():
        result = make()
        db.session.commit()
        db.session.remove()
    matrix.snapshot()
    return result


def new_role(matrix, *keys, name=None, **kwargs):
    def make():
        role = UserRoleModel(name=name or f'cr_role{next(unique)}', display_name='r', **kwargs)
        role.permissions = [RolePermissionModel(permission=key) for key in keys]
        db.session.add(role)
        db.session.flush()
        return role.id
    return seeded(matrix, make)


def role_named(matrix, name):
    with matrix.app.app_context():
        role_id = UserRoleModel.query.filter_by(name=name).one().id
        db.session.remove()
    return role_id


def new_user(matrix, *role_ids):
    """A user holding these roles (by id or built-in name); returns the identity name"""
    identity = f'cr{next(unique)}'

    def make():
        roles = [db.session.get(UserRoleModel, r) if isinstance(r, int) else UserRoleModel.query.filter_by(name=r).one()
                 for r in role_ids]
        matrix.add_identity(identity, *roles)
    seeded(matrix, make)
    return identity


def holding(matrix, *keys):
    return new_user(matrix, new_role(matrix, *keys))


def new_code(matrix, *role_ids, **kwargs):
    value = f'crcode{next(unique)}'

    def make():
        code = OnboardingCodeModel(name=value, onboardingCode=value,
                                   roles=[db.session.get(UserRoleModel, r) for r in role_ids], **kwargs)
        db.session.add(code)
        db.session.flush()
        return code.id
    return seeded(matrix, make), value


def new_pending(matrix, code_id, **kwargs):
    name = f'crpending{next(unique)}'

    def make():
        row = PendingRegistrationModel(username=name, email=f'{name}@example.com', password='x', firstName='f', lastName='l',
                                       callsign=name, onboarding_code_id=code_id, verification_token=f'verify-{name}',
                                       expires_at=datetime(2099, 1, 1), **kwargs)
        db.session.add(row)
        db.session.flush()
        return row.id
    return seeded(matrix, make), f'verify-{name}'


def new_link(matrix, code_id):
    def make():
        row = db.session.get(EventLinkModel, matrix.ids['link_player'])
        row.onboardingCodeId = code_id
        return row.id
    return seeded(matrix, make)


def call(matrix, identity, method, path, body=None, check=None):
    """Returns (status, json); check runs against the database before the call is undone"""
    seen = {}
    probe = (lambda resp, ids: seen.update(result=check()) or None) if check else None
    case = Case(f'{method} {path}', method, f'{API}{path}', body, probe=probe)
    _, resp = matrix.call(case, identity)
    return (resp.status_code, resp.get_json(silent=True), seen['result']) if check else (resp.status_code, resp.get_json(silent=True))


def stored_keys(role_id):
    return lambda: sorted(row.permission for row in RolePermissionModel.query.filter_by(role_id=role_id))


def role_names_of(user_id):
    return lambda: sorted(r.name for r in db.session.get(UserModel, user_id).roles)


def everything_of(*modules):
    return [k for m in modules for k, flags in CATALOGUE[m].items() if 'A' not in flags]


# ---------------------------------------------------------------- subset rule: role permissions

def test_role_admin_composes_only_what_it_holds(matrix):
    role_id = new_role(matrix)
    status, body, stored = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}/permissions',
                                {'permissions': ['roles.view']}, stored_keys(role_id))
    assert (status, stored) == (200, ['roles.view'])


def test_adding_a_key_the_actor_does_not_hold_is_refused(matrix):
    role_id = new_role(matrix, 'roles.view')
    status, body, stored = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}/permissions',
                                {'permissions': ['roles.view', 'users.create']}, stored_keys(role_id))
    assert (status, stored) == (403, ['roles.view'])
    assert body['missingPermissions'] == ['users.create']


def test_a_custom_actor_cannot_add_to_its_own_role(matrix):
    role_id = new_role(matrix, 'roles.permissions.edit', 'roles.view')
    actor = new_user(matrix, role_id)
    status, _, stored = call(matrix, actor, 'PUT', f'/roles/{role_id}/permissions',
                             {'permissions': ['roles.permissions.edit', 'roles.view', 'settings.view']}, stored_keys(role_id))
    assert status == 403
    assert stored == ['roles.permissions.edit', 'roles.view']


def test_removing_a_key_the_actor_does_not_hold_is_refused(matrix):
    role_id = new_role(matrix, 'roles.view', 'settings.view')
    status, _, stored = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}/permissions', {'permissions': ['roles.view']},
                             stored_keys(role_id))
    assert status == 403
    assert stored == ['roles.view', 'settings.view']


def test_a_role_above_the_actor_cannot_be_renamed(matrix):
    role_id = new_role(matrix, 'settings.view')
    assert call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}', {'name': 'cr_renamed'})[0] == 403
    assert call(matrix, 'administrator', 'PUT', f'/roles/{role_id}', {'name': 'cr_renamed'})[0] == 200


def test_a_role_above_the_actor_cannot_be_deleted(matrix):
    role_id = new_role(matrix, 'settings.view')
    assert call(matrix, 'role_admin', 'DELETE', f'/roles/{role_id}')[0] == 403
    assert call(matrix, 'administrator', 'DELETE', f'/roles/{role_id}')[0] == 200


def test_a_role_within_the_actor_can_be_renamed_and_deleted(matrix):
    role_id = new_role(matrix, 'roles.view')
    assert call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}', {'name': 'cr_renamed'})[0] == 200
    assert call(matrix, 'role_admin', 'DELETE', f'/roles/{role_id}')[0] == 200


def test_saving_the_same_keys_succeeds_and_affects_nobody(matrix):
    role_id = new_role(matrix, 'roles.view', 'roles.edit')
    new_user(matrix, role_id)
    status, body, stored = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}/permissions',
                                {'permissions': ['roles.edit', 'roles.view']}, stored_keys(role_id))
    assert (status, stored) == (200, ['roles.edit', 'roles.view'])
    assert body['impact'] == {'added': [], 'removed': [], 'userCount': 0, 'codeCount': 0}


def test_adding_and_removing_in_one_save(matrix):
    role_id = new_role(matrix, 'roles.view', 'roles.delete')
    status, body, stored = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}/permissions',
                                {'permissions': ['roles.view', 'roles.edit']}, stored_keys(role_id))
    assert (status, stored) == (200, ['roles.edit', 'roles.view'])
    assert (body['impact']['added'], body['impact']['removed']) == (['roles.edit'], ['roles.delete'])


def test_edit_is_stored_with_the_view_it_implies(matrix):
    role_id = new_role(matrix)
    status, body, stored = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}/permissions', {'permissions': ['roles.edit']},
                                stored_keys(role_id))
    assert status == 200
    assert body['permissions'] == stored == ['roles.edit', 'roles.view']


def test_rows_of_a_key_that_left_the_catalogue_do_not_block_a_save(matrix):
    role_id = new_role(matrix, 'roles.view', 'gone.from.catalogue')
    status, _, stored = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}/permissions', {'permissions': ['roles.view']},
                             stored_keys(role_id))
    assert (status, stored) == (200, ['roles.view'])


@pytest.mark.parametrize('key', sorted(ALL_PERMISSIONS - ASSIGNABLE_PERMISSIONS))
def test_administrator_only_keys_are_refused_even_for_the_administrator(matrix, key):
    role_id = new_role(matrix)
    status, body, stored = call(matrix, 'administrator', 'PUT', f'/roles/{role_id}/permissions', {'permissions': [key]},
                                stored_keys(role_id))
    assert (status, stored) == (400, [])
    assert body['permissions'] == [key]


def test_unknown_keys_are_refused(matrix):
    role_id = new_role(matrix)
    status, body, stored = call(matrix, 'administrator', 'PUT', f'/roles/{role_id}/permissions',
                                {'permissions': ['roles.view', 'no.such.key']}, stored_keys(role_id))
    assert (status, stored) == (400, [])
    assert body == {'error': 'Unknown permissions', 'permissions': ['no.such.key']}


@pytest.mark.parametrize('body', [{}, {'permissions': 'roles.view'}, {'permissions': [1]}, {'permissions': [['roles.view']]}, []])
def test_malformed_lists_are_refused(matrix, body):
    assert call(matrix, 'administrator', 'PUT', f'/roles/{new_role(matrix)}/permissions', body)[0] == 400


@pytest.mark.parametrize('name', ['administrator', 'user_admin', 'admin', 'user'])
def test_system_roles_cannot_bear_rows(matrix, name):
    role_id = role_named(matrix, name)
    status, _, stored = call(matrix, 'administrator', 'PUT', f'/roles/{role_id}/permissions', {'permissions': ['roles.view']},
                             stored_keys(role_id))
    assert (status, stored) == (403, [])


def test_a_role_from_ots_cannot_bear_rows(matrix):
    role_id = new_role(matrix, description='Auto-created from OTS')
    status, _, stored = call(matrix, 'administrator', 'PUT', f'/roles/{role_id}/permissions', {'permissions': ['roles.view']},
                             stored_keys(role_id))
    assert (status, stored) == (403, [])


@pytest.mark.parametrize('description', ['Radio team for OTS field kits', 'Auto-created from OTS (renamed)', 'OTS'])
def test_a_description_mentioning_ots_does_not_lock_a_role(matrix, description):
    role_id = new_role(matrix, description=description)
    status, body, stored = call(matrix, 'administrator', 'PUT', f'/roles/{role_id}/permissions', {'permissions': ['roles.view']},
                                stored_keys(role_id))
    assert (status, stored) == (200, ['roles.view'])
    status, body = call(matrix, 'administrator', 'GET', f'/roles/{role_id}')
    assert (status, body['isProtected'], body['isCustom'], body['locked']) == (200, False, True, False)


def test_a_role_from_ots_stays_protected(matrix):
    role_id = new_role(matrix, description='Auto-created from OTS')
    status, body = call(matrix, 'administrator', 'GET', f'/roles/{role_id}')
    assert (status, body['isProtected'], body['isCustom'], body['lockedReason']) == (200, True, False, 'system')
    assert call(matrix, 'administrator', 'PUT', f'/roles/{role_id}', {'description': 'x'})[0] == 403
    assert call(matrix, 'administrator', 'DELETE', f'/roles/{role_id}')[0] == 403


def test_deleting_a_role_removes_its_rows_through_the_api(matrix):
    role_id = new_role(matrix, 'roles.view')
    status, _, rows = call(matrix, 'administrator', 'DELETE', f'/roles/{role_id}', check=stored_keys(role_id))
    assert (status, rows) == (200, [])


# ---------------------------------------------------------------- impact

def test_impact_counts_who_holds_the_role(matrix):
    role_id = new_role(matrix)
    new_user(matrix, role_id)
    new_user(matrix, role_id)
    new_code(matrix, role_id)
    status, body = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}/permissions', {'permissions': ['roles.view']})
    assert status == 200
    assert body['impact'] == {'added': ['roles.view'], 'removed': [], 'userCount': 2, 'codeCount': 1}


def test_preview_says_the_same_and_writes_nothing(matrix):
    role_id = new_role(matrix, 'roles.delete')
    new_user(matrix, role_id)
    status, body, stored = call(matrix, 'role_admin', 'POST', f'/roles/{role_id}/permissions/preview',
                                {'permissions': ['roles.edit']}, stored_keys(role_id))
    assert (status, stored) == (200, ['roles.delete'])
    assert body == {'permissions': ['roles.edit', 'roles.view'],
                    'impact': {'added': ['roles.edit', 'roles.view'], 'removed': ['roles.delete'], 'userCount': 1, 'codeCount': 0}}


def test_preview_refuses_what_the_save_would_refuse(matrix):
    role_id = new_role(matrix)
    assert call(matrix, 'role_admin', 'POST', f'/roles/{role_id}/permissions/preview', {'permissions': ['users.create']})[0] == 403
    assert call(matrix, 'role_admin', 'POST', f'/roles/{role_id}/permissions/preview', {'permissions': ['nope']})[0] == 400


# ---------------------------------------------------------------- duplicate

def test_administrator_copies_a_built_in_without_administrator_only_keys(matrix):
    status, body, stored = call(matrix, 'administrator', 'POST', f"/roles/{role_named(matrix, 'administrator')}/duplicate",
                                {'name': 'cr_full'},
                                lambda: sorted(r.permission for r in UserRoleModel.query.filter_by(name='cr_full').one().permissions))
    assert status == 201
    assert body['role']['permissions'] == stored == sorted(ASSIGNABLE_PERMISSIONS)


def test_copy_of_a_built_in_is_custom_and_editable(matrix):
    def check():
        return rbac.is_custom_role_name('cr_copy'), sorted(r.permission for r in UserRoleModel.query.filter_by(name='cr_copy').one().permissions)
    status, _, (custom, stored) = call(matrix, 'role_admin', 'POST', f"/roles/{role_named(matrix, 'role_readonly')}/duplicate",
                                       {'name': 'cr_copy'}, check)
    assert (status, custom, stored) == (201, True, sorted(BUILTIN_ROLE_PERMISSIONS['role_readonly']))


def test_copy_of_a_role_above_the_actor_is_refused(matrix):
    status, body, exists = call(matrix, 'role_admin', 'POST', f"/roles/{role_named(matrix, 'radio_readonly')}/duplicate",
                                {'name': 'cr_copy'}, lambda: UserRoleModel.query.filter_by(name='cr_copy').count())
    assert (status, exists) == (403, 0)
    assert body['missingPermissions'] == sorted(BUILTIN_ROLE_PERMISSIONS['radio_readonly'])


def test_copying_the_administrator_is_refused_below_administrator(matrix):
    assert call(matrix, 'role_admin', 'POST', f"/roles/{role_named(matrix, 'administrator')}/duplicate", {'name': 'cr_copy'})[0] == 403


@pytest.mark.parametrize('name', [None, '', 'user_admin', ' ADMIN ', 'user', 5])
def test_copy_needs_a_new_unreserved_name(matrix, name):
    source = role_named(matrix, 'role_readonly')
    assert call(matrix, 'administrator', 'POST', f'/roles/{source}/duplicate', {'name': name})[0] == 400


def test_copy_needs_a_free_name(matrix):
    source = role_named(matrix, 'role_readonly')
    assert call(matrix, 'administrator', 'POST', f'/roles/{source}/duplicate', {'name': 'matrix_spare'})[0] == 409


def test_copy_needs_both_role_edit_keys(matrix):
    source = role_named(matrix, 'role_readonly')
    assert call(matrix, holding(matrix, 'roles.edit'), 'POST', f'/roles/{source}/duplicate', {'name': 'cr_copy'})[0] == 403
    assert call(matrix, holding(matrix, 'roles.permissions.edit'), 'POST', f'/roles/{source}/duplicate', {'name': 'cr_copy'})[0] == 403
    assert call(matrix, holding(matrix, 'roles.edit', 'roles.permissions.edit'), 'POST', f'/roles/{source}/duplicate',
                {'name': 'cr_copy'})[0] == 201


def test_copy_of_a_custom_role_has_its_own_rows(matrix):
    source = new_role(matrix, 'roles.view', description='medics')

    def check():
        copy = UserRoleModel.query.filter_by(name='cr_copy').one()
        return copy.id != source, copy.description, [r.permission for r in copy.permissions], copy.users
    status, _, (other_id, description, keys, users) = call(matrix, 'role_admin', 'POST', f'/roles/{source}/duplicate',
                                                           {'name': 'cr_copy'}, check)
    assert (status, other_id, description, keys, users) == (201, True, 'medics', ['roles.view'], [])


def test_copy_of_an_ots_role_does_not_inherit_the_lock(matrix):
    source = new_role(matrix, description='Auto-created from OTS')
    status, body = call(matrix, 'role_admin', 'POST', f'/roles/{source}/duplicate', {'name': 'cr_copy'})
    assert (status, body['role']['description']) == (201, '')


# ---------------------------------------------------------------- subset rule: role on a user

def test_user_admin_cannot_assign_itself_settings_admin(matrix):
    user_id = matrix.user_ids['user_admin']
    status, body, roles = call(matrix, 'user_admin', 'PUT', f'/users/{user_id}',
                               {'roleIds': [role_named(matrix, 'user_admin'), role_named(matrix, 'settings_admin')]},
                               role_names_of(user_id))
    assert (status, roles) == (403, ['user_admin'])
    assert 'settings.view' in body['missingPermissions']


def test_user_admin_cannot_assign_a_role_above_it_to_someone_else(matrix):
    target = matrix.ids['target']
    status, _, roles = call(matrix, 'user_admin', 'PUT', f'/users/{target}', {'roleIds': [role_named(matrix, 'settings_admin')]},
                            role_names_of(target))
    assert (status, roles) == (403, [])


def test_user_admin_cannot_create_a_user_with_a_role_above_it(matrix):
    body = {'username': 'crnewuser', 'password': GOOD_PASSWORD, 'email': 'crnew@example.com', 'firstName': 'f', 'lastName': 'l',
            'callsign': 'c', 'roleIds': [role_named(matrix, 'settings_admin')]}
    status, _, in_ots = call(matrix, 'user_admin', 'POST', '/users', body, lambda: 'crnewuser' in FakeState.accounts)
    assert (status, in_ots) == (403, False)


def test_a_key_that_does_nothing_alone_still_counts(matrix):
    """users.delete without its view key counts for nothing, until the holder gets that from elsewhere."""
    dormant = new_role(matrix, 'users.delete')
    actor = holding(matrix, 'users.roles.assign', 'users.accounts.view')
    target = matrix.ids['target']
    assert call(matrix, actor, 'PUT', f'/users/{target}', {'roleIds': [dormant]})[0] == 403


def test_assigning_a_role_within_the_actor_succeeds(matrix):
    target = matrix.ids['target']
    status, _, roles = call(matrix, 'user_admin', 'PUT', f'/users/{target}', {'roleIds': [role_named(matrix, 'user_readonly')]},
                            role_names_of(target))
    assert (status, roles) == (200, ['user_readonly'])


def test_saving_the_same_roles_succeeds(matrix):
    within = new_role(matrix, 'users.accounts.view')
    target = new_user(matrix, within)
    user_id = matrix.user_ids[target]
    status, _, roles = call(matrix, 'user_admin', 'PUT', f'/users/{user_id}', {'firstName': 'x', 'roleIds': [within]},
                            role_names_of(user_id))
    assert status == 200 and len(roles) == 1


def test_saving_own_profile_with_a_role_above_the_assign_right_succeeds(matrix):
    """Own roles are held by definition: an unchanged list passes, however it was obtained."""
    identity = new_user(matrix, new_role(matrix, 'users.roles.assign', 'users.accounts.view'), 'radio_admin')
    user_id = matrix.user_ids[identity]
    with matrix.app.app_context():
        own = [r.id for r in db.session.get(UserModel, user_id).roles]
        db.session.remove()
    assert call(matrix, identity, 'PUT', f'/users/{user_id}', {'roleIds': own})[0] == 200


def test_removing_a_role_above_the_actor_from_someone_is_refused(matrix):
    user_id = matrix.user_ids[new_user(matrix, 'settings_readonly')]
    status, _, roles = call(matrix, 'user_admin', 'PUT', f'/users/{user_id}', {'roleIds': []}, role_names_of(user_id))
    assert (status, roles) == (403, ['settings_readonly'])


def test_removing_an_own_role_with_a_key_the_actor_does_not_hold_is_refused(matrix):
    """The delta check without the target guard in the way: on one's own profile, a key that counts for nothing."""
    keep = new_role(matrix, 'users.roles.assign', 'users.accounts.view')
    dormant = new_role(matrix, 'radios.delete')
    identity = new_user(matrix, keep, dormant)
    user_id = matrix.user_ids[identity]
    assert call(matrix, identity, 'PUT', f'/users/{user_id}', {'roleIds': [keep, dormant]})[0] == 200
    status, _, roles = call(matrix, identity, 'PUT', f'/users/{user_id}', {'roleIds': [keep]}, role_names_of(user_id))
    assert status == 403 and len(roles) == 2


# ---------------------------------------------------------------- target guard

@pytest.fixture(scope='module')
def target_above(matrix):
    """Holds a key user_admin lacks, and an identity that may do everything to users"""
    return matrix.user_ids[new_user(matrix, 'settings_readonly')]


def test_target_above_the_actor_cannot_be_edited(matrix, target_above):
    assert call(matrix, 'user_admin', 'PUT', f'/users/{target_above}', {'firstName': 'x'})[0] == 403


def test_password_of_a_target_above_the_actor_cannot_be_reset(matrix, target_above):
    status, _, resets = call(matrix, 'user_admin', 'PUT', f'/users/{target_above}', {'password': GOOD_PASSWORD},
                             lambda: FakeState.password_resets)
    assert (status, resets) == (403, 0)


def test_email_of_a_target_above_the_actor_cannot_be_changed(matrix, target_above):
    status, _, email = call(matrix, 'user_admin', 'PUT', f'/users/{target_above}', {'email': 'taken@example.com'},
                            lambda: db.session.get(UserModel, target_above).email)
    assert status == 403 and email != 'taken@example.com'


def test_groups_of_a_target_above_the_actor_cannot_be_changed(matrix, target_above):
    assert call(matrix, 'user_admin', 'PUT', f'/users/{target_above}', {'groupIds': []})[0] == 403


def test_target_above_the_actor_cannot_be_deleted(matrix, target_above):
    status, _, kept = call(matrix, 'user_admin', 'DELETE', f'/users/{target_above}',
                           check=lambda: db.session.get(UserModel, target_above) is not None)
    assert (status, kept) == (403, True)


def test_target_within_the_actor_can_be_edited_and_deleted(matrix):
    user_id = matrix.user_ids[new_user(matrix, 'user_readonly')]
    assert call(matrix, 'user_admin', 'PUT', f'/users/{user_id}', {'firstName': 'x'})[0] == 200
    assert call(matrix, 'user_admin', 'DELETE', f'/users/{user_id}')[0] == 200


def test_custom_actor_may_not_touch_a_peer_with_more(matrix):
    actor = holding(matrix, 'users.accounts.edit')
    target = matrix.user_ids[holding(matrix, 'users.accounts.edit', 'users.delete')]
    assert call(matrix, actor, 'PUT', f'/users/{target}', {'firstName': 'x'})[0] == 403


def test_own_profile_stays_editable(matrix):
    identity = holding(matrix, 'groups.view')
    assert call(matrix, identity, 'PUT', f'/users/{matrix.user_ids[identity]}', {'firstName': 'x'})[0] == 200


# ---------------------------------------------------------------- subset rule: onboarding codes

def test_onboarding_admin_cannot_create_a_code_with_a_role_above_it(matrix):
    status, _, exists = call(matrix, 'onboarding_admin', 'POST', '/onboarding-codes',
                             {'name': 'n', 'onboardingCode': 'crnewcode', 'roleIds': [role_named(matrix, 'user_admin')]},
                             lambda: OnboardingCodeModel.get_onboarding_code_by_code('crnewcode') is not None)
    assert (status, exists) == (403, False)


def test_onboarding_admin_cannot_attach_a_role_above_it(matrix):
    code_id, _ = new_code(matrix)
    status, _, roles = call(matrix, 'onboarding_admin', 'PUT', f'/onboarding-codes/{code_id}',
                            {'roleIds': [role_named(matrix, 'user_admin')]},
                            lambda: [r.name for r in db.session.get(OnboardingCodeModel, code_id).roles])
    assert (status, roles) == (403, [])


def test_onboarding_admin_can_attach_a_role_within_it(matrix):
    code_id, _ = new_code(matrix)
    assert call(matrix, 'onboarding_admin', 'PUT', f'/onboarding-codes/{code_id}',
                {'roleIds': [role_named(matrix, 'onboarding_readonly')]})[0] == 200


def test_saving_a_code_with_unchanged_roles_succeeds(matrix):
    within = new_role(matrix, 'onboarding.view')
    code_id, _ = new_code(matrix, within)
    assert call(matrix, 'onboarding_admin', 'PUT', f'/onboarding-codes/{code_id}', {'description': 'x', 'roleIds': [within]})[0] == 200


# Approving needs every permission the code hands out (see the approvals tests), so only a privileged approver role
# is guarded here, as in phase 0

@pytest.mark.parametrize('approver,status', [('administrator', 403), ('settings_admin', 201)])
def test_only_a_privileged_approver_role_is_refused_on_create(matrix, approver, status):
    body = {'name': 'n', 'onboardingCode': 'crnewcode', 'approverRoleId': role_named(matrix, approver)}
    assert call(matrix, 'onboarding_admin', 'POST', '/onboarding-codes', body)[0] == status


@pytest.mark.parametrize('approver,status', [('administrator', 403), ('settings_admin', 200)])
def test_only_a_privileged_approver_role_is_refused_on_update(matrix, approver, status):
    code_id, _ = new_code(matrix)
    assert call(matrix, 'onboarding_admin', 'PUT', f'/onboarding-codes/{code_id}',
                {'approverRoleId': role_named(matrix, approver)})[0] == status


def test_privileged_approver_role_stays_on_a_save_that_keeps_it(matrix):
    high = role_named(matrix, 'administrator')
    code_id, _ = new_code(matrix, approverRoleId=high)
    assert call(matrix, 'onboarding_admin', 'PUT', f'/onboarding-codes/{code_id}', {'description': 'x', 'approverRoleId': high})[0] == 200


@pytest.mark.parametrize('approver,status', [('administrator', 403), ('settings_admin', 200)])
def test_only_a_privileged_approver_role_cannot_be_replaced(matrix, approver, status):
    code_id, _ = new_code(matrix, approverRoleId=role_named(matrix, approver))
    assert call(matrix, 'onboarding_admin', 'PUT', f'/onboarding-codes/{code_id}', {'approverRoleId': None})[0] == status


def test_administrator_may_set_a_privileged_approver_role(matrix):
    code_id, _ = new_code(matrix)
    assert call(matrix, 'administrator', 'PUT', f'/onboarding-codes/{code_id}',
                {'approverRoleId': role_named(matrix, 'administrator')})[0] == 200


def test_approver_role_without_permissions_is_fine(matrix):
    plain = new_role(matrix)
    assert call(matrix, 'onboarding_admin', 'POST', '/onboarding-codes',
                {'name': 'n', 'onboardingCode': 'crnewcode', 'approverRoleId': plain})[0] == 201


# ---------------------------------------------------------------- a code above the actor is read-only and masked

@pytest.fixture(scope='module')
def code_above(matrix):
    """Carries a key onboarding and registration admins lack"""
    return new_code(matrix, new_role(matrix, 'users.create', 'users.accounts.view'))


def test_code_above_the_actor_is_listed_without_its_value(matrix, code_above):
    status, body = call(matrix, 'onboarding_admin', 'GET', '/onboarding-codes')
    row = next(c for c in body['codes'] if c['id'] == code_above[0])
    assert (status, row['onboardingCode'], row['aboveLevel']) == (200, None, True)
    row = next(c for c in call(matrix, 'administrator', 'GET', '/onboarding-codes')[1]['codes'] if c['id'] == code_above[0])
    assert (row['onboardingCode'], row['aboveLevel']) == (code_above[1], False)


def test_code_above_the_actor_is_fetched_without_its_value(matrix, code_above):
    status, body = call(matrix, 'onboarding_admin', 'GET', f'/onboarding-codes/{code_above[0]}')
    assert (status, body['onboardingCode'], body['aboveLevel']) == (200, None, True)


def test_search_does_not_confirm_a_masked_code_value(matrix):
    above = new_role(matrix, 'users.create', 'users.accounts.view')

    def make():
        code = OnboardingCodeModel(name='cr masked', onboardingCode='crmaskedvalue', roles=[db.session.get(UserRoleModel, above)])
        db.session.add(code)
        db.session.flush()
        return code.id
    code_id = seeded(matrix, make)
    assert call(matrix, 'onboarding_admin', 'GET', '/onboarding-codes?search=crmaskedvalue')[1]['codes'] == []
    assert [c['id'] for c in call(matrix, 'onboarding_admin', 'GET', '/onboarding-codes?search=cr masked')[1]['codes']] == [code_id]
    assert [c['id'] for c in call(matrix, 'administrator', 'GET', '/onboarding-codes?search=crmaskedvalue')[1]['codes']] == [code_id]


@pytest.mark.parametrize('method,body', [('PUT', {'description': 'x'}), ('DELETE', None)])
def test_code_above_the_actor_is_read_only(matrix, code_above, method, body):
    status, _, exists = call(matrix, 'onboarding_admin', method, f'/onboarding-codes/{code_above[0]}', body,
                             lambda: db.session.get(OnboardingCodeModel, code_above[0]).description)
    assert (status, exists) == (403, None)


@pytest.mark.parametrize('method,body', [('GET', None), ('PUT', {'description': 'x'}), ('DELETE', None)])
def test_privileged_code_stays_hidden(matrix, method, body):
    code_id, _ = new_code(matrix, role_named(matrix, 'administrator'))
    assert call(matrix, 'onboarding_admin', method, f'/onboarding-codes/{code_id}', body)[0] == 404
    assert code_id not in [c['id'] for c in call(matrix, 'onboarding_admin', 'GET', '/onboarding-codes')[1]['codes']]


def test_code_within_the_actor_stays_visible(matrix):
    code_id, value = new_code(matrix, role_named(matrix, 'onboarding_readonly'))
    assert call(matrix, 'onboarding_admin', 'GET', f'/onboarding-codes/{code_id}')[1]['onboardingCode'] == value


def test_code_with_a_role_without_permissions_is_visible_to_every_viewer(matrix):
    code_id, value = new_code(matrix, new_role(matrix))
    assert call(matrix, 'onboarding_readonly', 'GET', f'/onboarding-codes/{code_id}')[1]['onboardingCode'] == value


def test_custom_viewer_below_a_custom_code_cannot_see_its_value(matrix):
    viewer = holding(matrix, 'onboarding.view')
    code_id, _ = new_code(matrix, new_role(matrix, 'onboarding.view', 'onboarding.edit'))
    assert call(matrix, viewer, 'GET', f'/onboarding-codes/{code_id}')[1]['onboardingCode'] is None


# ---------------------------------------------------------------- pending registrations

def test_pending_list_masks_the_value_of_a_code_above_the_viewer(matrix, code_above):
    pending_id, _ = new_pending(matrix, code_above[0])
    rows = call(matrix, 'registration_admin', 'GET', '/pending-registrations')[1]['pending_registrations']
    row = next(r for r in rows if r['id'] == pending_id)
    assert row['onboarding_code']['code'] is None and row['onboarding_code']['name']


def test_pending_detail_masks_value_and_token_of_a_code_above_the_viewer(matrix, code_above):
    pending_id, token = new_pending(matrix, code_above[0])
    body = call(matrix, 'registration_admin', 'GET', f'/pending-registrations/{pending_id}')[1]
    assert body['onboarding_code']['code'] is None and body['verification_token'] is None
    assert call(matrix, 'administrator', 'GET', f'/pending-registrations/{pending_id}')[1]['verification_token'] == token


def test_pending_cannot_be_created_with_a_code_above_the_actor(matrix, code_above):
    body = {'username': 'crpick', 'password': GOOD_PASSWORD, 'email': 'crpick@example.com', 'firstName': 'f', 'lastName': 'l',
            'callsign': 'c', 'onboarding_code_id': code_above[0]}
    assert call(matrix, 'registration_admin', 'POST', '/pending-registrations', body)[0] == 403


def test_pending_cannot_be_moved_to_a_code_above_the_actor(matrix, code_above):
    plain, _ = new_code(matrix)
    pending_id, _ = new_pending(matrix, plain)
    assert call(matrix, 'registration_admin', 'PUT', f'/pending-registrations/{pending_id}',
                {'onboarding_code_id': code_above[0]})[0] == 403


def test_pending_of_a_code_above_the_actor_cannot_be_edited(matrix, code_above):
    pending_id, _ = new_pending(matrix, code_above[0])
    assert call(matrix, 'registration_admin', 'PUT', f'/pending-registrations/{pending_id}', {'email': 'taken@example.com'})[0] == 403


def test_pending_of_a_code_above_the_actor_cannot_be_approved(matrix, code_above):
    pending_id, _ = new_pending(matrix, code_above[0])
    assert call(matrix, 'registration_admin', 'POST', f'/pending-registrations/{pending_id}/approve')[0] == 403


def test_pending_of_a_code_within_the_actor_can_be_approved(matrix):
    code_id, _ = new_code(matrix, role_named(matrix, 'registration_readonly'))
    pending_id, _ = new_pending(matrix, code_id)
    assert call(matrix, 'registration_admin', 'POST', f'/pending-registrations/{pending_id}/approve')[0] == 200


# ---------------------------------------------------------------- approvals flow

def approval_setup(matrix, *code_roles):
    approver_role = new_role(matrix)
    code_id, value = new_code(matrix, *code_roles, requireApproval=True, approverRoleId=approver_role)
    pending_id, _ = new_pending(matrix, code_id, approval_status='pending_approval', approval_token=f'plain{next(unique)}')
    return approver_role, code_id, value, pending_id


def test_approver_cannot_create_an_account_above_their_level(matrix):
    approver_role, _, _, pending_id = approval_setup(matrix, new_role(matrix, 'users.create', 'users.accounts.view'))
    status, _, created = call(matrix, new_user(matrix, approver_role), 'POST', f'/approvals/{pending_id}/approve', {},
                              lambda: PendingRegistrationModel.query.get(pending_id) is not None)
    assert (status, created) == (403, True)


def test_approver_may_create_an_account_within_their_level(matrix):
    within = new_role(matrix, 'groups.view')
    approver_role, _, _, pending_id = approval_setup(matrix, within)
    assert call(matrix, new_user(matrix, approver_role, within), 'POST', f'/approvals/{pending_id}/approve', {})[0] == 200


def test_approver_may_still_create_an_account_without_permissions(matrix):
    approver_role, _, _, pending_id = approval_setup(matrix, new_role(matrix))
    assert call(matrix, new_user(matrix, approver_role), 'POST', f'/approvals/{pending_id}/approve', {})[0] == 200


def test_approver_may_still_reject_above_their_level(matrix):
    approver_role, _, _, pending_id = approval_setup(matrix, new_role(matrix, 'groups.view'))
    assert call(matrix, new_user(matrix, approver_role), 'POST', f'/approvals/{pending_id}/reject', {})[0] == 200


def test_approvals_list_masks_a_code_above_the_approver(matrix):
    approver_role, _, _, pending_id = approval_setup(matrix, new_role(matrix, 'groups.view'))
    rows = call(matrix, new_user(matrix, approver_role), 'GET', '/approvals')[1]['pending_approvals']
    assert next(r for r in rows if r['id'] == pending_id)['onboarding_code']['code'] is None


def test_approvals_list_says_which_can_be_approved(matrix):
    approver_role, _, _, above = approval_setup(matrix, new_role(matrix, 'users.create', 'users.accounts.view'))
    within_role = new_role(matrix, 'groups.view')
    within_approver, _, _, within = approval_setup(matrix, within_role)
    user = new_user(matrix, approver_role, within_approver, within_role)
    rows = {r['id']: r['canApprove'] for r in call(matrix, user, 'GET', '/approvals')[1]['pending_approvals']}
    assert (rows[above], rows[within]) == (False, True)


def test_approvals_list_shows_a_code_within_the_approver(matrix):
    approver_role, _, value, pending_id = approval_setup(matrix, new_role(matrix))
    rows = call(matrix, new_user(matrix, approver_role), 'GET', '/approvals')[1]['pending_approvals']
    assert next(r for r in rows if r['id'] == pending_id)['onboarding_code']['code'] == value


# ---------------------------------------------------------------- the emailed approve link

@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(auth_mod, 'send_html_email', lambda **k: sent.append(k))
    return sent


def register(matrix, value):
    name = f'crreg{next(unique)}'
    body = {'username': name, 'password': GOOD_PASSWORD, 'email': f'{name}@example.com', 'firstName': 'f', 'lastName': 'l',
            'callsign': 'c', 'onboardingCode': value}

    def token():
        return PendingRegistrationModel.query.filter_by(username=name).one().approval_token
    status, _, approval_token = call(matrix, 'no_role', 'POST', '/auth/register', body, token)
    assert status in (200, 201)
    return approval_token


def recipients(mails):
    return sorted(m['recipients'][0] for m in mails if m['subject'].startswith('Approval Required'))


def test_link_of_a_code_with_permissions_only_goes_to_administrators(matrix, mails):
    approver_role, _, value, _ = approval_setup(matrix, new_role(matrix, 'groups.view'))
    approver = new_user(matrix, approver_role)
    token = register(matrix, value)
    assert token.startswith(auth_mod.ADMIN_ONLY_APPROVAL_TOKEN_PREFIX)
    assert 'matrix_administrator@example.com' in recipients(mails)
    assert f'matrix_{approver}@example.com' not in recipients(mails)


def test_link_of_a_code_without_permissions_still_goes_to_the_approvers(matrix, mails):
    approver_role, _, value, _ = approval_setup(matrix, new_role(matrix))
    approver = new_user(matrix, approver_role)
    token = register(matrix, value)
    assert not token.startswith(auth_mod.ADMIN_ONLY_APPROVAL_TOKEN_PREFIX)
    assert recipients(mails) == [f'matrix_{approver}@example.com']


def test_an_older_link_is_refused_once_the_code_hands_out_permissions(matrix):
    _, _, _, pending_id = approval_setup(matrix, new_role(matrix, 'groups.view'))
    with matrix.app.app_context():
        token = db.session.get(PendingRegistrationModel, pending_id).approval_token
        db.session.remove()
    status, _, kept = call(matrix, 'anonymous', 'GET', f'/registration/approve/{token}',
                           check=lambda: db.session.get(PendingRegistrationModel, pending_id) is not None)
    assert (status, kept) == (403, True)


def test_a_link_issued_to_administrators_still_works(matrix):
    _, _, _, pending_id = approval_setup(matrix, new_role(matrix, 'groups.view'))

    def make():
        db.session.get(PendingRegistrationModel, pending_id).approval_token = 'adm.crtoken'
    seeded(matrix, make)
    assert call(matrix, 'anonymous', 'GET', '/registration/approve/adm.crtoken')[0] == 200


# ---------------------------------------------------------------- event links

def test_event_link_cannot_be_bound_to_a_code_above_the_actor(matrix, code_above):
    body = {'teamId': matrix.ids['team_b'], 'role': 'player', 'onboardingCodeId': code_above[0]}
    assert call(matrix, 'events_admin', 'POST', f"/events/{matrix.ids['event']}/links", body)[0] == 403


def test_event_link_can_be_bound_to_a_code_without_permissions(matrix):
    code_id, _ = new_code(matrix, new_role(matrix))
    body = {'teamId': matrix.ids['team_b'], 'role': 'player', 'onboardingCodeId': code_id}
    assert call(matrix, 'events_admin', 'POST', f"/events/{matrix.ids['event']}/links", body)[0] == 201


def test_event_link_list_masks_a_code_above_the_viewer(matrix, code_above):
    link_id = new_link(matrix, code_above[0])
    links = call(matrix, 'events_admin', 'GET', f"/events/{matrix.ids['event']}/links")[1]['links']
    row = next(l for l in links if l['id'] == link_id)
    assert row['onboardingCode']['onboardingCode'] is None and row['retrievable'] is False


@pytest.mark.parametrize('method,suffix', [('GET', 'url'), ('POST', 'revoke')])
def test_event_link_token_of_a_code_above_the_actor_is_refused(matrix, code_above, method, suffix):
    link_id = new_link(matrix, code_above[0])
    status, body = call(matrix, 'events_admin', method, f'/events/links/{link_id}/{suffix}', {} if method == 'POST' else None)
    assert status == 403 and 'Only an administrator' not in body['error']


# ---------------------------------------------------------------- catalogue and role fields

def test_catalogue_covers_every_key_once_with_flags(matrix):
    status, body = call(matrix, 'role_readonly', 'GET', '/roles/permission-catalog')
    assert status == 200
    rows = [(m['module'], f, p) for m in body['modules'] for f in m['functions'] for p in f['permissions']]
    assert sorted(p['key'] for _, _, p in rows) == sorted(ALL_PERMISSIONS)
    by_key = {p['key']: p for _, _, p in rows}
    assert by_key['users.impersonate']['administratorOnly'] is True and by_key['users.impersonate']['sensitive'] is True
    assert by_key['settings.oidc.view']['administratorOnly'] is True and by_key['settings.oidc.view']['sensitive'] is False
    assert by_key['groups.view']['readonlyDefault'] is True and by_key['groups.edit']['readonlyDefault'] is False
    for key, permission in by_key.items():
        assert permission['requires'] == list(dict.fromkeys(requirements_of(key)))
    assert not any('label' in p for _, _, p in rows)


@pytest.mark.parametrize('function,kind,levels', [
    ('users.accounts', 'viewEdit', ['view', 'edit']),
    ('users.create', 'action', ['action']),
    ('radios', 'viewEdit', ['view', 'edit']),
    ('radios.claim_code', 'action', ['action']),
    ('settings', 'view', ['view']),
    ('settings.branding', 'edit', ['edit']),
    ('settings.oidc', 'viewEdit', ['view', 'edit']),
    ('api_docs', 'view', ['view']),
    ('roles.permissions', 'edit', ['edit']),
])
def test_catalogue_rows_for_a_ui(matrix, function, kind, levels):
    body = call(matrix, 'administrator', 'GET', '/roles/permission-catalog')[1]
    row = next(f for m in body['modules'] for f in m['functions'] if f['function'] == function)
    assert row['kind'] == kind
    assert [p['level'] for p in row['permissions']] == levels


def test_catalogue_says_what_the_actor_can_grant(matrix):
    body = call(matrix, 'role_admin', 'GET', '/roles/permission-catalog')[1]
    grantable = {p['key'] for m in body['modules'] for f in m['functions'] for p in f['permissions'] if p['grantable']}
    assert grantable == BUILTIN_ROLE_PERMISSIONS['role_admin']
    body = call(matrix, 'administrator', 'GET', '/roles/permission-catalog')[1]
    grantable = {p['key'] for m in body['modules'] for f in m['functions'] for p in f['permissions'] if p['grantable']}
    assert grantable == ASSIGNABLE_PERMISSIONS


def test_catalogue_is_for_role_viewers(matrix):
    assert call(matrix, 'group_admin', 'GET', '/roles/permission-catalog')[0] == 403


def test_role_detail_of_a_built_in_is_computed_and_locked(matrix):
    body = call(matrix, 'role_readonly', 'GET', f"/roles/{role_named(matrix, 'user_readonly')}")[1]
    assert body['permissions'] == sorted(BUILTIN_ROLE_PERMISSIONS['user_readonly'])
    assert (body['isCustom'], body['locked'], body['lockedReason'], body['isProtected']) == (False, True, 'system', True)


def test_role_detail_of_a_custom_role_has_its_rows_and_counts(matrix):
    role_id = new_role(matrix, 'groups.edit', 'no.such.key', 'users.impersonate')
    new_user(matrix, role_id)
    new_code(matrix, role_id)
    body = call(matrix, 'administrator', 'GET', f'/roles/{role_id}')[1]
    assert body['permissions'] == ['groups.edit', 'groups.view']
    assert (body['isCustom'], body['locked'], body['lockedReason']) == (True, False, None)
    assert (body['userCount'], body['codeCount'], len(body['users'])) == (1, 1, 1)


def test_role_detail_says_when_a_custom_role_is_above_the_viewer(matrix):
    body = call(matrix, 'role_admin', 'GET', f"/roles/{new_role(matrix, 'groups.view')}")[1]
    assert (body['isCustom'], body['locked'], body['lockedReason'], body['assignable']) == (True, True, 'aboveLevel', False)


def test_admin_row_holds_nothing_but_is_not_assignable_below_administrator(matrix):
    role_id = role_named(matrix, 'admin')
    body = call(matrix, 'role_admin', 'GET', f'/roles/{role_id}')[1]
    assert (body['permissions'], body['isCustom'], body['assignable']) == ([], False, False)
    assert call(matrix, 'administrator', 'GET', f'/roles/{role_id}')[1]['assignable'] is True


def test_role_list_keeps_its_fields_and_adds_the_new_ones(matrix):
    role_id = new_role(matrix, 'groups.view')
    new_code(matrix, role_id)
    row = next(r for r in call(matrix, 'role_readonly', 'GET', '/roles')[1]['roles'] if r['id'] == role_id)
    assert set(row) >= {'id', 'name', 'displayName', 'description', 'isProtected', 'userCount', 'codeCount', 'permissions',
                        'isCustom', 'locked', 'lockedReason', 'assignable'}
    assert row['permissions'] == ['groups.view'] and row['codeCount'] == 1


def test_role_list_as_a_picker_shows_nothing_of_what_a_role_holds(matrix):
    row = next(r for r in call(matrix, 'group_admin', 'GET', '/roles')[1]['roles'] if r['name'] == 'user_admin')
    assert not {'userCount', 'codeCount', 'permissions', 'users'} & set(row)
    assert row['assignable'] is False and row['locked'] is True


# ---------------------------------------------------------------- Meshtastic device configs

@pytest.fixture(scope='module')
def secret_group(matrix):
    def make():
        group = MeshtasticChannelGroup(name=f'cr_secret{next(unique)}', isPublic=False, yamlConfig='channel_url: secret',
                                       combined_url='https://meshtastic.org/e/#SECRET')
        db.session.add(group)
        db.session.flush()
        return group.id
    return seeded(matrix, make)


def test_groups_admin_list_masks_yaml_without_channel_access(matrix, secret_group):
    identity = holding(matrix, 'meshtastic.groups.view')
    row = next(g for g in call(matrix, identity, 'GET', '/meshtastic/groups/admin')[1]['groups'] if g['id'] == secret_group)
    assert row['yamlConfig'] is None and row['combined_url'] is None
    row = next(g for g in call(matrix, 'meshtastic_admin', 'GET', '/meshtastic/groups/admin')[1]['groups'] if g['id'] == secret_group)
    assert row['yamlConfig'] == 'channel_url: secret'


def test_group_detail_masks_yaml_without_channel_access(matrix, secret_group):
    identity = holding(matrix, 'meshtastic.groups.view')
    assert call(matrix, identity, 'GET', f'/meshtastic/groups/{secret_group}')[1]['yamlConfig'] is None
    assert call(matrix, 'meshtastic_admin', 'GET', f'/meshtastic/groups/{secret_group}')[1]['yamlConfig'] == 'channel_url: secret'


def test_group_edit_without_channel_access_cannot_touch_secrets(matrix, secret_group):
    # Group edit needs the channels (REQUIRES), so without them the key does not count at all
    identity = holding(matrix, 'meshtastic.groups.edit')
    status, _, kept = call(matrix, identity, 'PUT', f'/meshtastic/groups/{secret_group}',
                           {'description': 'x', 'yamlConfig': None, 'combined_url': None},
                           lambda: db.session.get(MeshtasticChannelGroup, secret_group).yamlConfig)
    assert (status, kept) == (403, 'channel_url: secret')


# ---------------------------------------------------------------- a role change must not wake keys of other roles

def _effective(identity):
    from app.rbac import _resolve_permissions
    return lambda: sorted(_resolve_permissions([r.name for r in UserModel.query.filter_by(username=f'matrix_{identity}').one().roles]))


def _wake_actor(matrix):
    """Holds users.accounts.view and may compose roles, but does not hold users.roles.assign"""
    return holding(matrix, 'users.accounts.view', 'roles.view', 'roles.edit', 'roles.permissions.edit')


@pytest.mark.parametrize('path', ['/permissions', '/permissions/preview'])
def test_a_change_that_wakes_a_key_for_a_user_is_refused(matrix, path):
    dormant = new_role(matrix, 'users.roles.assign')  # needs users.accounts.view to count
    target_role = new_role(matrix)
    new_user(matrix, dormant, target_role)
    status, body, stored = call(matrix, _wake_actor(matrix), 'PUT' if path == '/permissions' else 'POST',
                                f'/roles/{target_role}{path}', {'permissions': ['users.accounts.view']}, stored_keys(target_role))
    assert (status, body['missingPermissions'], stored) == (403, ['users.roles.assign'], [])


def test_a_change_that_wakes_a_key_for_a_code_is_refused(matrix):
    dormant = new_role(matrix, 'users.roles.assign')
    target_role = new_role(matrix)
    new_code(matrix, dormant, target_role)
    status, body = call(matrix, _wake_actor(matrix), 'PUT', f'/roles/{target_role}/permissions',
                        {'permissions': ['users.accounts.view']})
    assert (status, body['missingPermissions']) == (403, ['users.roles.assign'])


def test_the_refused_wake_up_would_really_have_granted_the_key(matrix):
    """The administrator holds everything, so the same change goes through and the holder gains the dormant key"""
    dormant = new_role(matrix, 'users.roles.assign')
    target_role = new_role(matrix)
    holder = new_user(matrix, dormant, target_role)
    status, _, keys = call(matrix, 'administrator', 'PUT', f'/roles/{target_role}/permissions',
                           {'permissions': ['users.accounts.view']}, _effective(holder))
    assert status == 200 and 'users.roles.assign' in keys


def test_a_change_that_wakes_only_keys_the_actor_holds_is_fine(matrix):
    dormant = new_role(matrix, 'users.roles.assign')
    target_role = new_role(matrix)
    new_user(matrix, dormant, target_role)
    actor = holding(matrix, 'users.accounts.view', 'users.roles.assign', 'roles.view', 'roles.edit', 'roles.permissions.edit')
    assert call(matrix, actor, 'PUT', f'/roles/{target_role}/permissions', {'permissions': ['users.accounts.view']})[0] == 200


def test_a_change_without_holders_wakes_nothing(matrix):
    target_role = new_role(matrix)
    assert call(matrix, _wake_actor(matrix), 'PUT', f'/roles/{target_role}/permissions',
                {'permissions': ['users.accounts.view']})[0] == 200


# ---------------------------------------------------------------- the OTS description is reserved

OTS_DESCRIPTION = 'Auto-created from OTS'


def test_a_new_role_cannot_take_the_ots_description(matrix):
    status, _, exists = call(matrix, 'role_admin', 'POST', '/roles', {'name': 'cr_ots_new', 'description': OTS_DESCRIPTION},
                             lambda: UserRoleModel.query.filter_by(name='cr_ots_new').first() is not None)
    assert (status, exists) == (400, False)


def test_a_role_cannot_be_given_the_ots_description(matrix):
    role_id = new_role(matrix, description='medics')
    status, _, description = call(matrix, 'role_admin', 'PUT', f'/roles/{role_id}', {'description': OTS_DESCRIPTION, 'name': 'cr_renamed'},
                                  lambda: (db.session.get(UserRoleModel, role_id).description, db.session.get(UserRoleModel, role_id).name))
    assert (status, description) == (400, ('medics', db_name(matrix, role_id)))


def test_a_copy_cannot_take_the_ots_description(matrix):
    source = new_role(matrix, 'roles.view')
    assert call(matrix, 'role_admin', 'POST', f'/roles/{source}/duplicate',
                {'name': 'cr_ots_copy', 'description': OTS_DESCRIPTION})[0] == 400


def db_name(matrix, role_id):
    with matrix.app.app_context():
        name = db.session.get(UserRoleModel, role_id).name
        db.session.remove()
    return name


# ---------------------------------------------------------------- Meshtastic group edits need the channels

def test_group_edit_without_channel_view_does_not_count(matrix):
    editor = holding(matrix, 'meshtastic.groups.view', 'meshtastic.groups.edit')
    status, _, exists = call(matrix, editor, 'POST', '/meshtastic/groups', {'name': 'cr_mesh_group'},
                             lambda: MeshtasticChannelGroup.query.filter_by(name='cr_mesh_group').first() is not None)
    assert (status, exists) == (403, False)


def test_group_edit_with_channel_view_creates_with_secrets(matrix):
    editor = holding(matrix, 'meshtastic.channels.view', 'meshtastic.groups.view', 'meshtastic.groups.edit')
    status, _, stored = call(matrix, editor, 'POST', '/meshtastic/groups',
                             {'name': 'cr_mesh_group2', 'combined_url': 'https://meshtastic.org/e/#K', 'yamlConfig': 'a: 1'},
                             lambda: [(g.combined_url, g.yamlConfig) for g in MeshtasticChannelGroup.query.filter_by(name='cr_mesh_group2')])
    assert (status, stored) == (201, [('https://meshtastic.org/e/#K', 'a: 1')])


def test_meshtastic_admin_holds_what_group_edit_needs(matrix):
    from app.permissions import BUILTIN_ROLE_PERMISSIONS, enforce_requires
    keys = BUILTIN_ROLE_PERMISSIONS['meshtastic_admin']
    assert enforce_requires(keys) == keys and 'meshtastic.groups.edit' in keys
