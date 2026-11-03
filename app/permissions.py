"""
Permission catalogue: what a role can be allowed to do, per module and function.

The built-in roles get their permissions from BUILTIN_ROLE_PERMISSIONS below (no database rows).
Custom roles get theirs from the role_permissions table. app/rbac.py resolves them per request.

Flags per key:
- R: part of the module's <x>_readonly role
- S: sensitive (shown as such when composing a role)
- A: administrator only: not part of <x>_admin, cannot be given to a custom role

The per-setting risk does not show in the flags: settings.general.edit is S for the installer QR URLs
(where a phone downloads the app from), settings.enrollment.edit for kiosk_enrollment_enabled.
"""

CATALOGUE = {
    'users': {
        'users.accounts.view': 'R',
        'users.accounts.edit': 'S',
        'users.create': 'S',
        'users.delete': 'S',
        'users.roles.assign': 'S',
        'users.groups.assign': '',
        'users.password.reset': 'S',
        'users.impersonate': 'SA',
    },
    'roles': {
        'roles.view': 'R',
        'roles.edit': '',
        'roles.delete': '',
        'roles.permissions.edit': 'S',
    },
    'onboarding': {
        'onboarding.view': 'RS',
        'onboarding.edit': '',
        'onboarding.delete': '',
        'onboarding.roles.assign': 'S',
        'onboarding.groups.assign': '',
    },
    'registrations': {
        'registrations.view': 'RS',
        'registrations.edit': 'S',
        'registrations.approve': 'S',
        'registrations.delete': '',
    },
    'tak_profiles': {
        'tak_profiles.view': 'R',
        'tak_profiles.edit': '',
        'tak_profiles.assign': '',
        'tak_profiles.delete': '',
    },
    'meshtastic': {
        'meshtastic.channels.view': 'RS',
        'meshtastic.channels.edit': '',
        'meshtastic.channels.delete': '',
        'meshtastic.channels.sync': '',
        'meshtastic.groups.view': 'S',
        'meshtastic.groups.edit': '',
        'meshtastic.groups.delete': '',
    },
    'radios': {
        'radios.view': 'R',
        'radios.edit': 'S',
        'radios.assign': '',
        'radios.delete': '',
        'radios.claim_code': 'R',
        'radios.program': 'RS',
    },
    'announcements': {
        'announcements.view': 'R',
        'announcements.email_details': '',
        'announcements.edit': '',
        'announcements.delete': '',
        'announcements.send': 'S',
    },
    'settings': {
        'settings.view': 'R',
        'settings.general.edit': 'S',
        'settings.branding.edit': '',
        'settings.registration.edit': 'S',
        'settings.enrollment.edit': 'S',
        'settings.radios.edit': '',
        'settings.modules.edit': 'S',
        'settings.security.edit': 'S',
        'settings.oidc.view': 'A',
        'settings.oidc.edit': 'SA',
    },
    'api_keys': {
        'api_keys.view': 'R',
        'api_keys.edit': 'S',
        'api_keys.delete': '',
    },
    'api_docs': {
        'api_docs.view': 'R',
    },
    'groups': {
        'groups.view': 'R',
        'groups.members.view': 'R',
        'groups.edit': '',
        'groups.delete': 'S',
        'groups.members.edit': 'S',
        'groups.sync': '',
    },
    'events': {
        'events.view': 'R',
        'events.edit': '',
        'events.delete': '',
        'events.teams.edit': 'S',
        'events.links.view': 'RS',
        'events.links.reveal': 'S',
        'events.links.edit': 'S',
        'events.leaders.view': 'R',
        'events.leaders.edit': 'S',
        'events.accounts.view': 'R',
        'events.accounts.edit': '',
    },
}

# What a key needs to be of any use, besides the view key an edit key implies. Enforced when a custom
# role is resolved (enforce_requires): a key whose requirement is missing does not count, so nobody
# can overwrite what they cannot read. The role editor shows the same hints.
REQUIRES = {
    'users.create': ('users.accounts.view',),
    'users.delete': ('users.accounts.view',),
    'users.roles.assign': ('users.accounts.view',),
    'users.groups.assign': ('users.accounts.view',),
    'users.password.reset': ('users.accounts.view',),
    'users.impersonate': ('users.accounts.view',),
    'roles.delete': ('roles.view',),
    'onboarding.delete': ('onboarding.view',),
    'onboarding.roles.assign': ('onboarding.edit',),
    'onboarding.groups.assign': ('onboarding.edit',),
    'registrations.approve': ('registrations.view',),
    'registrations.delete': ('registrations.view',),
    'tak_profiles.assign': ('tak_profiles.edit',),
    'tak_profiles.delete': ('tak_profiles.view',),
    'meshtastic.channels.delete': ('meshtastic.channels.view',),
    'meshtastic.channels.sync': ('meshtastic.channels.view',),
    # A group's combined URL holds the channel keys, and editing can make it public to oneself
    'meshtastic.groups.edit': ('meshtastic.channels.view',),
    'meshtastic.groups.delete': ('meshtastic.groups.view',),
    'radios.assign': ('radios.view',),
    'radios.delete': ('radios.view',),
    'radios.claim_code': ('radios.view',),
    'radios.program': ('radios.view',),
    'announcements.email_details': ('announcements.view',),
    'announcements.delete': ('announcements.view',),
    'announcements.send': ('announcements.view',),
    'api_keys.delete': ('api_keys.view',),
    'groups.members.view': ('groups.view',),
    'groups.delete': ('groups.view',),
    'groups.sync': ('groups.view',),
    'events.delete': ('events.view',),
    'events.links.view': ('events.view',),
    'events.links.reveal': ('events.links.view',),
    'events.leaders.view': ('events.view',),
    'events.accounts.view': ('events.view',),
}

