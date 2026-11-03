"""
Permissions: the built-in map is consistent with the roles and modules it replaced, a custom role
gets exactly the rows it holds, and sessions never act with more than they should.
Access of the built-in roles per route is frozen in tests/test_rbac_golden.py.
"""
import json
import os
import re
from unittest import mock

import pytest
from flask_jwt_extended import verify_jwt_in_request

from app import rbac
from app.models import MODULE_SETTINGS, RolePermissionModel, SystemSettingsModel, TakProfileModel, UserModel, UserRoleModel, db
from app.permissions import (ALL_PERMISSIONS, ASSIGNABLE_PERMISSIONS, BUILTIN_ROLE_PERMISSIONS, CATALOGUE, PORTAL_MODULES,
                             REQUIRES, SETTING_PERMISSIONS, SETTINGS_EDIT_PERMISSIONS, enforce_requires, requirements_of,
                             with_implied_views)
from app.rbac import ADMIN_ROLES, MODULE_ROLES, RESERVED_ROLE_NAMES, is_custom_role_name
from tests.rbac_matrix import API, NO_ROLE, Case, Matrix

KEYS_WITHOUT_ROUTE = {'api_docs.view'}  # UI only: /api/docs has no role check
APP_DIR = os.path.join(os.path.dirname(__file__), '..', 'app')


@pytest.fixture(scope='module')
def matrix():
    with Matrix() as m:
        yield m


counter = iter(range(1000))


def custom_identity(matrix, *keys, name=None):
    """A user holding one custom role with these rows; part of the clean state from here on."""
    identity = f'custom{next(counter)}'
    with matrix.app.app_context():
        role = UserRoleModel(name=name or f'perm_{identity}', display_name=identity)
        role.permissions = [RolePermissionModel(permission=key) for key in keys]
        db.session.add(role)
        db.session.flush()
        matrix.add_identity(identity, role)
        db.session.remove()
    matrix.snapshot()
    return identity


def status(cell):
    return int(cell.split('|')[0].split('~')[0])


def probe(cell):
    return json.loads(cell.split('~', 1)[1])


def effective(matrix, identity, case=None):
    cell, resp = matrix.call(case or matrix.case('auth permissions'), identity)
    assert status(cell) == 200, cell
    return resp.get_json()


def get(matrix, case, identity):
    return matrix.call(matrix.case(case), identity)[0]


def allowed(matrix, identity, *cases):
    for case in cases:
        assert status(get(matrix, case, identity)) < 400, case


def denied(matrix, identity, *cases):
    for case in cases:
        assert status(get(matrix, case, identity)) == 403, case


# ---------------------------------------------------------------- the built-in map

def test_every_admin_role_is_in_the_map_and_holds_something():
    assert set(BUILTIN_ROLE_PERMISSIONS) == set(ADMIN_ROLES)
    assert all(BUILTIN_ROLE_PERMISSIONS.values())
    assert BUILTIN_ROLE_PERMISSIONS['administrator'] == ALL_PERMISSIONS


def test_map_agrees_with_module_roles():
    assert list(PORTAL_MODULES) == list(MODULE_ROLES)
    for module, roles in MODULE_ROLES.items():
        keys = set(CATALOGUE[PORTAL_MODULES[module]])
        holders = {role for role, held in BUILTIN_ROLE_PERMISSIONS.items() if held & keys}
        assert holders == set(roles), module


def test_module_admin_and_readonly_levels():
    for role, held in BUILTIN_ROLE_PERMISSIONS.items():
        if role == 'administrator':
            continue
        modules = [module for module, keys in CATALOGUE.items() if held & set(keys)]
        everything = {k for m in modules for k, flags in CATALOGUE[m].items() if 'A' not in flags}
        read_only = {k for m in modules for k, flags in CATALOGUE[m].items() if 'R' in flags}
        assert held == (everything if role.endswith('_admin') else read_only), role


def test_administrator_only_keys_are_for_the_administrator_alone():
    admin_only = ALL_PERMISSIONS - ASSIGNABLE_PERMISSIONS
    assert admin_only == {'users.impersonate', 'settings.oidc.view', 'settings.oidc.edit'}
    for role, held in BUILTIN_ROLE_PERMISSIONS.items():
        assert role == 'administrator' or not held & admin_only, role


def test_custom_is_decided_by_name():
    for name in RESERVED_ROLE_NAMES | {'Admin', ' ADMINISTRATOR ', 'User_Admin', 'USER', 'Event_Leader'}:
        assert not is_custom_role_name(name), name
    assert is_custom_role_name('medic')


