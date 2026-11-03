"""
Role-Based Access Control (RBAC) helpers for the OpenTAK Onboarding Portal.

Roles are stored in the local database, not in OTS, and are read on every request. Endpoints check
permissions (catalogue in app/permissions.py), not role names:
- administrator: all permissions
- <x>_admin / <x>_readonly in ADMIN_ROLES: the permissions of their module(s), fixed in code
- any other role that is not reserved (custom): the permissions in its role_permissions rows
Audiences (who gets a TAK profile, an announcement, an approval) go by role id, not by permission.
"""

from functools import wraps
from flask import current_app, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity

from app.permissions import (ALL_PERMISSIONS, ALSO_NEEDS_MODULE, ASSIGNABLE_PERMISSIONS, BUILTIN_ROLE_PERMISSIONS, CATALOGUE,
                             PORTAL_MODULES, denied_message, enforce_requires, module_of, with_implied_views)

# Define admin roles with description and display name
# Format: role_name: (description, display_name)
ADMIN_ROLES = {
    'administrator': ('Full administrator access', 'Administrator'),
    'user_admin': ('Manage user accounts', 'User Admin'),
    'user_readonly': ('View user accounts (read-only)', 'User Read-Only'),
    'role_admin': ('Manage user roles', 'Role Admin'),
    'role_readonly': ('View user roles (read-only)', 'Role Read-Only'),
    'onboarding_admin': ('Manage onboarding codes', 'Onboarding Admin'),
    'onboarding_readonly': ('View onboarding codes (read-only)', 'Onboarding Read-Only'),
    'registration_admin': ('Manage pending registrations', 'Registration Admin'),
    'registration_readonly': ('View pending registrations (read-only)', 'Registration Read-Only'),
    'tak_profile_admin': ('Manage TAK profiles', 'TAK Profile Admin'),
    'tak_profile_readonly': ('View TAK profiles (read-only)', 'TAK Profile Read-Only'),
    'meshtastic_admin': ('Manage Meshtastic configurations', 'Meshtastic Admin'),
    'meshtastic_readonly': ('View Meshtastic configurations (read-only)', 'Meshtastic Read-Only'),
    'radio_admin': ('Manage radio equipment', 'Radio Admin'),
    'radio_readonly': ('View radios and program/validate devices', 'Radio Read-Only'),
    'announcement_admin': ('Manage announcements', 'Announcement Admin'),
    'announcement_readonly': ('View announcements (read-only)', 'Announcement Read-Only'),
    'settings_admin': ('Manage system settings', 'Settings Admin'),
    'settings_readonly': ('View system settings (read-only)', 'Settings Read-Only'),
    'api_key_admin': ('Manage API keys', 'API Key Admin'),
    'api_key_readonly': ('View API keys (read-only)', 'API Key Read-Only'),
    'group_admin': ('Manage OTS groups', 'Group Admin'),
    'group_readonly': ('View OTS groups (read-only)', 'Group Read-Only'),
    'events_admin': ('Manage events, teams and access links', 'Events Admin'),
    'events_readonly': ('View events (read-only)', 'Events Read-Only'),
}

# Map modules to required roles (administrator always has access)
MODULE_ROLES = {
    'users': ['administrator', 'user_admin', 'user_readonly'],
    'roles': ['administrator', 'role_admin', 'role_readonly'],
    'onboarding_codes': ['administrator', 'onboarding_admin', 'onboarding_readonly'],
    'pending_registrations': ['administrator', 'registration_admin', 'registration_readonly'],
    'tak_profiles': ['administrator', 'tak_profile_admin', 'tak_profile_readonly'],
    'meshtastic': ['administrator', 'meshtastic_admin', 'meshtastic_readonly'],
    'radios': ['administrator', 'radio_admin', 'radio_readonly'],
    'announcements': ['administrator', 'announcement_admin', 'announcement_readonly'],
    'settings': ['administrator', 'settings_admin', 'settings_readonly'],
    'api_keys': ['administrator', 'api_key_admin', 'api_key_readonly'],
    'api_docs': ['administrator', 'api_key_admin', 'api_key_readonly'],
    'groups': ['administrator', 'group_admin', 'group_readonly'],
    'events': ['administrator', 'events_admin', 'events_readonly'],
}