# A permission only shows while every module its routes need is on (see app/api_v1/module_gate.py)
ALSO_NEEDS_MODULE = {'radios.program': 'meshtastic'}

ALL_PERMISSIONS = frozenset(key for functions in CATALOGUE.values() for key in functions)
_KEY_MODULE = {key: module for module, functions in CATALOGUE.items() for key in functions}
_KEY_FLAGS = {key: flags for functions in CATALOGUE.values() for key, flags in functions.items()}
# What a custom role may hold
ASSIGNABLE_PERMISSIONS = frozenset(key for key in ALL_PERMISSIONS if 'A' not in _KEY_FLAGS[key])


def _module_admin(*modules):
    return frozenset(key for module in modules for key, flags in CATALOGUE[module].items() if 'A' not in flags)


def _module_readonly(*modules):
    return frozenset(key for module in modules for key, flags in CATALOGUE[module].items() if 'R' in flags)


# The name of a role says little about its module (user_admin -> users, api_key_* -> two modules),
# hence an explicit map. Roles that are not in here and are not custom hold nothing.
BUILTIN_ROLE_PERMISSIONS = {
    'administrator': ALL_PERMISSIONS,
    'user_admin': _module_admin('users'),
    'user_readonly': _module_readonly('users'),
    'role_admin': _module_admin('roles'),
    'role_readonly': _module_readonly('roles'),
    'onboarding_admin': _module_admin('onboarding'),
    'onboarding_readonly': _module_readonly('onboarding'),
    'registration_admin': _module_admin('registrations'),
    'registration_readonly': _module_readonly('registrations'),
    'tak_profile_admin': _module_admin('tak_profiles'),
    'tak_profile_readonly': _module_readonly('tak_profiles'),
    'meshtastic_admin': _module_admin('meshtastic'),
    'meshtastic_readonly': _module_readonly('meshtastic'),
    'radio_admin': _module_admin('radios'),
    'radio_readonly': _module_readonly('radios'),
    'announcement_admin': _module_admin('announcements'),
    'announcement_readonly': _module_readonly('announcements'),
    'settings_admin': _module_admin('settings'),
    'settings_readonly': _module_readonly('settings'),
    'api_key_admin': _module_admin('api_keys', 'api_docs'),
    'api_key_readonly': _module_readonly('api_keys', 'api_docs'),
    'group_admin': _module_admin('groups'),
    'group_readonly': _module_readonly('groups'),
    'events_admin': _module_admin('events'),
    'events_readonly': _module_readonly('events'),
}

# The modules the frontend knows (see MODULE_ROLES), in its order -> the catalogue module behind each
PORTAL_MODULES = {
    'users': 'users',
    'roles': 'roles',
    'onboarding_codes': 'onboarding',
    'pending_registrations': 'registrations',
    'tak_profiles': 'tak_profiles',
    'meshtastic': 'meshtastic',
    'radios': 'radios',
    'announcements': 'announcements',
    'settings': 'settings',
    'api_keys': 'api_keys',
    'api_docs': 'api_docs',
    'groups': 'groups',
    'events': 'events',
}

