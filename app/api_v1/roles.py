"""
Roles API endpoints
CRUD operations for role management, and the permissions of custom roles
"""

from flask import request, jsonify
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy.exc import IntegrityError
from app.api_v1 import api_v1
from app.models import RolePermissionModel, UserRoleModel, db
from app.pagination import search_and_paginate
from app.permissions import ALL_PERMISSIONS, ASSIGNABLE_PERMISSIONS, catalogue_for_api, enforce_requires, with_implied_views
from app.rbac import (ADMIN_ROLES, enabled_modules, get_effective_permissions, has_permission, is_administrator, is_custom_role_name,
                      is_privileged_role, is_reserved_role_name, permission_denied, require_any_permission, require_permission,
                      role_beyond_actor, role_permission_keys)

RESERVED_NAME_ERROR = 'This role name is reserved'
SYSTEM_ROLE_ERROR = 'Only a custom role can be given permissions'
ROLE_LOCKED_ERROR = 'This role holds permissions you do not hold, so you cannot change it'
GRANT_BEYOND_ACTOR_ERROR = 'You cannot give a role permissions you do not hold'
WAKES_BEYOND_ACTOR_ERROR = ('This change would make permissions of other roles take effect for users or onboarding codes '
                            'holding this role, and you do not hold those permissions')
# The description the login sync gives a role it creates from OTS (app/api_v1/auth.py), unchanged since 1.0.0
OTS_ROLE_DESCRIPTION = 'Auto-created from OTS'
# A role given that description would be locked as if it came from OTS
OTS_DESCRIPTION_ERROR = 'This description is reserved for roles created from OpenTAK Server'


def is_protected_role(role):
    """
    Check if a role is protected from editing/deletion.
    Protected roles include:
    - Admin roles defined in ADMIN_ROLES
    - Roles imported from OTS (identified by the exact description the login sync gives them)
    """
    # Admin roles are always protected
    if role.name in ADMIN_ROLES:
        return True
    # Other privileged rows (e.g. "admin"): only an administrator may change them
    if is_privileged_role(role) and not is_administrator():
        return True
    # Roles auto-created from OTS; a description that merely mentions OTS does not lock a role
    if role.description == OTS_ROLE_DESCRIPTION:
        return True
    return False


def is_custom_role(role):
    """Can this role be given permissions: not a built-in, reserved name or role that came from OTS"""
    return is_custom_role_name(role.name) and not is_protected_role(role)


def _role_lock_fields(role):
    """Whether the current user can change this role's permissions; shows nothing of what the role holds"""
    beyond_actor = role_beyond_actor(role)
    custom = is_custom_role(role)
    locked_reason = 'system' if not custom else ('aboveLevel' if beyond_actor else None)
    return {
        'isProtected': is_protected_role(role),
        'isCustom': custom,
        'locked': locked_reason is not None,
        'lockedReason': locked_reason,
        'assignable': not beyond_actor,
    }


def _role_viewer_fields(role):
    return {
        'permissions': sorted(role_permission_keys(role)),
        'userCount': len(role.users),
        'codeCount': len(role.onboarding_codes),
    }


@api_v1.route('/roles', methods=['GET'])
@jwt_required()
def get_roles():
    """Get roles for any admin (admin pages use this as a picker), optionally filtered by search and
    paginated (all roles when per_page is omitted). What roles hold and who holds them only with access
    to the roles module."""
    error = require_any_permission()
    if error:
        return error

    show_details = has_permission('roles.view')
    roles, meta = search_and_paginate(
        UserRoleModel.query, UserRoleModel.name,
        [UserRoleModel.name, UserRoleModel.display_name, UserRoleModel.description])

    return jsonify({
        'roles': [{
            'id': role.id,
            'name': role.name,
            'displayName': role.display_name,
            'description': role.description,
            **_role_lock_fields(role),
            **(_role_viewer_fields(role) if show_details else {}),
        } for role in roles],
        **meta
    }), 200


@api_v1.route('/roles/permission-catalog', methods=['GET'])
@jwt_required()
@require_permission('roles.view')
def get_permission_catalog():
    """What a role can hold, for the role editor. grantable: the current user may tick this key."""
    held = get_effective_permissions()
    modules = catalogue_for_api()
    for module in modules:
        module['enabled'] = module['module'] in enabled_modules()
        for function in module['functions']:
            for permission in function['permissions']:
                permission['grantable'] = permission['key'] in held and permission['key'] in ASSIGNABLE_PERMISSIONS
    return jsonify({'modules': modules}), 200


@api_v1.route('/roles/<int:role_id>', methods=['GET'])
@jwt_required()
@require_permission('roles.view')
def get_role(role_id):
    """Get role by ID with details"""
    role = UserRoleModel.get_by_id(role_id)
    if not role:
        return jsonify({'error': 'Role not found'}), 404

    return jsonify({
        'id': role.id,
        'name': role.name,
        'displayName': role.display_name,
        'description': role.description,
        **_role_lock_fields(role),
        **_role_viewer_fields(role),
        'users': [{'id': u.id, 'username': u.username} for u in role.users],
        'takProfiles': [{'id': tp.id, 'name': tp.name} for tp in role.takprofiles],
        'meshtastic': [{'id': m.id, 'name': m.name} for m in role.meshtastic]
    }), 200