def test_edit_implies_the_view_of_the_same_function():
    assert with_implied_views(['radios.edit']) == {'radios.edit', 'radios.view'}
    assert with_implied_views(['users.accounts.edit']) == {'users.accounts.edit', 'users.accounts.view'}
    assert with_implied_views(['settings.branding.edit']) == {'settings.branding.edit', 'settings.view'}
    assert with_implied_views(['events.links.edit']) == {'events.links.edit', 'events.links.view'}
    assert with_implied_views(['events.teams.edit']) == {'events.teams.edit', 'events.view'}
    assert with_implied_views(['groups.members.edit']) == {'groups.members.edit', 'groups.members.view'}
    # Nothing but edit does: not reveal, not delete, not the other functions
    assert with_implied_views(['events.links.reveal']) == {'events.links.reveal'}
    assert with_implied_views(['radios.delete']) == {'radios.delete'}


def test_all_settings_are_mapped_to_a_setting_permission(matrix):
    with matrix.app.app_context():
        keys = {row.key for row in SystemSettingsModel.query.all()}
        db.session.remove()
    assert keys == set(SETTING_PERMISSIONS)
    assert set(SETTING_PERMISSIONS.values()) == SETTINGS_EDIT_PERMISSIONS
    assert SETTINGS_EDIT_PERMISSIONS <= ASSIGNABLE_PERMISSIONS


def test_code_and_catalogue_agree():
    used = set()
    for folder, _, files in os.walk(APP_DIR):
        for filename in files:
            if filename.endswith('.py') and filename != 'permissions.py':
                with open(os.path.join(folder, filename)) as f:
                    source = f.read()
                used |= set(re.findall(r"(?:require_permission|has_permission|permission_denied)\('([a-z_.]+)'\)", source))
                used |= set(re.findall(r"'([a-z_]+\.[a-z_.]+)': \(", source))
    used |= SETTINGS_EDIT_PERMISSIONS
    assert used - ALL_PERMISSIONS == set()
    assert ALL_PERMISSIONS - used == KEYS_WITHOUT_ROUTE


# ---------------------------------------------------------------- /auth/permissions

@pytest.mark.parametrize('role', sorted(ADMIN_ROLES))
def test_auth_permissions_lists_the_built_in_keys(matrix, role):
    body = effective(matrix, role)
    assert body['permissions'] == sorted(BUILTIN_ROLE_PERMISSIONS[role])
    assert body['isAdmin'] is True


def _disable(*modules):
    def setup(ids):
        for module in modules:
            SystemSettingsModel.set_setting(f'{module}_enabled', 'false')
    return setup


def test_auth_permissions_leaves_out_disabled_modules(matrix):
    case = Case('modules off', 'GET', f'{API}/auth/permissions', setup=_disable('radios', 'events'))
    body = effective(matrix, 'administrator', case)
    assert not [k for k in body['permissions'] if k.startswith(('radios.', 'events.'))]
    assert 'settings.radios.edit' in body['permissions']  # a setting: stays, to turn the module back on
    assert 'radios' not in body['modules'] and 'events' not in body['modules']
    assert 'meshtastic.channels.view' in body['permissions'] and 'meshtastic' in body['modules']

    body = effective(matrix, 'radio_admin', case)
    assert body == {**body, 'permissions': [], 'modules': []}


def test_auth_permissions_without_any_role(matrix):
    body = effective(matrix, NO_ROLE)
    assert (body['permissions'], body['modules'], body['isAdmin']) == ([], [], False)


# ---------------------------------------------------------------- custom roles

def test_custom_role_gets_exactly_its_rows(matrix):
    identity = custom_identity(matrix, 'radios.program', 'radios.view', 'groups.view')
    body = effective(matrix, identity)
    assert body['permissions'] == ['groups.view', 'radios.program', 'radios.view']
    assert body['modules'] == ['radios', 'groups']
    assert body['isAdmin'] is False  # unchanged for now: role names only


def test_custom_role_edit_implies_view(matrix):
    identity = custom_identity(matrix, 'groups.edit')
    assert effective(matrix, identity)['permissions'] == ['groups.edit', 'groups.view']
    allowed(matrix, identity, 'list groups', 'get group', 'update group')
    denied(matrix, identity, 'delete group', 'group members', 'sync groups')


def test_custom_role_unknown_and_administrator_only_keys_are_ignored(matrix):
    identity = custom_identity(matrix, 'no.such.key', 'users.impersonate', 'settings.oidc.view', 'settings.oidc.edit',
                               'groups.view')
    assert effective(matrix, identity)['permissions'] == ['groups.view']
    denied(matrix, identity, 'impersonate', 'list oidc providers (admin)', 'update oidc provider')