# Who may write a setting, by key (the database category does not say: events_enabled is in 'general').
# A setting that is not in here can only be changed by an administrator.
_SETTINGS_BY_PERMISSION = {
    'settings.general.edit': (
        'brand_name_enabled', 'brand_name_value', 'help_link_enabled', 'help_link_value', 'help_email_enabled',
        'help_email_value', 'open_in_atak_enabled', 'atak_homepage_icon_enabled', 'itak_homepage_icon_enabled',
        'truststore_homepage_icon_enabled', 'meshtastic_homepage_icon_enabled', 'atak_installer_qr_enabled',
        'atak_installer_qr_url', 'itak_installer_qr_enabled', 'itak_installer_qr_url',
        'meshtastic_installer_qr_android_enabled', 'meshtastic_installer_qr_android_url',
        'meshtastic_installer_qr_iphone_enabled', 'meshtastic_installer_qr_iphone_url',
    ),
    'settings.branding.edit': (
        'custom_logo_enabled', 'custom_logo_path', 'logo_display_mode', 'primary_color', 'accent_color',
        'login_layout', 'login_background_path', 'login_overlay_strength', 'login_glass_enabled',
        'login_header_bar_enabled', 'login_hero_texts', 'default_theme', 'kiosk_default_theme', 'date_format',
    ),
    'settings.registration.edit': (
        'notify_admin_pending_registration', 'notify_admin_new_registration', 'allow_manual_approval',
        'remove_expired_accounts_enabled', 'forgot_password_enabled',
    ),
    'settings.enrollment.edit': (
        'generate_atak_qr_code', 'generate_itak_qr_code', 'qr_token_expiry_minutes', 'qr_token_max_uses',
        'callsign_qr_code_enabled', 'kiosk_enrollment_enabled', 'kiosk_session_timeout_minutes', 'kiosk_language',
    ),
    'settings.radios.edit': (
        'claim_radio_enabled', 'user_program_radio_enabled', 'user_validate_radio_enabled',
        'user_radio_enrollment_enabled',
    ),
    'settings.modules.edit': ('meshtastic_enabled', 'radios_enabled', 'events_enabled'),
    'settings.security.edit': (
        'oidc_enabled', 'oidc_auto_create_user', 'magic_link_login_enabled', 'magic_link_expiry_minutes',
    ),
}
SETTING_PERMISSIONS = {key: permission for permission, keys in _SETTINGS_BY_PERMISSION.items() for key in keys}
SETTINGS_EDIT_PERMISSIONS = frozenset(_SETTINGS_BY_PERMISSION)

# Texts of the role checks these permissions replaced: (edit, read-only)
_DENIED_MESSAGES = {
    'users': ('User admin role required', 'User admin or readonly role required'),
    'roles': ('Role admin access required', 'Role admin or readonly access required'),
    'onboarding': ('Onboarding admin access required', 'Onboarding admin or readonly access required'),
    'registrations': ('Registration admin access required', 'Registration admin or readonly access required'),
    'tak_profiles': ('TAK profile admin access required', 'TAK profile admin or readonly access required'),
    'meshtastic': ('Meshtastic admin access required', 'Meshtastic admin or readonly access required'),
    'radios': ('Radio admin access required', 'Admin access required'),
    'announcements': ('Announcement admin access required', 'Announcement admin or readonly access required'),
    'settings': ('Settings admin access required', 'Settings admin or readonly access required'),
    'api_keys': ('API Key admin access required', 'API Key admin or readonly access required'),
    'groups': ('Group admin access required', 'Group admin or readonly access required'),
    'events': ('Access denied. Required role: events_admin',
               'Access denied. Required role: events_admin or events_readonly'),
}


def denied_message(key):
    """The error text of a refused permission"""
    if 'A' in _KEY_FLAGS[key]:
        return 'Access denied. Required role: administrator'
    edit, readonly = _DENIED_MESSAGES[_KEY_MODULE[key]]
    return readonly if 'R' in _KEY_FLAGS[key] else edit


def module_of(key):
    return _KEY_MODULE[key]


def _implied_view(key):
    """The view key of the same function: x.edit -> x.view, else the module's own view key"""
    if not key.endswith('.edit'):
        return None
    function = key[:-len('.edit')]
    for candidate in (f'{function}.view', f"{function.split('.')[0]}.view"):
        if candidate in ALL_PERMISSIONS:
            return candidate
    return None


def with_implied_views(keys):
    """Who can edit a function can also view it"""
    keys = set(keys)
    return frozenset(keys | {view for view in map(_implied_view, keys) if view})


def requirements_of(key):
    """The keys this key directly needs: its REQUIRES hint plus the view key an edit key implies"""
    implied = _implied_view(key)
    return REQUIRES.get(key, ()) + ((implied,) if implied else ())


def _function_and_level(key):
    head, _, tail = key.rpartition('.')
    return (head, tail) if tail in ('view', 'edit') else (key, 'action')


def _function_kind(levels):
    if 'action' in levels:
        return 'action'
    return 'viewEdit' if len(levels) == 2 else levels[0]


def catalogue_for_api():
    """The catalogue for the role editor, keys only (the labels are in the frontend's translations):
    per module its functions, each a row with a view and/or edit key, or a single action key."""
    modules = []
    for module, keys in CATALOGUE.items():
        functions = {}
        for key, flags in keys.items():
            function, level = _function_and_level(key)
            functions.setdefault(function, []).append({
                'key': key,
                'level': level,
                'readonlyDefault': 'R' in flags,
                'sensitive': 'S' in flags,
                'administratorOnly': 'A' in flags,
                'requires': list(dict.fromkeys(requirements_of(key))),
            })
        modules.append({
            'module': module,
            'functions': [{'function': function, 'kind': _function_kind([p['level'] for p in permissions]),
                           'permissions': permissions} for function, permissions in functions.items()],
        })
    return modules


def enforce_requires(keys):
    """Leave out keys whose requirements are missing, also when that is itself because of a missing one"""
    keys = set(keys)
    while True:
        unmet = {key for key in keys if not keys.issuperset(requirements_of(key))}
        if not unmet:
            return frozenset(keys)
        keys -= unmet