@api_v1.route('/roles', methods=['POST'])
@jwt_required()
@require_permission('roles.edit')
def create_role():
    """Create a new role (admin only)"""
    data = request.get_json()

    if not data.get('name'):
        return jsonify({'error': 'Name is required'}), 400

    if is_reserved_role_name(data['name']):
        return jsonify({'error': RESERVED_NAME_ERROR}), 400
    if data.get('description') == OTS_ROLE_DESCRIPTION:
        return jsonify({'error': OTS_DESCRIPTION_ERROR}), 400

    # Check if role already exists
    existing = UserRoleModel.get_role_by_name(data['name'])
    if existing:
        return jsonify({'error': 'Role already exists'}), 409

    try:
        role = UserRoleModel.create_role(
            name=data['name'],
            description=data.get('description', ''),
            display_name=data.get('displayName')
        )

        return jsonify({
            'message': 'Role created successfully',
            'role': {
                'id': role.id,
                'name': role.name,
                'displayName': role.display_name,
                'description': role.description
            }
        }), 201

    except Exception as e:
        return jsonify({'error': f'Failed to create role: {str(e)}'}), 400


def _beyond_actor_denied(role):
    if role_beyond_actor(role):
        return jsonify({'error': ROLE_LOCKED_ERROR, 'missingPermissions': sorted(role_permission_keys(role) - get_effective_permissions())}), 403
    return None


@api_v1.route('/roles/<int:role_id>', methods=['PUT'])
@jwt_required()
@require_permission('roles.edit')
def update_role(role_id):
    """Update role (admin only)"""
    role = UserRoleModel.get_by_id(role_id)
    if not role:
        return jsonify({'error': 'Role not found'}), 404

    # Check if role is protected
    if is_protected_role(role):
        return jsonify({'error': 'This role is protected and cannot be modified'}), 403
    denied = _beyond_actor_denied(role)
    if denied:
        return denied

    data = request.get_json()
    # A protected role never gets here, so this would be a change to the OTS description
    if data.get('description') == OTS_ROLE_DESCRIPTION:
        return jsonify({'error': OTS_DESCRIPTION_ERROR}), 400

    if data.get('name'):
        # An existing row keeps working under its own name, whatever that is
        if data['name'] != role.name and is_reserved_role_name(data['name']):
            return jsonify({'error': RESERVED_NAME_ERROR}), 400

        # Check if new name conflicts with existing role
        existing = UserRoleModel.get_role_by_name(data['name'])
        if existing and existing.id != role_id:
            return jsonify({'error': 'Role name already exists'}), 409
        role.name = data['name']

    if 'description' in data:
        role.description = data['description']

    if 'displayName' in data:
        role.display_name = data['displayName']

    try:
        UserRoleModel.update(role)

        return jsonify({
            'message': 'Role updated successfully',
            'role': {
                'id': role.id,
                'name': role.name,
                'displayName': role.display_name,
                'description': role.description
            }
        }), 200

    except Exception as e:
        return jsonify({'error': f'Failed to update role: {str(e)}'}), 400


@api_v1.route('/roles/<int:role_id>', methods=['DELETE'])
@jwt_required()
@require_permission('roles.delete')
def delete_role(role_id):
    """Delete role (admin only)"""
    role = UserRoleModel.get_by_id(role_id)
    if not role:
        return jsonify({'error': 'Role not found'}), 404

    # Check if role is protected
    if is_protected_role(role):
        return jsonify({'error': 'This role is protected and cannot be deleted'}), 403
    denied = _beyond_actor_denied(role)
    if denied:
        return denied

    try:
        UserRoleModel.delete_by_id(role_id)
        return jsonify({'message': 'Role deleted successfully'}), 200

    except Exception as e:
        return jsonify({'error': f'Failed to delete role: {str(e)}'}), 400