@pytest.mark.parametrize('name', ['admin', 'Admin', ' ADMINISTRATOR ', 'user', 'event_leader', 'User_Admin'])
def test_reserved_names_never_bear_rows(matrix, name):
    with matrix.app.app_context():
        existing = UserRoleModel.query.filter_by(name=name).first()
        db.session.remove()
    if existing:
        def setup(ids):
            role = UserRoleModel.query.filter_by(name=name).first()
            db.session.add(RolePermissionModel(role_id=role.id, permission='settings.view'))
            db.session.commit()
        holder = {'admin': 'admin_row', 'user': 'ots_user'}[name]
        case = Case('holder', 'GET', f'{API}/auth/permissions', setup=setup)
        assert effective(matrix, holder, case)['permissions'] == []
    else:
        identity = custom_identity(matrix, 'settings.view', name=name)
        assert effective(matrix, identity)['permissions'] == []
        denied(matrix, identity, 'get settings (admin)')


def test_rows_on_a_built_in_role_do_not_count(matrix):
    def setup(ids):
        db.session.add(RolePermissionModel(role_id=ids['role_user_admin'], permission='settings.view'))
        db.session.commit()
    body = effective(matrix, 'user_admin', Case('stray', 'GET', f'{API}/auth/permissions', setup=setup))
    assert body['permissions'] == sorted(BUILTIN_ROLE_PERMISSIONS['user_admin'])


def test_several_roles_add_up(matrix):
    identity = custom_identity(matrix, 'groups.view')
    with matrix.app.app_context():
        user = db.session.get(UserModel, matrix.user_ids[identity])
        second = UserRoleModel(name=f'{identity}_second')
        second.permissions = [RolePermissionModel(permission='radios.view')]
        user.roles.extend([second, UserRoleModel.query.filter_by(name='role_readonly').first()])
        db.session.commit()
        db.session.remove()
    matrix.snapshot()
    assert effective(matrix, identity)['permissions'] == ['groups.view', 'radios.view', 'roles.view']


def test_custom_role_may_use_the_role_picker(matrix):
    assert status(get(matrix, 'list roles', custom_identity(matrix, 'groups.view'))) == 200
    assert status(get(matrix, 'list roles', custom_identity(matrix, 'no.such.key'))) == 403


def test_radios_program_without_radios_edit(matrix):
    identity = custom_identity(matrix, 'radios.view', 'radios.program')
    allowed(matrix, identity, 'program config', 'compare config', 'list radios')
    denied(matrix, identity, 'update radio', 'create radio', 'delete radio', 'assign radio', 'claim code')


def test_radios_edit_does_not_assign(matrix):
    identity = custom_identity(matrix, 'radios.edit')
    untouched = probe(get(matrix, 'update radio assignment', 'administrator'))
    assert untouched['assignedTo'] is None  # the administrator does change it
    cell = get(matrix, 'update radio assignment', identity)
    assert status(cell) == 200 and probe(cell)['assignedTo'] is not None
    cell = get(matrix, 'update radio assignment by id', identity)
    assert status(cell) == 200 and probe(cell)['owner'] is not None
    cell = get(matrix, 'create radio with assignment', identity)
    assert status(cell) == 201 and probe(cell) == {'assignedTo': None, 'owner': None}
    denied(matrix, identity, 'assign radio')

    identity = custom_identity(matrix, 'radios.view', 'radios.assign')
    assert status(get(matrix, 'assign radio', identity)) == 200
    denied(matrix, identity, 'update radio assignment')


def test_users_accounts_edit_without_users_delete(matrix):
    identity = custom_identity(matrix, 'users.accounts.edit')
    allowed(matrix, identity, 'list users', 'get other user')  # edit implies view
    denied(matrix, identity, 'delete user', 'create user', 'impersonate')
    assert probe(get(matrix, 'update other basics', identity))['firstName'] == 'edited'
    assert probe(get(matrix, 'update other expiry', identity))['expiry'] is True
    # Other fields are ignored, not refused
    for case, field, unchanged in (('update other roles', 'roles', []), ('update other groups', 'groups', [f"{matrix.ids['group']}:BOTH"]),
                                   ('update other password', 'passwordResets', 0)):
        cell = get(matrix, case, identity)
        assert status(cell) == 200 and probe(cell)[field] == unchanged, case


