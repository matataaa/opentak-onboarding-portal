"""
Settings API endpoint
Returns application configuration settings for the frontend
"""

from flask import jsonify, current_app, request
from flask_jwt_extended import jwt_required, get_jwt
from app.api_v1 import api_v1
from app.models import SystemSettingsModel
from app.permissions import SETTING_PERMISSIONS, SETTINGS_EDIT_PERMISSIONS
from app.rbac import has_permission, is_administrator, permission_denied
from app import db
import json

LOGIN_HERO_LANGUAGES = ('en', 'nl', 'de')
LOGIN_HERO_FIELDS = ('eyebrow', 'headline', 'description', 'footer')
LOGIN_HERO_MAX_LENGTH = 300
# Settings restricted to a fixed set of values (not only login settings)
LOGIN_ENUM_SETTINGS = {
    'login_layout': ('classic', 'centered', 'split'),
    'login_overlay_strength': ('none', 'light', 'medium', 'strong'),
    'date_format': ('eu', 'us', 'iso'),
    'kiosk_language': ('', 'en', 'nl', 'de'),
    'meshtastic_enabled': ('true', 'false'),
    'radios_enabled': ('true', 'false'),
    'events_enabled': ('true', 'false'),
}


def _can_edit_some_setting():
    return any(has_permission(key) for key in SETTINGS_EDIT_PERMISSIONS)


def _can_edit_setting(key):
    """Settings are authorised per key; one without a permission is for an administrator only"""
    permission = SETTING_PERMISSIONS.get(key)
    return has_permission(permission) if permission else is_administrator()


def _parse_login_hero_texts(raw):
    """Parse the stored login_hero_texts JSON, returning {} when it is missing or malformed"""
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate_setting_value(key, value):
    """Return an error message if the value is invalid for the given setting key, else None"""
    if key == 'login_background_path':
        return 'login_background_path can only be changed through /api/v1/admin/login-background'
    if key in LOGIN_ENUM_SETTINGS:
        allowed = LOGIN_ENUM_SETTINGS[key]
        # JSON booleans are stored as 'true'/'false'
        if isinstance(value, bool):
            value = str(value).lower()
        if value not in allowed:
            return f'{key} must be one of: {", ".join(allowed)}'
        return None
    if key != 'login_hero_texts':
        return None
    error = ('login_hero_texts must be a JSON object keyed by language (en, nl, de) with '
             f'string fields {", ".join(LOGIN_HERO_FIELDS)} of at most {LOGIN_HERO_MAX_LENGTH} characters')
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return error
    if not isinstance(parsed, dict):
        return error
    for lang, texts in parsed.items():
        if lang not in LOGIN_HERO_LANGUAGES or not isinstance(texts, dict):
            return error
        for field, text in texts.items():
            if field not in LOGIN_HERO_FIELDS or not isinstance(text, str) or len(text) > LOGIN_HERO_MAX_LENGTH:
                return error
    return None


