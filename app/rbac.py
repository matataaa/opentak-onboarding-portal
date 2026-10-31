"""
Role-Based Access Control (RBAC) helpers for the OpenTAK Onboarding Portal.

Admin roles (stored in local database, not OTS):
- administrator: Full access to everything
- user_admin: Manage users
- role_admin: Manage roles
- onboarding_admin: Manage onboarding codes
- registration_admin: Manage pending registrations
- tak_profile_admin: Manage TAK profiles
- meshtastic_admin: Manage Meshtastic configurations
- radio_admin: Manage radios
- announcement_admin: Manage announcements
- settings_admin: Manage system settings
"""

from functools import wraps
from flask import current_app, jsonify
from flask_jwt_extended import get_jwt, get_jwt_identity

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


def has_role(role_name):
    """Check if user has a specific role (from database)"""
    return role_name in get_user_roles()


def has_any_role(role_names):
    """Check if user has any of the specified roles (from database)"""
    user_roles = get_user_roles()
    return any(role in user_roles for role in role_names)


def has_module_access(module_name):
    """Check if user has access to a specific module"""
    required_roles = MODULE_ROLES.get(module_name, ['administrator'])
    return has_any_role(required_roles)


def require_role(*allowed_roles):
    """
    Decorator to require specific role(s) for an endpoint.
    Checks roles from database, not JWT.
    Administrator role always has access.

    Usage:
        @require_role('user_admin')
        def get_users():
            ...

        @require_role('role_admin', 'user_admin')
        def some_endpoint():
            ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_roles = get_user_roles()

            # Administrator always has access
            if 'administrator' in user_roles:
                return fn(*args, **kwargs)

            # Check if user has any of the allowed roles
            if any(role in user_roles for role in allowed_roles):
                return fn(*args, **kwargs)

            return jsonify({
                'error': 'Access denied. Required role: ' + ' or '.join(allowed_roles)
            }), 403

        return wrapper
    return decorator


def require_any_admin_role():
    """Check if user has any admin-level role (from database)"""
    user_roles = get_user_roles()
    admin_role_names = list(ADMIN_ROLES.keys())
    if any(role in user_roles for role in admin_role_names):
        return None
    return jsonify({'error': 'Administrator role required'}), 403


def require_admin():
    """Check for full administrator role (from database)"""
    if 'administrator' not in get_user_roles():
        return jsonify({'error': 'Administrator role required'}), 403
    return None


def _normalize_role_name(name):
    return str(name or '').strip().casefold()


def is_privileged_role(role):
    """True for a role that grants full administrator rights, whatever its case or padding"""
    return _normalize_role_name(role.name) in PRIVILEGED_ROLE_NAMES


def is_reserved_role_name(name):
    """True if a new or renamed role may not use this name"""
    return _normalize_role_name(name) in RESERVED_ROLE_NAMES


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


def privileged_role_ids():
    """Ids of all privileged roles, to filter queries (names are compared here, not in SQL)"""
    from app.models import UserRoleModel

    return [role.id for role in UserRoleModel.get_all_roles() if is_privileged_role(role)]


def visible_code_value(code):
    """The value of an onboarding code, or None if the current user may not see it"""
    if has_privileged_role(code) and not is_administrator():
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


def get_user_admin_modules():
    """Get list of modules the current user has access to (from database); disabled modules are left out"""
    from app.models import MODULE_SETTINGS, SystemSettingsModel

    user_roles = get_user_roles()
    accessible_modules = []

    for module, required_roles in MODULE_ROLES.items():
        if module in MODULE_SETTINGS and not SystemSettingsModel.is_module_enabled(module):
            continue
        if any(role in user_roles for role in required_roles):
            accessible_modules.append(module)

    return accessible_modules


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