def _checked_permission_change(role_id):
    """For a write of a role's full permission list: (role, keys it would hold, None), or (None, None, error response).

    Refuses what is not a custom role, a role above the user's level (so a key they do not hold is never
    removed either), keys that do not exist or are administrator only, and keys the user does not hold.
    """
    def refuse(status, error, **extra):
        return None, None, (jsonify({'error': error, **extra}), status)

    role = UserRoleModel.get_by_id(role_id)
    if not role:
        return refuse(404, 'Role not found')
    if not is_custom_role(role):
        return refuse(403, SYSTEM_ROLE_ERROR)
    denied = _beyond_actor_denied(role)
    if denied:
        return None, None, denied

    data = request.get_json(silent=True)
    keys = data.get('permissions') if isinstance(data, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        return refuse(400, 'permissions must be a list of permission keys')
    unknown = set(keys) - ALL_PERMISSIONS
    if unknown:
        return refuse(400, 'Unknown permissions', permissions=sorted(unknown))
    administrator_only = set(keys) - ASSIGNABLE_PERMISSIONS
    if administrator_only:
        return refuse(400, 'Only the administrator role can hold these permissions', permissions=sorted(administrator_only))

    # Stored with the view an edit key implies, so the list always says what the role really holds
    keys = with_implied_views(keys)
    missing = keys - get_effective_permissions()
    if missing:
        return refuse(403, GRANT_BEYOND_ACTOR_ERROR, missingPermissions=sorted(missing))
    woken = _woken_for_holders(role, keys) - get_effective_permissions()
    if woken:
        return refuse(403, WAKES_BEYOND_ACTOR_ERROR, missingPermissions=sorted(woken))
    return role, keys, None


def _woken_for_holders(role, new_keys):
    """Keys that start to count for a holder of this role (a user, or an account from an onboarding code) when the
    role gets these keys: a key another role of the holder stores, that only counts once its requirement is met
    (see enforce_requires), wakes up when this role adds that requirement."""
    old_keys = role_permission_keys(role)
    if not new_keys - old_keys:
        return set()
    woken = set()
    for holder in list(role.users) + list(role.onboarding_codes):
        others = set().union(*(role_permission_keys(r) for r in holder.roles if r.id != role.id))
        woken |= enforce_requires(others | new_keys) - enforce_requires(others | old_keys)
    return woken


def _change_impact(role, new_keys):
    """What changes for whom when this role gets these keys: holders only count if something changes"""
    current = role_permission_keys(role)
    added, removed = new_keys - current, current - new_keys
    return {
        'added': sorted(added),
        'removed': sorted(removed),
        'userCount': len(role.users) if added or removed else 0,
        'codeCount': len(role.onboarding_codes) if added or removed else 0,
    }


@api_v1.route('/roles/<int:role_id>/permissions', methods=['PUT'])
@jwt_required()
@require_permission('roles.permissions.edit')
def set_role_permissions(role_id):
    """Replace the permissions of a custom role with this full list"""
    role, keys, error = _checked_permission_change(role_id)
    if error:
        return error

    impact = _change_impact(role, keys)
    # Row by row: a replaced list would insert before it deletes, and trip the unique pair
    rows = {row.permission: row for row in role.permissions}
    for key, row in rows.items():
        if key not in keys:
            role.permissions.remove(row)
    for key in keys - set(rows):
        role.permissions.append(RolePermissionModel(permission=key))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'The permissions of this role were changed at the same time; try again'}), 409
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Failed to update the permissions: {str(e)}'}), 400

    return jsonify({
        'message': 'Role permissions updated successfully',
        'permissions': sorted(role_permission_keys(role)),
        'impact': impact,
    }), 200


@api_v1.route('/roles/<int:role_id>/permissions/preview', methods=['POST'])
@jwt_required()
@require_permission('roles.permissions.edit')
def preview_role_permissions(role_id):
    """What PUT /roles/<id>/permissions would do with this list, without doing it; refuses what it would refuse"""
    role, keys, error = _checked_permission_change(role_id)
    if error:
        return error
    return jsonify({'permissions': sorted(keys), 'impact': _change_impact(role, keys)}), 200


@api_v1.route('/roles/<int:role_id>/duplicate', methods=['POST'])
@jwt_required()
@require_permission('roles.edit')
def duplicate_role(role_id):
    """A new custom role with the permissions of this one, under a new name. Audiences (TAK profiles,
    Meshtastic, announcements) and holders are not copied. All or nothing: a copy the user cannot fully
    grant is refused rather than silently left short. Administrator-only keys are never copied."""
    if not has_permission('roles.permissions.edit'):
        return permission_denied('roles.permissions.edit')

    source = UserRoleModel.get_by_id(role_id)
    if not source:
        return jsonify({'error': 'Role not found'}), 404

    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    name = data.get('name')
    if not name or not isinstance(name, str):
        return jsonify({'error': 'Name is required'}), 400
    if is_reserved_role_name(name):
        return jsonify({'error': RESERVED_NAME_ERROR}), 400
    if data.get('description') == OTS_ROLE_DESCRIPTION:
        return jsonify({'error': OTS_DESCRIPTION_ERROR}), 400
    if UserRoleModel.get_role_by_name(name):
        return jsonify({'error': 'Role already exists'}), 409

    keys = role_permission_keys(source) & ASSIGNABLE_PERMISSIONS
    missing = keys - get_effective_permissions()
    if missing:
        return jsonify({'error': GRANT_BEYOND_ACTOR_ERROR, 'missingPermissions': sorted(missing)}), 403

    # The OTS description would lock the copy (see is_protected_role)
    default_description = '' if source.description == OTS_ROLE_DESCRIPTION else source.description
    role = UserRoleModel(name=name, display_name=data.get('displayName'),
                         description=data.get('description', default_description))
    role.permissions = [RolePermissionModel(permission=key) for key in sorted(keys)]
    db.session.add(role)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Role already exists'}), 409
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Failed to duplicate role: {str(e)}'}), 400

    return jsonify({
        'message': 'Role duplicated successfully',
        'role': {
            'id': role.id,
            'name': role.name,
            'displayName': role.display_name,
            'description': role.description,
            'permissions': sorted(keys),
        }
    }), 201