@pytest.mark.parametrize('key,case,field,changed', [
    ('users.roles.assign', 'update other roles', 'roles', ['matrix_audience']),
    ('users.groups.assign', 'update other groups', 'groups', []),
    ('users.password.reset', 'update other password', 'passwordResets', 1),
])
def test_user_field_permissions_stand_alone(matrix, key, case, field, changed):
    identity = custom_identity(matrix, 'users.accounts.view', key)
    cell = get(matrix, case, identity)
    assert status(cell) == 200 and probe(cell)[field] == changed
    # ... and do not open the basics
    assert probe(get(matrix, 'update other basics', identity))['firstName'] == 'F'


def test_create_user_only_applies_assignable_fields(matrix):
    identity = custom_identity(matrix, 'users.accounts.view', 'users.create')
    cell, resp = matrix.call(matrix.case('create user with role'), identity)
    assert status(cell) == 201 and probe(cell) == {'roles': [], 'groups': []}
    assert resp.get_json()['ignoredFields'] == ['groupIds', 'roleIds']


def test_phase_0_rules_apply_to_custom_roles(matrix):
    identity = custom_identity(matrix, 'users.accounts.edit', 'users.roles.assign', 'users.delete', 'users.create')
    # (users.accounts.edit brings the users.accounts.view the other keys need)
    assert get(matrix, 'update privileged user', identity).startswith(
        '403|Only an administrator can change a user with the administrator role')
    assert get(matrix, 'update other administrator role', identity).startswith(
        '403|Only an administrator can grant or remove the administrator role')
    assert status(get(matrix, 'delete privileged user', identity)) == 403
    assert status(get(matrix, 'create user with administrator', identity)) == 403


def test_settings_branding_without_settings_modules(matrix):
    identity = custom_identity(matrix, 'settings.branding.edit')
    allowed(matrix, identity, 'get settings (admin)', 'PUT setting primary_color', 'PUT setting date_format',
            'upload logo', 'delete logo', 'get logo settings')
    denied(matrix, identity, 'PUT setting radios_enabled', 'PUT setting events_enabled', 'PUT setting brand_name_value',
           'PUT setting oidc_enabled', 'update setting by id, module flag', 'update setting by id',
           'list oidc providers (admin)')


@pytest.mark.parametrize('key,setting', [
    ('settings.modules.edit', 'events_enabled'),
    ('settings.registration.edit', 'forgot_password_enabled'),
    ('settings.general.edit', 'brand_name_value'),
    ('settings.enrollment.edit', 'kiosk_language'),
    ('settings.radios.edit', 'claim_radio_enabled'),
    ('settings.security.edit', 'magic_link_login_enabled'),
])
def test_settings_are_authorised_by_key_not_category(matrix, key, setting):
    identity = custom_identity(matrix, key)
    assert status(get(matrix, f'PUT setting {setting}', identity)) == 200
    denied(matrix, identity, 'PUT setting primary_color', 'upload logo')


def test_setting_without_a_permission_is_for_the_administrator(matrix):
    def setup(ids):
        SystemSettingsModel.set_setting('legacy_setting', 'x', category='general')
    case = Case('legacy', 'PUT', f'{API}/admin/settings/key/legacy_setting', {'value': 'y'}, setup=setup)
    assert status(matrix.call(case, 'administrator')[0]) == 200
    assert matrix.call(case, 'settings_admin')[0] == '403|Settings admin access required'
    assert status(matrix.call(case, custom_identity(matrix, 'settings.general.edit'))[0]) == 403


def test_events_links_reveal(matrix):
    identity = custom_identity(matrix, 'events.view', 'events.links.view', 'events.links.reveal')
    allowed(matrix, identity, 'event link url', 'event links')
    denied(matrix, identity, 'update event link', 'delete event link')

    identity = custom_identity(matrix, 'events.view', 'events.links.view')
    allowed(matrix, identity, 'event links')
    denied(matrix, identity, 'event link url')


def test_events_team_edit_does_not_set_the_leader(matrix):
    identity = custom_identity(matrix, 'events.teams.edit')
    cell = get(matrix, 'update event team leader field', identity)
    assert status(cell) == 200 and probe(cell)['leader'] is None
    assert probe(get(matrix, 'update event team leader field', 'events_admin'))['leader'] == matrix.ids['target']
    denied(matrix, identity, 'create team leader account', 'designate team leader')