# Roles that grant full administrator rights in OTS (a row named admin/Admin counts too)
PRIVILEGED_ROLE_NAMES = {'administrator', 'admin'}
# Names a new or renamed role may not take: they have a fixed meaning in the portal or OTS
RESERVED_ROLE_NAMES = PRIVILEGED_ROLE_NAMES | {'event_leader', 'user'} | set(ADMIN_ROLES)

# Subset rule: nobody grants what they do not hold (see deny_beyond_actor)
ROLE_BEYOND_ACTOR_ERROR = 'You cannot grant or remove a role with permissions you do not hold'
APPROVER_BEYOND_ACTOR_ERROR = 'Only an administrator can set or replace an administrator role as approver'
TARGET_BEYOND_ACTOR_ERROR = 'You cannot change a user with permissions you do not hold'
CODE_BEYOND_ACTOR_ERROR = 'You cannot use an onboarding code that hands out permissions you do not hold'

SERVICE_ACCOUNT_PASSWORD_ERROR = (
    'The password of the portal service account cannot be changed through the portal: '
    'change it in OpenTAK Server and in the portal configuration (OTS_PASSWORD)'
)


def get_user_roles():
    """Get the roles from the database for the current user (not from JWT)"""
    from app.models import UserModel

    try:
        # A bounded event-leader session is least-privilege: whoever holds the
        # shared leader link gets only the synthetic 'event_leader' role,
        # regardless of any DB roles on the underlying shared account.
        try:
            if get_jwt().get('event_leader_team') is not None:
                return ['event_leader']
        except Exception:
            pass

        # A kiosk session runs on a shared terminal: it never acts with the
        # user's roles, so an admin signing in at a kiosk leaves no admin token.
        try:
            if get_jwt().get('kiosk_session'):
                return []
        except Exception:
            pass

        current_user_id = get_jwt_identity()
        if not current_user_id:
            return []

        user = UserModel.get_user_by_id(int(current_user_id))
        if not user:
            return []

        return [role.name for role in user.roles]
    except Exception:
        return []


def get_effective_permissions():
    """The permissions of the current user: the union over their roles, from the database.

    Cached on the WSGI environ, so per request (flask.g lives as long as the app context).
    Kiosk and event-leader sessions resolve to no permissions through get_user_roles().
    """
    permissions = request.environ.get('portal.permissions')
    if permissions is None:
        permissions = _resolve_permissions(get_user_roles())
        request.environ['portal.permissions'] = permissions
    return permissions


def _resolve_permissions(role_names):
    from app.models import RolePermissionModel, UserRoleModel

    permissions = set()
    custom_names = []
    for name in role_names:
        if name in BUILTIN_ROLE_PERMISSIONS:
            permissions |= BUILTIN_ROLE_PERMISSIONS[name]
        elif is_custom_role_name(name):
            custom_names.append(name)
    if custom_names:
        rows = RolePermissionModel.query.join(UserRoleModel).filter(UserRoleModel.name.in_(custom_names)).all()
        # A key can have left the catalogue since it was stored
        permissions |= with_implied_views(row.permission for row in rows if row.permission in ASSIGNABLE_PERMISSIONS)
        # Over all roles together: another role may hold what a key needs. Built-ins always meet their own.
        permissions = enforce_requires(permissions)
    return frozenset(permissions)


def has_permission(key):
    """Check if the current user holds a permission of the catalogue in app/permissions.py"""
    return key in get_effective_permissions()


def permission_denied(key):
    return jsonify({'error': denied_message(key)}), 403


