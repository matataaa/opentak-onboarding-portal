"""
Users API endpoints
CRUD operations for user management
"""

from flask import request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.api_v1 import api_v1
from app.models import UserModel, UserRoleModel, OTSGroupModel, GroupUserAssociation, db
from app.exceptions import BadRequestError, OTSRoleUnconfirmed
from app.ots import OTSClient, delete_user_if_present, new_password_error, set_ots_admin
from app.rbac import (ROLE_BEYOND_ACTOR_ERROR, SERVICE_ACCOUNT_PASSWORD_ERROR, TARGET_BEYOND_ACTOR_ERROR, deny_beyond_actor,
                      drop_fields_without_permission, has_permission, has_privileged_role, ignored_fields_body, is_administrator,
                      is_last_administrator, is_privileged_role, is_service_account, require_permission,
                      role_ids_include_privileged, roles_from_ids)
from app.settings import OTS_URL, OTS_USERNAME, OTS_PASSWORD, OTS_VERIFY_SSL
from datetime import datetime
from sqlalchemy.exc import IntegrityError
import re


PRIVILEGED_ROLE_ERROR = 'Only an administrator can grant or remove the administrator role'
PRIVILEGED_TARGET_ERROR = 'Only an administrator can change a user with the administrator role'


def _set_ots_admin(username, privileged):
    set_ots_admin(OTSClient(OTS_URL, OTS_USERNAME, OTS_PASSWORD), username, privileged)


def _restore_ots_admin(username, privileged):
    """OTS may have been changed while the local change failed: put the OTS role back.

    Returns True if the role is confirmed back, False if OTS and the portal may differ.
    """
    try:
        _set_ots_admin(username, privileged)
        current_app.logger.warning(f"Restored the OTS role of '{username}' after a failed update")
        return True
    except Exception as e:
        current_app.logger.error(f"Could not restore the OTS role of '{username}'; OTS and the portal now differ: {e}")
        return False


@api_v1.route('/users', methods=['GET'])
@jwt_required()
@require_permission('users.accounts.view')
def get_users():
    """
    Get all users (admin only)

    Query parameters:
    - page: int (default: 1)
    - per_page: int (default: 50)
    - search: string (search username, email, callsign)

    Response:
    {
        "users": [
            {
                "id": "int",
                "username": "string",
                "email": "string",
                "callsign": "string",
                ...
            }
        ],
        "total": "int",
        "page": "int",
        "per_page": "int"
    }
    """
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    search = request.args.get('search', '')

    query = UserModel.query

    if search:
        search_filter = f'%{search}%'
        query = query.filter(
            (UserModel.username.ilike(search_filter)) |
            (UserModel.email.ilike(search_filter)) |
            (UserModel.callsign.ilike(search_filter))
        )

    total = query.count()
    users = query.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        'users': [{
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'firstName': user.firstName,
            'lastName': user.lastName,
            'callsign': user.callsign,
            'roles': [{'name': role.name, 'displayName': role.display_name} for role in user.roles],
            'groups': [{'id': a.group.id, 'name': a.group.name, 'displayName': a.group.display_name, 'direction': a.direction} for a in user.group_associations],
            'expiryDate': user.expiryDate.isoformat() if user.expiryDate else None,
            'onboardedBy': user.onboardedBy
        } for user in users],
        'total': total,
        'page': page,
        'per_page': per_page
    }), 200