def test_onboarding_fields(matrix):
    identity = custom_identity(matrix, 'onboarding.edit')
    plain = probe(get(matrix, 'update code basics', 'administrator'))
    for case in ('update code roles', 'update code approver', 'update code groups'):
        cell = get(matrix, case, identity)
        assert status(cell) == 200
        assert {k: probe(cell)[k] for k in ('roles', 'approver', 'groups')} == \
            {k: plain[k] for k in ('roles', 'approver', 'groups')}, case
    assert probe(get(matrix, 'create code with roles/groups', identity)) == {'roles': [], 'groups': [], 'approver': None, 'requireApproval': False}

    identity = custom_identity(matrix, 'onboarding.edit', 'onboarding.roles.assign')
    assert probe(get(matrix, 'update code roles', identity))['roles'] == []
    assert probe(get(matrix, 'update code groups', identity))['groups'] == plain['groups']


def test_tak_profile_roles(matrix):
    identity = custom_identity(matrix, 'tak_profiles.edit')
    assert probe(get(matrix, 'update profile roles', identity))['roles'] == []
    identity = custom_identity(matrix, 'tak_profiles.edit', 'tak_profiles.assign')
    assert probe(get(matrix, 'update profile roles', identity))['roles'] == ['matrix_audience']

    # For the others a form without roles clears them; without the permission it must not
    def roles_left(resp, ids):
        return json.dumps(sorted(r.name for r in db.session.get(TakProfileModel, ids['profile_role']).roles))
    save = Case('save', 'PUT', f'{API}/tak-profiles/{{profile_role}}', form={'description': 'x'}, probe=roles_left)
    assert probe(matrix.call(save, custom_identity(matrix, 'tak_profiles.edit'))[0]) == ['matrix_audience']
    assert probe(matrix.call(save, 'tak_profile_admin')[0]) == []


def test_announcements_dispatch_needs_send(matrix):
    identity = custom_identity(matrix, 'announcements.edit')
    assert probe(get(matrix, 'create announcement draft', identity))['status'] == 'draft'
    assert probe(get(matrix, 'update announcement draft', identity))['title'] == 'edited'
    denied(matrix, identity, 'create announcement send now', 'create announcement scheduled',
           'update announcement to scheduled', 'update scheduled announcement', 'send announcement')

    identity = custom_identity(matrix, 'announcements.edit', 'announcements.send')
    assert probe(get(matrix, 'create announcement send now', identity))['emailStatus'] == 'pending'
    assert probe(get(matrix, 'update announcement to scheduled', identity))['status'] == 'scheduled'
    assert probe(get(matrix, 'send announcement', identity))['status'] == 'sent'


def test_announcement_email_details(matrix):
    identity = custom_identity(matrix, 'announcements.view')
    assert matrix.call(matrix.case('get announcement (admin)'), identity)[1].get_json()['emailLastError'] is None
    identity = custom_identity(matrix, 'announcements.view', 'announcements.email_details')
    assert matrix.call(matrix.case('get announcement (admin)'), identity)[1].get_json()['emailLastError'] == 'raw smtp error'


# ---------------------------------------------------------------- what a key needs

def test_every_requirement_names_a_real_key():
    for key, needed in REQUIRES.items():
        assert key in ALL_PERMISSIONS and needed, key
        assert set(needed) <= ALL_PERMISSIONS, key


def test_built_in_roles_meet_their_own_requirements():
    for role, held in BUILTIN_ROLE_PERMISSIONS.items():
        assert enforce_requires(held) == held, role


def test_an_assignable_key_only_needs_assignable_keys():
    for key in ASSIGNABLE_PERMISSIONS:
        assert set(requirements_of(key)) <= ASSIGNABLE_PERMISSIONS, key


def test_every_key_can_be_met():
    for key in ASSIGNABLE_PERMISSIONS:
        keys, todo = set(), [key]
        while todo:
            current = todo.pop()
            if current not in keys:
                keys.add(current)
                todo.extend(requirements_of(current))
        assert key in enforce_requires(keys), key


@pytest.mark.parametrize('keys,left', [
    (['users.groups.assign'], []),
    (['events.links.reveal'], []),
    (['events.links.reveal', 'events.links.view'], []),  # nothing holds events.view, so nothing is left
    (['events.view', 'events.links.reveal'], ['events.view']),
    (['onboarding.roles.assign', 'onboarding.view'], ['onboarding.view']),
    (['tak_profiles.assign'], []),
])
def test_requirements_are_enforced_when_resolving(matrix, keys, left):
    assert effective(matrix, custom_identity(matrix, *keys))['permissions'] == left


def test_a_user_field_key_cannot_work_blind(matrix):
    identity = custom_identity(matrix, 'users.groups.assign')
    assert status(get(matrix, 'update other groups', identity)) == 403
    assert status(get(matrix, 'get other user', identity)) == 403
    # Without any right on others, their own groups stay out of reach too
    cell = get(matrix, 'update self groups', identity)
    assert status(cell) == 200 and probe(cell)['groups'] == []