def require_permission(key):
    """
    Decorator to require a permission for an endpoint; goes below @jwt_required().
    Field-level permissions and settings by key are checked in the endpoint with has_permission().
    """
    if key not in ALL_PERMISSIONS:
        raise ValueError(f'Unknown permission: {key}')

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not has_permission(key):
                return permission_denied(key)
            return fn(*args, **kwargs)

        return wrapper
    return decorator


def drop_fields_without_permission(data, fields_by_permission):
    """Remove request fields the current user may not set and return which were sent (see ignored_fields_body).

    A form sends its whole record, so refusing the request would also refuse a save that changes nothing.
    """
    ignored = []
    for key, fields in fields_by_permission.items():
        if not has_permission(key):
            ignored += [field for field in fields if field in data]
            for field in fields:
                data.pop(field, None)
    return ignored


def ignored_fields_body(ignored):
    """For the response of a write: which fields of the request had no effect. Left out when there are none."""
    return {'ignoredFields': sorted(ignored)} if ignored else {}


def require_any_permission():
    """Check if the user holds any permission (the admin pages use the role list as a picker)"""
    if get_effective_permissions():
        return None
    return jsonify({'error': 'Administrator role required'}), 403


def _normalize_role_name(name):
    return str(name or '').strip().casefold()


def is_privileged_role(role):
    """True for a role that grants full administrator rights, whatever its case or padding"""
    return _normalize_role_name(role.name) in PRIVILEGED_ROLE_NAMES


def is_reserved_role_name(name):
    """True if a new or renamed role may not use this name"""
    return _normalize_role_name(name) in RESERVED_ROLE_NAMES


def is_custom_role_name(name):
    """Only a custom role can bear permissions in the database: not a built-in, OTS or privileged name"""
    return not is_reserved_role_name(name)


def role_permission_keys(role):
    """What this role itself holds: fixed for a built-in, stored rows for a custom role, nothing otherwise.

    Without enforce_requires: a key that does nothing on its own starts to count next to another role,
    so it counts when deciding who may hand out the role.
    """
    if role.name in BUILTIN_ROLE_PERMISSIONS:
        return BUILTIN_ROLE_PERMISSIONS[role.name]
    if not is_custom_role_name(role.name):
        return frozenset()
    return with_implied_views(row.permission for row in role.permissions if row.permission in ASSIGNABLE_PERMISSIONS)


def _keys_to_hand_out(role):
    """A privileged role also makes its holder an OTS administrator: only the administrator covers that"""
    return ALL_PERMISSIONS if is_privileged_role(role) else role_permission_keys(role)


def permissions_beyond_actor(roles):
    """The keys of these roles the current user does not hold, sorted"""
    keys = set().union(*map(_keys_to_hand_out, roles)) if roles else set()
    return sorted(keys - get_effective_permissions())


def deny_beyond_actor(error, roles):
    """The 403 response if these roles hold anything the current user does not, else None"""
    missing = permissions_beyond_actor(roles)
    return (jsonify({'error': error, 'missingPermissions': missing}), 403) if missing else None


def role_beyond_actor(role):
    return bool(permissions_beyond_actor([role]))


def holder_beyond_actor(holder):
    """True if a user or onboarding code carries roles above the current user's level"""
    return bool(permissions_beyond_actor(list(holder.roles)))


def roles_from_ids(role_ids):
    """The existing roles among the ids of a request"""
    from app.models import UserRoleModel

    if not isinstance(role_ids, list):
        return []
    return [role for role in (UserRoleModel.get_by_id(role_id) for role_id in role_ids if role_id is not None) if role]


def code_hands_out_permissions(code):
    """True if an account from this code gets any permission at all"""
    return any(_keys_to_hand_out(role) for role in code.roles)


def is_administrator():
    """True if the current user holds the 'administrator' role (from database)"""
    return 'administrator' in get_user_roles()


def has_privileged_role(holder):
    """True if a user or onboarding code carries a privileged role"""
    return any(is_privileged_role(role) for role in holder.roles)