@api_v1.route('/users/<int:user_id>', methods=['GET'])
@jwt_required()
def get_user(user_id):
    """
    Get user by ID

    Response includes full user details with relationships
    """
    current_user_id = get_jwt_identity()

    # Users can view their own profile, admins and user readonly can view any
    is_user_viewer = has_permission('users.accounts.view')
    if not is_user_viewer and int(current_user_id) != user_id:
        return jsonify({'error': 'Permission denied'}), 403

    user = UserModel.get_user_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    return jsonify({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'firstName': user.firstName,
        'lastName': user.lastName,
        'callsign': user.callsign,
        'roles': [{
            'id': role.id,
            'name': role.name,
            'displayName': role.display_name,
            'description': role.description
        } for role in user.roles],
        'takProfiles': [{
            'id': tp.id,
            'name': tp.name,
            'description': tp.description
        } for tp in user.takprofiles],
        'meshtastic': [{
            'id': m.id,
            'name': m.name,
            'description': m.description
        } for m in user.meshtastic],
        'groups': [{
            'id': g.id,
            'name': g.name,
            'displayName': g.display_name
        } for g in user.ots_groups],
        'assignedRadios': [{
            'id': r.id,
            'name': r.name,
            'platform': r.platform,
            'radioType': r.radioType
        } for r in UserModel.query.get(user_id).radios_assigned.all()] if hasattr(UserModel.query.get(user_id), 'radios_assigned') else [],
        'expiryDate': user.expiryDate.isoformat() if user.expiryDate else None,
        'onboardedBy': user.onboardedBy,
        'language': user.language or 'en'
    }), 200


def _group_request_error(data):
    """What is wrong with groups/groupIds, or None. Only real ints: '5' would count as another group than 5."""
    def bad_ids(ids):
        return any(type(gid) is not int for gid in ids) or len(set(ids)) != len(ids)

    if 'groupIds' in data and (not isinstance(data['groupIds'], list) or bad_ids(data['groupIds'])):
        return 'groupIds must be a list of different integer ids'
    if 'groups' in data:
        groups = data['groups']
        if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
            return 'groups must be a list of objects with an id and a valid direction'
        if bad_ids([g.get('id') for g in groups]) or any(g.get('direction', 'BOTH') not in ('BOTH', 'IN', 'OUT') for g in groups):
            return 'groups must be a list of objects with a different integer id and a valid direction'
    return None


def _flagged_new_groups(data, existing_ids=()):
    """Groups in the request that are flagged missing in OTS and not already associated."""
    ids = [g.get('id') for g in data.get('groups') or [] if isinstance(g, dict)]
    ids += list(data.get('groupIds') or [])
    def as_int(value):
        try:
            return None if isinstance(value, bool) else int(value)
        except (TypeError, ValueError):
            return None
    return OTSGroupModel.flagged_among([i for i in ids if as_int(i) not in existing_ids])