def test_another_role_can_meet_a_requirement(matrix):
    identity = custom_identity(matrix, 'users.groups.assign')
    with matrix.app.app_context():
        user = db.session.get(UserModel, matrix.user_ids[identity])
        user.roles.append(UserRoleModel.query.filter_by(name='user_readonly').first())
        db.session.commit()
        db.session.remove()
    matrix.snapshot()
    assert probe(get(matrix, 'update other groups', identity))['groups'] == []


# ---------------------------------------------------------------- review fixes

def test_group_members_only_with_members_view(matrix):
    body = matrix.call(matrix.case('get group'), custom_identity(matrix, 'groups.view'))[1].get_json()
    assert 'users' not in body and body['name'] == 'matrix_group'
    body = matrix.call(matrix.case('get group'), custom_identity(matrix, 'groups.view', 'groups.members.view'))[1].get_json()
    assert body['users'][0]['email'] == 'matrix_target@example.com'
    for role in ('group_readonly', 'group_admin', 'administrator'):
        assert matrix.call(matrix.case('get group'), role)[1].get_json()['users'][0]['username'] == 'matrix_target'


def test_email_of_another_user_needs_password_reset_too(matrix):
    identity = custom_identity(matrix, 'users.accounts.edit')
    cell, resp = matrix.call(matrix.case('update other email'), identity)
    assert status(cell) == 200 and probe(cell)['email'] == 'matrix_target@example.com'
    assert resp.get_json()['ignoredFields'] == ['email']
    identity = custom_identity(matrix, 'users.accounts.view', 'users.password.reset')
    assert probe(get(matrix, 'update other email', identity))['email'] == 'matrix_target@example.com'
    identity = custom_identity(matrix, 'users.accounts.edit', 'users.password.reset')
    cell, resp = matrix.call(matrix.case('update other email'), identity)
    assert probe(cell)['email'] == 'new@example.com' and 'ignoredFields' not in resp.get_json()
    # Your own stays yours
    assert probe(get(matrix, 'update self email', NO_ROLE))['emailChanged'] is True
    assert probe(get(matrix, 'update other email', 'user_admin'))['email'] == 'new@example.com'


def test_approval_flags_need_onboarding_roles_assign(matrix):
    identity = custom_identity(matrix, 'onboarding.edit')
    cell, resp = matrix.call(matrix.case('update code approval flags'), identity)
    assert status(cell) == 200 and (probe(cell)['requireApproval'], probe(cell)['autoApprove']) == (False, False)
    assert resp.get_json()['ignoredFields'] == ['autoApprove', 'requireApproval']
    assert probe(get(matrix, 'create code with approval flags', identity))['requireApproval'] is False

    identity = custom_identity(matrix, 'onboarding.edit', 'onboarding.roles.assign')
    cell = get(matrix, 'update code approval flags', identity)
    assert (probe(cell)['requireApproval'], probe(cell)['autoApprove']) == (True, True)
    assert probe(get(matrix, 'create code with approval flags', identity))['requireApproval'] is True


def test_deleting_a_team_needs_events_delete(matrix):
    identity = custom_identity(matrix, 'events.teams.edit')
    denied(matrix, identity, 'delete event team')
    allowed(matrix, identity, 'update event team', 'create event team')
    allowed(matrix, custom_identity(matrix, 'events.view', 'events.delete'), 'delete event team')
    assert 'S' in CATALOGUE['events']['events.teams.edit']


def test_sensitive_settings_keys_are_flagged():
    assert 'S' in CATALOGUE['settings']['settings.general.edit']  # installer QR URLs
    assert 'S' in CATALOGUE['settings']['settings.enrollment.edit']  # kiosk_enrollment_enabled


def test_tak_profile_public_needs_assign(matrix):
    identity = custom_identity(matrix, 'tak_profiles.edit')
    cell, resp = matrix.call(matrix.case('update profile public'), identity)
    assert status(cell) == 200 and probe(cell)['isPublic'] is True
    assert resp.get_json()['ignoredFields'] == ['isPublic']
    cell, resp = matrix.call(matrix.case('create profile public'), identity)
    assert status(cell) == 201 and probe(cell)['isPublic'] is False
    assert resp.get_json()['ignoredFields'] == ['isPublic']

    identity = custom_identity(matrix, 'tak_profiles.edit', 'tak_profiles.assign')
    assert probe(get(matrix, 'update profile public', identity))['isPublic'] is False
    assert probe(get(matrix, 'create profile public', identity))['isPublic'] is True