@api_v1.route('/settings', methods=['GET'])
def get_settings():
    """
    Get public application settings

    Returns configuration that the frontend needs to know about,
    such as feature flags and display settings.

    Response:
    {
        "brand_name": "string",
        "primary_color": "string",
        "accent_color": "string",
        "logo_path": "string",
        "generate_atak_qr_code": boolean,
        "ots_hostname": "string",
        "meshtastic_homepage_icon_enabled": boolean,
        "meshtastic_enabled": boolean,
        "radios_enabled": boolean,
        "events_enabled": boolean
    }
    """
    # Helper function to get boolean setting from DB
    def get_bool_setting(key, default=False):
        db_value = SystemSettingsModel.get_setting(key)
        if db_value is not None and db_value != '':
            if isinstance(db_value, bool):
                return db_value
            return str(db_value).lower() == 'true'
        return default

    # Helper function to get string setting from DB
    def get_str_setting(key, default=''):
        db_value = SystemSettingsModel.get_setting(key)
        if db_value is not None:
            return db_value
        return default

    # Get enabled/value pairs for configurable settings
    brand_name_enabled = get_bool_setting('brand_name_enabled', True)
    brand_name_value = get_str_setting('brand_name_value', 'My OTS Portal')

    # Switchable modules; the fine-grained flags below are effective (module AND setting)
    meshtastic_enabled = SystemSettingsModel.is_module_enabled('meshtastic')
    radios_enabled = SystemSettingsModel.is_module_enabled('radios')
    events_enabled = SystemSettingsModel.is_module_enabled('events')

    settings = {
        # Brand name - only show if enabled
        'brand_name': brand_name_value if brand_name_enabled else 'My OTS Portal',
        # Theme colors from database
        'primary_color': get_str_setting('primary_color', '#000000'),
        'accent_color': get_str_setting('accent_color', '#ff9800'),
        'logo_path': '/static/img/logo.png',  # Default logo path
        'generate_atak_qr_code': get_bool_setting('generate_atak_qr_code', True),
        'generate_itak_qr_code': get_bool_setting('generate_itak_qr_code', True),
        'ots_hostname': current_app.config.get('OTS_HOSTNAME', ''),
        'ots_url': current_app.config.get('OTS_URL', ''),
        'meshtastic_homepage_icon_enabled': meshtastic_enabled and get_bool_setting('meshtastic_homepage_icon_enabled', True),
        'atak_homepage_icon_enabled': get_bool_setting('atak_homepage_icon_enabled', True),
        'itak_homepage_icon_enabled': get_bool_setting('itak_homepage_icon_enabled', True),
        'truststore_homepage_icon_enabled': get_bool_setting('truststore_homepage_icon_enabled', False),
        # Installer QR codes
        'atak_installer_qr_enabled': get_bool_setting('atak_installer_qr_enabled', True),
        'atak_installer_qr_url': get_str_setting('atak_installer_qr_url', 'https://play.google.com/store/apps/details?id=com.atakmap.app.civ&hl=en'),
        'itak_installer_qr_enabled': get_bool_setting('itak_installer_qr_enabled', True),
        'itak_installer_qr_url': get_str_setting('itak_installer_qr_url', 'https://apps.apple.com/app/itak/id1561656396'),
        'meshtastic_installer_qr_android_enabled': get_bool_setting('meshtastic_installer_qr_android_enabled', True),
        'meshtastic_installer_qr_android_url': get_str_setting('meshtastic_installer_qr_android_url', 'https://play.google.com/store/apps/details?id=com.geeksville.mesh'),
        'meshtastic_installer_qr_iphone_enabled': get_bool_setting('meshtastic_installer_qr_iphone_enabled', True),
        'meshtastic_installer_qr_iphone_url': get_str_setting('meshtastic_installer_qr_iphone_url', 'https://apps.apple.com/app/meshtastic/id1586432531'),
        # Callsign QR code
        'callsign_qr_code_enabled': get_bool_setting('callsign_qr_code_enabled', True),
        # Logo settings
        'custom_logo_enabled': get_bool_setting('custom_logo_enabled', False),
        'custom_logo_path': get_str_setting('custom_logo_path', ''),
        'logo_display_mode': get_str_setting('logo_display_mode', 'logo_and_text'),
        'default_logo_path': '/static/img/logo.png',
        # Login page appearance
        'login_layout': get_str_setting('login_layout', 'classic'),
        'login_background_path': get_str_setting('login_background_path', ''),
        'login_overlay_strength': get_str_setting('login_overlay_strength', 'medium'),
        'login_glass_enabled': get_bool_setting('login_glass_enabled', True),
        'login_header_bar_enabled': get_bool_setting('login_header_bar_enabled', False),
        'login_hero_texts': _parse_login_hero_texts(get_str_setting('login_hero_texts', '')),
        # Modules
        'meshtastic_enabled': meshtastic_enabled,
        'radios_enabled': radios_enabled,
        'events_enabled': events_enabled,
        # Radio settings (programming, validation and enrolment also need Meshtastic)
        'claim_radio_enabled': radios_enabled and get_bool_setting('claim_radio_enabled', False),
        'user_program_radio_enabled': radios_enabled and meshtastic_enabled and get_bool_setting('user_program_radio_enabled', False),
        'user_validate_radio_enabled': radios_enabled and meshtastic_enabled and get_bool_setting('user_validate_radio_enabled', False),
        'user_radio_enrollment_enabled': radios_enabled and meshtastic_enabled and get_bool_setting('user_radio_enrollment_enabled', False),
        # OIDC / SSO
        'oidc_enabled': get_bool_setting('oidc_enabled', False),
        'oidc_auto_create_user': get_bool_setting('oidc_auto_create_user', False),
        'forgot_password_enabled': get_bool_setting('forgot_password_enabled', True),
        # Kiosk
        'kiosk_enrollment_enabled': get_bool_setting('kiosk_enrollment_enabled', False),
        'kiosk_session_timeout_minutes': get_str_setting('kiosk_session_timeout_minutes', '10'),
        # Magic Link
        'magic_link_login_enabled': get_bool_setting('magic_link_login_enabled', False),
        # Theme
        'default_theme': get_str_setting('default_theme', 'light'),
        'kiosk_default_theme': get_str_setting('kiosk_default_theme', 'dark'),
        'kiosk_language': get_str_setting('kiosk_language', ''),
        'date_format': get_str_setting('date_format', 'eu'),
        # Help
        'help_link_enabled': get_bool_setting('help_link_enabled', False),
        'help_link_value': get_str_setting('help_link_value', ''),
        'help_email_enabled': get_bool_setting('help_email_enabled', False),
        'help_email_value': get_str_setting('help_email_value', ''),
        'open_in_atak_enabled': get_bool_setting('open_in_atak_enabled', True),
    }

    return jsonify(settings), 200