def role_ids_include_privileged(role_ids):
    """True if the roleIds of a request name a privileged role.

    Anything but a list cannot be vetted, so it counts as privileged.
    """
    from app.models import UserRoleModel

    if not role_ids:
        return False
    if not isinstance(role_ids, list):
        return True
    roles = [UserRoleModel.get_by_id(role_id) for role_id in role_ids]
    return any(role and is_privileged_role(role) for role in roles)


def role_ids_beyond_actor():
    """Ids of all roles above the current user's level, to filter queries"""
    from app.models import UserRoleModel

    return [role.id for role in UserRoleModel.get_all_roles() if role_beyond_actor(role)]


def visible_code_value(code):
    """The value of an onboarding code, or None if the current user may not see it: it is a registration ticket"""
    if holder_beyond_actor(code):
        return None
    return code.onboardingCode


def is_service_account(username):
    """True for the account the portal itself uses for every OTS call (OTS_USERNAME)"""
    service_username = current_app.config.get('OTS_USERNAME')
    # settings.py turns an unset OTS_USERNAME into the string 'None'
    if not username or not service_username or service_username == 'None':
        return False
    return username.strip().casefold() == service_username.strip().casefold()


def is_last_administrator(user):
    """True if this user is the only one holding the 'administrator' role"""
    from app.models import UserModel, UserRoleModel

    if not any(role.name == 'administrator' for role in user.roles):
        return False
    return not UserModel.query.filter(
        UserModel.id != user.id,
        UserModel.roles.any(UserRoleModel.name == 'administrator')
    ).first()


def enabled_modules():
    """The catalogue modules that are switched on, read once per request"""
    from app.models import MODULE_SETTINGS, SystemSettingsModel

    enabled = request.environ.get('portal.enabled_modules')
    if enabled is None:
        off = {module for module in MODULE_SETTINGS if not SystemSettingsModel.is_module_enabled(module)}
        enabled = frozenset(CATALOGUE) - off
        request.environ['portal.enabled_modules'] = enabled
    return enabled


def get_user_admin_modules():
    """Get list of modules the current user has access to (from database); disabled modules are left out"""
    permissions = get_effective_permissions()
    enabled = enabled_modules()
    return [module for module, catalogue_module in PORTAL_MODULES.items()
            if catalogue_module in enabled and permissions.intersection(CATALOGUE[catalogue_module])]


def get_user_permissions_list():
    """The current user's permissions, leaving out what a disabled module would refuse anyway"""
    enabled = enabled_modules()
    return sorted(key for key in get_effective_permissions()
                  if module_of(key) in enabled and ALSO_NEEDS_MODULE.get(key, module_of(key)) in enabled)


def seed_admin_roles(db, UserRoleModel):
    """
    Seed the database with admin roles if they don't exist.
    Also updates display names for any roles missing them.
    Call this during app initialization.
    """
    try:
        # Seed admin roles
        for role_name, (description, display_name) in ADMIN_ROLES.items():
            existing = UserRoleModel.get_role_by_name(role_name)
            if not existing:
                UserRoleModel.create_role(role_name, description, display_name)
                print(f"Created admin role: {role_name}")
            elif existing and not getattr(existing, 'display_name', None):
                # Update existing roles with display_name if missing
                existing.display_name = display_name
                db.session.commit()
                print(f"Updated admin role display_name: {role_name}")

        # Update any other roles (e.g., OTS-created) that don't have display names
        all_roles = UserRoleModel.get_all_roles()
        for role in all_roles:
            if not getattr(role, 'display_name', None):
                # Generate display name by capitalizing (e.g., "user" -> "User")
                role.display_name = role.name.replace('_', ' ').title()
                db.session.commit()
                print(f"Updated role display_name: {role.name} -> {role.display_name}")
    except Exception as e:
        # Handle case where migration hasn't run yet (missing columns)
        print(f"Warning: Could not seed admin roles (likely pending migration): {e}")
        db.session.rollback()