def test_group_routes_hide_channel_urls_without_channels_view(matrix):
    groups_only = custom_identity(matrix, 'meshtastic.groups.view')
    denied(matrix, groups_only, 'ungrouped channels')
    body = matrix.call(matrix.case('meshtastic groups admin'), groups_only)[1].get_json()
    private = next(g for g in body['groups'] if g['name'] == 'private')
    assert private['channels'][0]['name'] == 'role-bound' and private['channels'][0]['url'] is None
    assert private['combined_url'] is None
    body = matrix.call(matrix.case('get meshtastic group private'), groups_only)[1].get_json()
    assert body['channels'][0]['url'] is None and body['combined_url'] is None

    both = custom_identity(matrix, 'meshtastic.groups.view', 'meshtastic.channels.view')
    allowed(matrix, both, 'ungrouped channels')
    body = matrix.call(matrix.case('get meshtastic group private'), both)[1].get_json()
    assert body['channels'][0]['url'] == 'https://meshtastic.org/e/#BBBB'

    # Who may see the group anyway keeps the URLs: everyone on a public group
    for identity in (groups_only, NO_ROLE):
        body = matrix.call(matrix.case('get meshtastic group public'), identity)[1].get_json()
        assert body['channels'][0]['url'] == 'https://meshtastic.org/e/#AAAA'


def test_group_writes_need_the_channels(matrix):
    # Group edit requires channel view (REQUIRES): without it the key does not count, so no write shows a URL
    identity = custom_identity(matrix, 'meshtastic.groups.edit')
    assert matrix.call(matrix.case('regenerate group url'), identity)[1].status_code == 403
    with_channels = custom_identity(matrix, 'meshtastic.channels.view', 'meshtastic.groups.edit')
    assert matrix.call(matrix.case('regenerate group url'), with_channels)[1].get_json()['combined_url'] is not None
    assert matrix.call(matrix.case('regenerate group url'), 'meshtastic_admin')[1].get_json()['combined_url'] is not None


def test_ignored_fields_only_when_there_are_any(matrix):
    for case, identity in (('update other roles', 'user_admin'), ('update radio assignment', 'radio_admin'),
                           ('update code roles', 'onboarding_admin'), ('update profile roles', 'tak_profile_admin'),
                           ('update event team leader field', 'events_admin')):
        assert 'ignoredFields' not in matrix.call(matrix.case(case), identity)[1].get_json(), case
    assert matrix.call(matrix.case('update self roles'), NO_ROLE)[1].get_json()['ignoredFields'] == ['roleIds']
    radio = custom_identity(matrix, 'radios.edit')
    assert matrix.call(matrix.case('update radio assignment'), radio)[1].get_json()['ignoredFields'] == ['assignedTo', 'owner']
    assert matrix.call(matrix.case('create radio with assignment'), radio)[1].get_json()['ignoredFields'] == ['assignedTo', 'owner']
    team = custom_identity(matrix, 'events.teams.edit')
    assert matrix.call(matrix.case('update event team leader field'), team)[1].get_json()['ignoredFields'] == ['leaderUserId']
    codes = custom_identity(matrix, 'onboarding.edit')
    assert matrix.call(matrix.case('create code with roles/groups'), codes)[1].get_json()['ignoredFields'] == \
        ['approverRoleId', 'groupIds', 'roleIds']
    profile = custom_identity(matrix, 'tak_profiles.edit')
    assert matrix.call(matrix.case('update profile roles'), profile)[1].get_json()['ignoredFields'] == ['roleIds[]']
    user = custom_identity(matrix, 'users.accounts.view', 'users.groups.assign')
    assert matrix.call(matrix.case('update other basics'), user)[1].get_json()['ignoredFields'] == ['firstName']


def test_radios_program_is_hidden_without_meshtastic(matrix):
    case = Case('mesh off', 'GET', f'{API}/auth/permissions', setup=_disable('meshtastic'))
    body = effective(matrix, 'radio_readonly', case)
    assert body['permissions'] == ['radios.claim_code', 'radios.view']
    assert body['modules'] == ['radios']
    off = Case('program, mesh off', 'POST', f'{API}/radios/{{radio_other}}/program-config', {}, setup=_disable('meshtastic'))
    assert matrix.call(off, 'radio_readonly')[1].get_json()['code'] == 'MODULE_DISABLED'