@api_v1.route('/users', methods=['POST'])
@jwt_required()
@require_permission('users.create')
def create_user():
    """
    Create a new user (admin only)

    Request body:
    {
        "username": "string",
        "password": "string",
        "email": "string",
        "firstName": "string",
        "lastName": "string",
        "callsign": "string",
        "roleIds": [int],
        "expiryDate": "ISO8601 string" (optional)
    }
    """
    data = request.get_json()
    ignored = drop_fields_without_permission(data, {'users.roles.assign': ('roleIds',),
                                                    'users.groups.assign': ('groups', 'groupIds')})

    # Before anything reaches OTS
    if not is_administrator() and role_ids_include_privileged(data.get('roleIds')):
        return jsonify({'error': PRIVILEGED_ROLE_ERROR}), 403
    denied = deny_beyond_actor(ROLE_BEYOND_ACTOR_ERROR, roles_from_ids(data.get('roleIds')))
    if denied:
        return denied

    # Validate required fields
    required_fields = ['username', 'password', 'email', 'firstName', 'lastName', 'callsign']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    # Normalize and validate the username (same rules as self-registration)
    if not isinstance(data['username'], str):
        return jsonify({'error': 'Username must be a string'}), 400
    username = data['username'].lower().strip()
    if not re.match(r'^[a-z0-9]+$', username):
        return jsonify({'error': 'Username can only contain letters and numbers (no spaces, underscores, or periods)'}), 400
    if len(username) < 3 or len(username) > 32:
        return jsonify({'error': 'Username must be between 3 and 32 characters'}), 400

    password_error = new_password_error(data['password'])
    if password_error:
        return jsonify({'error': password_error}), 400

    # Check if user already exists
    existing_user = UserModel.get_user_by_username(username)
    if existing_user:
        return jsonify({'error': 'Username already exists'}), 409

    flagged = _flagged_new_groups(data)
    if flagged:
        return jsonify({'error': 'Groups not found in OpenTAK Server: ' + ', '.join(g.name for g in flagged)}), 400

    try:
        # Create user in OTS first
        ots = OTSClient(OTS_URL, OTS_USERNAME, OTS_PASSWORD)

        # Determine OTS roles - OTS only supports 'user' and 'administrator'
        ots_roles = ['user']  # Default role
        if data.get('roleIds'):
            roles = UserRoleModel.query.filter(UserRoleModel.id.in_(data['roleIds'])).all()
            if any(is_privileged_role(role) for role in roles):
                ots_roles = ['administrator']

        # Create user in OTS with username, password, and roles
        ots.create_user(username, data['password'], ots_roles)

        # Create user in local database
        expiry_date = None
        if data.get('expiryDate'):
            expiry_date = datetime.fromisoformat(data['expiryDate'].replace('Z', '+00:00'))

        user = UserModel.create_user(
            username=username,
            email=data['email'],
            firstname=data['firstName'],
            lastname=data['lastName'],
            callsign=data['callsign'],
            expirydate=expiry_date
        )

        # Add roles
        if data.get('roleIds'):
            for role_id in data['roleIds']:
                role = UserRoleModel.get_by_id(role_id)
                if role:
                    user.roles.append(role)

        # Add OTS groups with direction
        if data.get('groups'):
            for g in data['groups']:
                group = OTSGroupModel.get_by_id(g['id'])
                if group:
                    assoc = GroupUserAssociation(group_id=group.id, user_id=user.id, direction=g.get('direction', 'BOTH'))
                    db.session.add(assoc)
                    directions = ['IN', 'OUT'] if assoc.direction == 'BOTH' else [assoc.direction]
                    for d in directions:
                        try:
                            ots.add_user_to_group(username, group.name, direction=d)
                        except Exception as e:
                            current_app.logger.error(f"Failed to add {username} to OTS group {group.name} {d}: {e}")
        elif data.get('groupIds'):
            for gid in data['groupIds']:
                group = OTSGroupModel.get_by_id(gid)
                if group:
                    assoc = GroupUserAssociation(group_id=group.id, user_id=user.id, direction='BOTH')
                    db.session.add(assoc)
                    for d in ('IN', 'OUT'):
                        try:
                            ots.add_user_to_group(username, group.name, direction=d)
                        except Exception as e:
                            current_app.logger.error(f"Failed to add {username} to OTS group {group.name} {d}: {e}")

        db.session.commit()

        return jsonify({
            'message': 'User created successfully',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'callsign': user.callsign
            },
            **ignored_fields_body(ignored)
        }), 201

    except Exception as e:
        return jsonify({'error': f'Failed to create user: {str(e)}'}), 400