@api_v1.route('/admin/settings', methods=['GET'])
@jwt_required()
def get_admin_settings():
    """
    Get all system settings grouped by category
    ---
    tags:
      - Settings
    security:
      - Bearer: []
    responses:
      200:
        description: System settings grouped by category
        schema:
          type: object
          properties:
            notifications:
              type: array
              items:
                type: object
                properties:
                  id:
                    type: integer
                  key:
                    type: string
                  value:
                    type: string
                  description:
                    type: string
            email:
              type: array
              items:
                type: object
            security:
              type: array
              items:
                type: object
            general:
              type: array
              items:
                type: object
      403:
        description: Admin access required
      500:
        description: Failed to retrieve settings
    """
    try:
        if not has_permission('settings.view'):
            return permission_denied('settings.view')

        # Initialize defaults if needed
        SystemSettingsModel.initialize_defaults()

        # Get settings by category
        settings_by_category = {}
        categories = ['notifications', 'registration', 'email', 'security', 'qr_enrollment', 'branding', 'radios', 'kiosk', 'appearance', 'modules', 'general']

        for category in categories:
            settings = SystemSettingsModel.get_category_settings(category)
            settings_by_category[category] = [
                {
                    'id': s.id,
                    'key': s.key,
                    'value': s.value,
                    'description': s.description,
                    # Whether the current user may change it, so the page can disable what it may not
                    'editable': _can_edit_setting(s.key)
                }
                for s in settings
            ]

        return jsonify(settings_by_category), 200

    except Exception as e:
        current_app.logger.error(f"Error getting admin settings: {str(e)}")
        return jsonify({'error': 'Failed to retrieve settings'}), 500


@api_v1.route('/admin/settings/<int:setting_id>', methods=['PUT'])
@jwt_required()
def update_admin_setting(setting_id):
    """
    Update a system setting by ID (admin only)
    ---
    tags:
      - Settings
    security:
      - Bearer: []
    parameters:
      - name: setting_id
        in: path
        type: integer
        required: true
        description: The ID of the setting to update
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - value
          properties:
            value:
              type: string
              description: The new value for the setting (can be "true"/"false" for booleans)
    responses:
      200:
        description: Setting updated successfully
        schema:
          type: object
          properties:
            message:
              type: string
            setting:
              type: object
              properties:
                id:
                  type: integer
                key:
                  type: string
                value:
                  type: string
                description:
                  type: string
      400:
        description: Value is required or invalid for this setting
      403:
        description: Admin access required
      404:
        description: Setting not found
      500:
        description: Failed to update setting
    """
    try:
        if not _can_edit_some_setting():
            return permission_denied('settings.general.edit')

        data = request.get_json()
        if not data or 'value' not in data:
            return jsonify({'error': 'Value is required'}), 400

        # Get the setting
        setting = SystemSettingsModel.query.get(setting_id)
        if not setting:
            return jsonify({'error': 'Setting not found'}), 404

        if not _can_edit_setting(setting.key):
            return permission_denied('settings.general.edit')

        error = _validate_setting_value(setting.key, data['value'])
        if error:
            return jsonify({'error': error}), 400

        # Update the setting
        setting.value = str(data['value']).lower() if isinstance(data['value'], bool) else data['value']
        db.session.commit()

        current_app.logger.info(f"Setting '{setting.key}' updated to '{setting.value}'")

        return jsonify({
            'message': 'Setting updated successfully',
            'setting': {
                'id': setting.id,
                'key': setting.key,
                'value': setting.value,
                'description': setting.description
            }
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating setting: {str(e)}")
        return jsonify({'error': 'Failed to update setting'}), 500


@api_v1.route('/admin/settings/key/<key>', methods=['PUT'])
@jwt_required()
def update_admin_setting_by_key(key):
    """
    Update a system setting by key name (admin only)
    ---
    tags:
      - Settings
    security:
      - Bearer: []
    parameters:
      - name: key
        in: path
        type: string
        required: true
        description: The key name of the setting to update (e.g., "notify_admin_pending_registration")
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - value
          properties:
            value:
              type: string
              description: The new value for the setting (can be "true"/"false" for booleans)
    responses:
      200:
        description: Setting updated successfully
        schema:
          type: object
          properties:
            message:
              type: string
            setting:
              type: object
              properties:
                id:
                  type: integer
                key:
                  type: string
                value:
                  type: string
                description:
                  type: string
      400:
        description: Value is required or invalid for this setting
      403:
        description: Admin access required
      404:
        description: Setting not found
      500:
        description: Failed to update setting
    """
    try:
        if not _can_edit_some_setting():
            return permission_denied('settings.general.edit')

        data = request.get_json()
        if not data or 'value' not in data:
            return jsonify({'error': 'Value is required'}), 400

        # Get the setting
        setting = SystemSettingsModel.query.filter_by(key=key).first()
        if not setting:
            return jsonify({'error': 'Setting not found'}), 404

        if not _can_edit_setting(setting.key):
            return permission_denied('settings.general.edit')

        error = _validate_setting_value(setting.key, data['value'])
        if error:
            return jsonify({'error': error}), 400

        # Update the setting
        setting.value = str(data['value']).lower() if isinstance(data['value'], bool) else data['value']
        db.session.commit()

        current_app.logger.info(f"Setting '{setting.key}' updated to '{setting.value}'")

        return jsonify({
            'message': 'Setting updated successfully',
            'setting': {
                'id': setting.id,
                'key': setting.key,
                'value': setting.value,
                'description': setting.description
            }
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating setting: {str(e)}")
        return jsonify({'error': 'Failed to update setting'}), 500