def test_enabled_modules_are_read_once(matrix):
    from sqlalchemy import event as sa_event
    with matrix.app.app_context():
        engine = db.engine
    statements = []

    def record(conn, cursor, statement, *args):
        if 'system_settings' in statement:
            statements.append(statement)
    sa_event.listen(engine, 'before_cursor_execute', record)
    try:
        effective(matrix, 'administrator')
    finally:
        sa_event.remove(engine, 'before_cursor_execute', record)
    # Plus one of leader_designations_for, which checks the events module for its own part (33 before)
    assert len(statements) == len(MODULE_SETTINGS) + 1


def test_audience_stays_with_the_role_id(matrix):
    """A custom role with permissions is still an audience by its id, not by what it may do"""
    identity = custom_identity(matrix, 'tak_profiles.view', 'meshtastic.channels.view')
    assert status(get(matrix, 'profile files', identity)) == 200
    denied(matrix, identity, 'get profile private', 'approval approve')


# ---------------------------------------------------------------- sessions

def test_event_leader_session_holds_nothing(matrix):
    """The account behind it holds the administrator role in the database"""
    body = effective(matrix, 'event_leader')
    assert (body['permissions'], body['modules']) == ([], [])


def test_kiosk_session_holds_nothing(matrix):
    """The account behind it holds the administrator role in the database"""
    # /auth/permissions is not on the kiosk allowlist, so ask the resolver directly
    with matrix.app.test_request_context(headers={'Authorization': f'Bearer {matrix.tokens["kiosk"]}'}):
        verify_jwt_in_request()
        assert rbac.get_effective_permissions() == frozenset()
        db.session.remove()
    assert matrix.call(matrix.case('list radios'), 'kiosk')[1].get_json()['radios'] == []


def test_impersonation_resolves_to_the_target(matrix):
    cell, resp = matrix.request(matrix.case('impersonate'), 'administrator')
    try:
        assert cell == '200'
        headers = {'Authorization': f"Bearer {resp.get_json()['access_token']}"}
        body = matrix.app.test_client().get(f'{API}/auth/permissions', headers=headers).get_json()
        assert body['permissions'] == []
        assert matrix.app.test_client().get(f'{API}/users', headers=headers).status_code == 403
    finally:
        matrix.restore()


def test_impersonating_a_role_holder_gets_that_role(matrix):
    target = matrix.user_ids['radio_admin']
    case = Case('impersonate radio admin', 'POST', f'{API}/auth/impersonate/{target}')
    cell, resp = matrix.request(case, 'administrator')
    try:
        assert cell == '200'
        headers = {'Authorization': f"Bearer {resp.get_json()['access_token']}"}
        body = matrix.app.test_client().get(f'{API}/auth/permissions', headers=headers).get_json()
        assert body['permissions'] == sorted(BUILTIN_ROLE_PERMISSIONS['radio_admin'])
        assert matrix.app.test_client().get(f'{API}/admin/settings', headers=headers).status_code == 403
    finally:
        matrix.restore()


def test_demotion_applies_to_an_old_token(matrix):
    user_id = matrix.user_ids['user_admin']

    def demote(ids):
        user = db.session.get(UserModel, user_id)
        user.roles.clear()
        db.session.commit()
    allowed(matrix, 'user_admin', 'list users')
    assert status(matrix.call(Case('demoted', 'GET', f'{API}/users', setup=demote), 'user_admin')[0]) == 403


def test_removing_a_row_applies_to_an_old_token(matrix):
    identity = custom_identity(matrix, 'groups.sync', 'groups.view')
    role_name = f'perm_{identity}'

    def remove(ids):
        role = UserRoleModel.query.filter_by(name=role_name).first()
        RolePermissionModel.query.filter_by(role_id=role.id, permission='groups.view').delete()
        db.session.commit()
    assert status(matrix.call(Case('row gone', 'GET', f'{API}/groups', setup=remove), identity)[0]) == 403
    allowed(matrix, identity, 'list groups')


def test_resolved_once_per_request(matrix):
    with mock.patch.object(rbac, '_resolve_permissions', wraps=rbac._resolve_permissions) as resolve:
        assert status(get(matrix, 'update other basics', 'user_admin')) == 200
    assert resolve.call_count == 1


def test_a_new_request_context_does_not_inherit_permissions(matrix):
    with matrix.app.test_request_context(headers={'Authorization': f'Bearer {matrix.tokens["administrator"]}'}):
        verify_jwt_in_request()
        assert rbac.has_permission('users.impersonate')
        db.session.remove()
    with matrix.app.test_request_context(headers={'Authorization': f'Bearer {matrix.tokens[NO_ROLE]}'}):
        verify_jwt_in_request()
        assert not rbac.has_permission('users.impersonate')
        db.session.remove()