@api_v1.route('/users/<int:user_id>', methods=['PUT'])
@jwt_required()
def update_user(user_id):
    """
    Update a user: anyone on their own profile for the basic fields, others per field.

    Request body:
    {
        "email": "string",  // others: users.accounts.edit and users.password.reset
        "firstName": "string",  // others: users.accounts.edit, like lastName, callsign, language
        "lastName": "string",
        "callsign": "string",
        "roleIds": [int],  // users.roles.assign
        "groups": [{"id": int, "direction": "string"}],  // users.groups.assign, or groupIds: [int]
        "password": "string",  // users.password.reset
        "expiryDate": "ISO8601 string"  // users.accounts.edit
    }
    Fields the user has no permission for have no effect and are named in ignoredFields.

    Everything is validated before the first OTS side effect. Local changes are flushed,
    then OTS is changed (administrator role first), then they are committed. If a step
    fails after the OTS role may have changed, the local changes are rolled back and the role
    is restored. The error only says nothing was changed if OTS definitely refused or did
    not change, or the restore was confirmed; otherwise it says to check the account in OTS.
    """
    actor_is_administrator = is_administrator()
    can_edit_account = has_permission('users.accounts.edit')
    can_assign_roles = has_permission('users.roles.assign')
    can_assign_groups = has_permission('users.groups.assign')
    can_reset_password = has_permission('users.password.reset')
    is_own_profile = int(get_jwt_identity()) == user_id

    if not (can_edit_account or can_assign_roles or can_assign_groups or can_reset_password or is_own_profile):
        return jsonify({'error': 'Permission denied'}), 403

    user = UserModel.get_user_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    if not actor_is_administrator and not is_own_profile and has_privileged_role(user):
        return jsonify({'error': PRIVILEGED_TARGET_ERROR}), 403
    if not is_own_profile:
        denied = deny_beyond_actor(TARGET_BEYOND_ACTOR_ERROR, list(user.roles))
        if denied:
            return denied

    data = request.get_json()

    # On another user the email counts as a password reset: with forgot-password it takes the account over
    can_edit_email = is_own_profile or (can_edit_account and can_reset_password)
    can_edit_basics = is_own_profile or can_edit_account
    ignored = [field for field, allowed in (
        ('email', can_edit_email), ('firstName', can_edit_basics), ('lastName', can_edit_basics),
        ('callsign', can_edit_basics), ('language', can_edit_basics), ('expiryDate', can_edit_account),
        ('roleIds', can_assign_roles), ('groups', can_assign_groups), ('groupIds', can_assign_groups),
        ('password', can_reset_password)) if field in data and not allowed]

    group_change = can_assign_groups and ('groups' in data or 'groupIds' in data)
    if group_change:
        group_error = _group_request_error(data)
        if group_error:
            return jsonify({'error': group_error}), 400

    # The portal logs in to OTS with this account's credentials from its configuration
    if is_service_account(user.username):
        if data.get('password'):
            return jsonify({'error': SERVICE_ACCOUNT_PASSWORD_ERROR}), 403
        if data.get('expiryDate'):
            return jsonify({'error': 'The portal service account cannot be given an expiry date'}), 403

    new_expiry = None
    if can_edit_account and data.get('expiryDate'):
        try:
            new_expiry = datetime.fromisoformat(data['expiryDate'].replace('Z', '+00:00'))
        except (AttributeError, ValueError):
            return jsonify({'error': 'Invalid expiryDate'}), 400

    # The UI sends the full list, so only what is added or removed counts
    new_roles = None
    was_privileged = has_privileged_role(user)
    now_privileged = was_privileged
    if can_assign_roles and 'roleIds' in data:
        if not isinstance(data['roleIds'], list):
            return jsonify({'error': 'roleIds must be a list'}), 400
        new_roles = [role for role in (UserRoleModel.get_by_id(role_id) for role_id in data['roleIds']) if role]
        added = set(new_roles) - set(user.roles)
        removed = set(user.roles) - set(new_roles)
        if not actor_is_administrator and any(is_privileged_role(role) for role in added | removed):
            return jsonify({'error': PRIVILEGED_ROLE_ERROR}), 403
        denied = deny_beyond_actor(ROLE_BEYOND_ACTOR_ERROR, list(added | removed))
        if denied:
            return denied
        if is_service_account(user.username) and any(is_privileged_role(role) for role in removed):
            return jsonify({'error': 'The portal service account cannot lose the administrator role'}), 403
        if any(role.name == 'administrator' for role in removed) and is_last_administrator(user):
            return jsonify({'error': 'The last administrator cannot lose the administrator role'}), 409
        now_privileged = any(is_privileged_role(role) for role in new_roles)
    # Without a password the user has no OTS account yet; set-password creates it with the right role
    ots_role_changes = now_privileged != was_privileged and user.has_password is not False

    # Before anything is changed: the group sync below already pushes to OTS
    if can_reset_password and data.get('password'):
        password_error = new_password_error(data['password'])
        if password_error:
            return jsonify({'error': password_error}), 400

    if group_change:
        flagged = _flagged_new_groups(data, {a.group_id for a in user.group_associations})
        if flagged:
            return jsonify({'error': 'Groups not found in OpenTAK Server: ' + ', '.join(g.name for g in flagged)}), 400

    # Update basic fields (allowed for own profile)
    if can_edit_email and data.get('email'):
        user.email = data['email']
    if can_edit_basics:
        if data.get('firstName'):
            user.firstName = data['firstName']
        if data.get('lastName'):
            user.lastName = data['lastName']
        if data.get('callsign'):
            user.callsign = data['callsign']
        if 'language' in data:
            user.language = data['language']

    # Update roles if roleIds is provided in the request
    if new_roles is not None:
        user.roles.clear()
        for role in new_roles:
            user.roles.append(role)

    if can_edit_account and 'expiryDate' in data:
        user.expiryDate = new_expiry

    # A local change that cannot be stored never reaches OTS
    username = user.username
    try:
        if group_change:
            new_groups_list = data.get('groups', [{'id': gid, 'direction': 'BOTH'} for gid in data.get('groupIds', [])])
            old_assocs = {a.group_id: a.direction for a in user.group_associations}
            new_assocs = {g['id']: g.get('direction', 'BOTH') for g in new_groups_list}

            GroupUserAssociation.query.filter_by(user_id=user.id).delete()
            for g in new_groups_list:
                group = OTSGroupModel.get_by_id(g['id'])
                if group:
                    assoc = GroupUserAssociation(group_id=group.id, user_id=user.id, direction=g.get('direction', 'BOTH'))
                    db.session.add(assoc)

        db.session.flush()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to store the update of user '{username}': {e}")
        return jsonify({'error': 'Failed to update user'}), 400

    # OTS first: if it refuses, nothing has been changed anywhere
    if ots_role_changes:
        try:
            _set_ots_admin(username, now_privileged)
            current_app.logger.info(f"OTS role of '{username}' set for privileged={now_privileged}")
        except BadRequestError as e:
            db.session.rollback()
            current_app.logger.error(f"OTS refused the role change of '{username}': {e}")
            return jsonify({'error': 'OpenTAK Server refused the role change (for example because it uses LDAP or has no such account); nothing was changed'}), 400
        except OTSRoleUnconfirmed as e:
            db.session.rollback()
            current_app.logger.error(f"Could not confirm the OTS role of '{username}': {e}")
            if _restore_ots_admin(username, was_privileged):
                return jsonify({'error': 'Could not change the role in OpenTAK Server; nothing was changed'}), 502
            return jsonify({'error': 'Could not confirm the role change in OpenTAK Server, so the portal was not changed. '
                                     'OpenTAK Server and the portal may differ: check this account in OpenTAK Server.'}), 502
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Failed to change the OTS role of '{username}': {e}")
            return jsonify({'error': 'Could not change the role in OpenTAK Server; nothing was changed'}), 502

    # From here on OTS may have changed: any failure rolls back and restores the role
    new_password = data.get('password') if can_reset_password else None
    password_changed = False
    try:
        # Handle password reset (admin only)
        if new_password:
            ots = OTSClient(OTS_URL, OTS_USERNAME, OTS_PASSWORD)
            ots.reset_user_password(username, new_password)
            password_changed = True
            current_app.logger.info(f"Password reset for user '{username}' by admin")

        if group_change:
            added = set(new_assocs.keys()) - set(old_assocs.keys())
            removed = set(old_assocs.keys()) - set(new_assocs.keys())
            changed = {gid for gid in set(new_assocs.keys()) & set(old_assocs.keys()) if new_assocs[gid] != old_assocs[gid]}

            try:
                ots = OTSClient(OTS_URL, OTS_USERNAME, OTS_PASSWORD)
                for gid in added | changed:
                    group = OTSGroupModel.get_by_id(gid)
                    if group and not group.missing_in_ots:
                        new_dir = new_assocs[gid]
                        directions = ['IN', 'OUT'] if new_dir == 'BOTH' else [new_dir]
                        old_dir = old_assocs.get(gid)
                        if old_dir:
                            old_dirs = ['IN', 'OUT'] if old_dir == 'BOTH' else [old_dir]
                            for d in old_dirs:
                                if d not in directions:
                                    try:
                                        ots.remove_user_from_group(user.username, group.name, direction=d)
                                    except Exception as e:
                                        current_app.logger.error(f"Failed to remove {user.username} from OTS group {group.name} {d}: {e}")
                        for d in directions:
                            try:
                                ots.add_user_to_group(user.username, group.name, direction=d)
                            except Exception as e:
                                current_app.logger.error(f"Failed to add {user.username} to OTS group {group.name} {d}: {e}")
                for gid in removed:
                    group = OTSGroupModel.get_by_id(gid)
                    if group and not group.missing_in_ots:
                        old_dir = old_assocs[gid]
                        directions = ['IN', 'OUT'] if old_dir == 'BOTH' else [old_dir]
                        for d in directions:
                            try:
                                ots.remove_user_from_group(user.username, group.name, direction=d)
                            except Exception as e:
                                current_app.logger.error(f"Failed to remove {user.username} from OTS group {group.name} {d}: {e}")
            except Exception as e:
                current_app.logger.error(f"Failed to sync OTS groups for {user.username}: {e}")

        # Commit directly to ensure relationship changes are saved
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to update user '{username}': {e}")
        if ots_role_changes:
            _restore_ots_admin(username, was_privileged)
        if password_changed:
            error = 'Failed to update user, but the password was already changed in OpenTAK Server'
        elif new_password:
            error = 'Failed to reset password'
        else:
            error = 'Failed to update user'
        return jsonify({'error': error, 'passwordChanged': password_changed}), 400

    message = 'User updated successfully'
    if password_changed:
        message = 'User updated and password reset successfully'

    return jsonify({
        'message': message,
        'passwordChanged': password_changed,
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'callsign': user.callsign,
            'roles': [role.name for role in user.roles]
        },
        **ignored_fields_body(ignored)
    }), 200


@api_v1.route('/users/<int:user_id>', methods=['DELETE'])
@jwt_required()
@require_permission('users.delete')
def delete_user(user_id):
    """
    Delete user from both OTS and local database (admin only)

    The local delete is flushed first so one that cannot succeed never touches OTS;
    it is only committed once OTS has processed the delete.
    """
    user = UserModel.get_user_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    if not is_administrator() and has_privileged_role(user):
        return jsonify({'error': PRIVILEGED_TARGET_ERROR}), 403
    denied = deny_beyond_actor(TARGET_BEYOND_ACTOR_ERROR, list(user.roles))
    if denied:
        return denied
    if is_service_account(user.username):
        return jsonify({'error': 'The portal service account cannot be deleted'}), 403
    if is_last_administrator(user):
        return jsonify({'error': 'The last administrator cannot be deleted'}), 409

    username = user.username

    try:
        # Personal event leader designations reference the user (committed with the delete)
        from app.api_v1.events import remove_user_leader_designations
        remove_user_leader_designations(user_id, int(get_jwt_identity()))
        UserModel.clear_references(user_id)
        db.session.delete(user)
        db.session.flush()

        message = f'User {username} deleted from both OTS and the local database'
        try:
            if not delete_user_if_present(OTSClient(OTS_URL, OTS_USERNAME, OTS_PASSWORD), username):
                current_app.logger.warning(f"OTS has no account '{username}', deleting locally only")
                message = f'User {username} deleted from the local database; OTS has no such account'
        except Exception as ots_error:
            db.session.rollback()
            current_app.logger.error(f"Failed to delete user '{username}' from OTS: {ots_error}")
            return jsonify({'error': f'Could not delete user {username} from OTS; the user was kept'}), 502

        db.session.commit()
        current_app.logger.info(f"Deleted user '{username}': {message}")
        return jsonify({'message': message}), 200

    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.warning(f"Cannot delete user '{username}': {e}")
        return jsonify({'error': f'User {username} cannot be deleted: other records still reference them'}), 409

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to delete user '{username}': {e}")
        return jsonify({'error': f'Failed to delete user: {e}'}), 500
