"""
Logo API endpoints
Upload and manage custom logo for the portal
"""

from flask import request, jsonify, current_app, send_from_directory
from flask_jwt_extended import jwt_required
from app.api_v1 import api_v1
from app.models import SystemSettingsModel
from app import db
from werkzeug.utils import secure_filename
import os
import uuid

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2MB

LOGIN_BG_ALLOWED_EXTENSIONS = ('png', 'jpg', 'jpeg', 'webp')
LOGIN_BG_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB


def _is_login_bg_image(file):
    """Check the file's magic bytes match one of the allowed background image types"""
    header = file.read(12)
    file.seek(0)
    return (header.startswith(b'\x89PNG\r\n\x1a\n')
            or header.startswith(b'\xff\xd8\xff')
            or (header[:4] == b'RIFF' and header[8:12] == b'WEBP'))


def get_logo_upload_folder():
    """Get the path to the logo upload folder (in instance folder for persistence)"""
    instance_path = current_app.instance_path
    upload_folder = os.path.join(instance_path, 'uploads')
    os.makedirs(upload_folder, exist_ok=True)
    return upload_folder


def _save_upload(file, prefix, allowed, max_size):
    """
    Validate an uploaded file and save it to the upload folder as
    <prefix>_<uuid8>.<ext> (a new name per upload avoids caching issues).
    Returns (filename, None) on success or (None, error_message).
    """
    if file.filename == '':
        return None, 'No file selected'

    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed):
        return None, f'Invalid file type. Allowed: {", ".join(allowed)}'

    # Check file size
    file.seek(0, 2)  # Seek to end
    size = file.tell()
    file.seek(0)  # Reset to beginning
    if size > max_size:
        return None, f'File too large. Maximum size: {max_size // (1024*1024)}MB'

    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = secure_filename(f"{prefix}_{uuid.uuid4().hex[:8]}.{ext}")
    file.save(os.path.join(get_logo_upload_folder(), filename))
    return filename, None


def _remove_upload(path):
    """Delete a previously uploaded file referenced by its stored URL/path, if present."""
    if not path:
        return
    # Handle both old path format (/static/img/custom/) and new format (/api/v1/uploads/)
    old_filename = os.path.basename(path)
    # Try new location first (instance/uploads)
    old_file = os.path.join(get_logo_upload_folder(), old_filename)
    if os.path.exists(old_file):
        os.remove(old_file)
    else:
        # Try old location (static/img/custom)
        old_static_path = os.path.join(current_app.root_path, 'static', 'img', 'custom', old_filename)
        if os.path.exists(old_static_path):
            os.remove(old_static_path)


@api_v1.route('/admin/logo', methods=['POST'])
@jwt_required()
def upload_logo():
    """
    Upload a custom logo (admin only)
    ---
    tags:
      - Logo
    security:
      - Bearer: []
    consumes:
      - multipart/form-data
    parameters:
      - name: logo
        in: formData
        type: file
        required: true
        description: Logo image file (PNG, JPG, JPEG, GIF, max 2MB)
    responses:
      200:
        description: Logo uploaded successfully
      400:
        description: Invalid file or no file provided
      403:
        description: Admin access required
      500:
        description: Failed to upload logo
    """
    from app.rbac import has_any_role
    if not has_any_role(['administrator', 'settings_admin']):
        return jsonify({'error': 'Settings admin access required'}), 403

    if 'logo' not in request.files:
        return jsonify({'error': 'No logo file provided'}), 400

    try:
        filename, error = _save_upload(request.files['logo'], 'logo', ALLOWED_EXTENSIONS, MAX_FILE_SIZE)
        if error:
            return jsonify({'error': error}), 400

        old_logo_path = SystemSettingsModel.get_setting('custom_logo_path')

        # Update settings - use API route to serve from instance folder
        logo_url = f'/api/v1/uploads/{filename}'

        # Update or create settings
        logo_enabled_setting = SystemSettingsModel.query.filter_by(key='custom_logo_enabled').first()
        if logo_enabled_setting:
            logo_enabled_setting.value = 'true'
        else:
            logo_enabled_setting = SystemSettingsModel(
                key='custom_logo_enabled',
                value='true',
                category='branding',
                description='Enable custom logo display'
            )
            db.session.add(logo_enabled_setting)

        logo_path_setting = SystemSettingsModel.query.filter_by(key='custom_logo_path').first()
        if logo_path_setting:
            logo_path_setting.value = logo_url
        else:
            logo_path_setting = SystemSettingsModel(
                key='custom_logo_path',
                value=logo_url,
                category='branding',
                description='Path to uploaded custom logo file'
            )
            db.session.add(logo_path_setting)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            _remove_upload(logo_url)
            current_app.logger.error(f"Error uploading logo: {str(e)}")
            return jsonify({'error': 'Failed to upload logo'}), 500

        # Delete old custom logo if exists (only after the new one is committed)
        _remove_upload(old_logo_path)

        current_app.logger.info(f"Logo uploaded successfully: {logo_url}")

        return jsonify({
            'message': 'Logo uploaded successfully',
            'logo_path': logo_url
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error uploading logo: {str(e)}")
        return jsonify({'error': 'Failed to upload logo'}), 500


@api_v1.route('/admin/logo', methods=['DELETE'])
@jwt_required()
def delete_logo():
    """
    Delete custom logo and reset to default
    ---
    tags:
      - Logo
    security:
      - Bearer: []
    responses:
      200:
        description: Logo reset to default
      403:
        description: Admin access required
      500:
        description: Failed to delete logo
    """
    from app.rbac import has_any_role
    if not has_any_role(['administrator', 'settings_admin']):
        return jsonify({'error': 'Settings admin access required'}), 403

    try:
        _remove_upload(SystemSettingsModel.get_setting('custom_logo_path'))

        # Update settings
        logo_enabled_setting = SystemSettingsModel.query.filter_by(key='custom_logo_enabled').first()
        if logo_enabled_setting:
            logo_enabled_setting.value = 'false'

        logo_path_setting = SystemSettingsModel.query.filter_by(key='custom_logo_path').first()
        if logo_path_setting:
            logo_path_setting.value = ''

        db.session.commit()

        current_app.logger.info("Logo reset to default")

        return jsonify({'message': 'Logo reset to default'}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting logo: {str(e)}")
        return jsonify({'error': 'Failed to delete logo'}), 500


@api_v1.route('/admin/login-background', methods=['POST'])
@jwt_required()
def upload_login_background():
    """
    Upload a login page background image (admin only)
    ---
    tags:
      - Logo
    security:
      - Bearer: []
    consumes:
      - multipart/form-data
    parameters:
      - name: background
        in: formData
        type: file
        required: true
        description: Background image file (PNG, JPG, JPEG, WebP, max 5MB)
    responses:
      200:
        description: Background uploaded successfully
      400:
        description: Invalid file or no file provided
      403:
        description: Admin access required
      500:
        description: Failed to upload background
    """
    from app.rbac import has_any_role
    if not has_any_role(['administrator', 'settings_admin']):
        return jsonify({'error': 'Settings admin access required'}), 403

    if 'background' not in request.files:
        return jsonify({'error': 'No background file provided'}), 400

    file = request.files['background']
    if file.filename and not _is_login_bg_image(file):
        return jsonify({'error': f'Invalid file type. Allowed: {", ".join(LOGIN_BG_ALLOWED_EXTENSIONS)}'}), 400

    try:
        filename, error = _save_upload(file, 'login_bg', LOGIN_BG_ALLOWED_EXTENSIONS, LOGIN_BG_MAX_FILE_SIZE)
        if error:
            return jsonify({'error': error}), 400

        old_background_path = SystemSettingsModel.get_setting('login_background_path')
        background_url = f'/api/v1/uploads/{filename}'
        if not SystemSettingsModel.set_setting(
            'login_background_path', background_url, category='branding',
            description='Path to uploaded login page background image'
        ):
            _remove_upload(background_url)
            return jsonify({'error': 'Failed to upload login background'}), 500

        # Delete old background if exists
        _remove_upload(old_background_path)

        current_app.logger.info(f"Login background uploaded successfully: {background_url}")

        return jsonify({
            'message': 'Login background uploaded successfully',
            'login_background_path': background_url
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error uploading login background: {str(e)}")
        return jsonify({'error': 'Failed to upload login background'}), 500


@api_v1.route('/admin/login-background', methods=['DELETE'])
@jwt_required()
def delete_login_background():
    """
    Delete the login page background image
    ---
    tags:
      - Logo
    security:
      - Bearer: []
    responses:
      200:
        description: Login background removed
      403:
        description: Admin access required
      500:
        description: Failed to delete background
    """
    from app.rbac import has_any_role
    if not has_any_role(['administrator', 'settings_admin']):
        return jsonify({'error': 'Settings admin access required'}), 403

    try:
        _remove_upload(SystemSettingsModel.get_setting('login_background_path'))

        setting = SystemSettingsModel.query.filter_by(key='login_background_path').first()
        if setting:
            setting.value = ''
        db.session.commit()

        current_app.logger.info("Login background removed")

        return jsonify({'message': 'Login background removed'}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting login background: {str(e)}")
        return jsonify({'error': 'Failed to delete login background'}), 500


@api_v1.route('/admin/logo', methods=['GET'])
@jwt_required()
def get_logo_settings():
    """
    Get current logo settings
    ---
    tags:
      - Logo
    security:
      - Bearer: []
    responses:
      200:
        description: Current logo settings
        schema:
          type: object
          properties:
            custom_logo_enabled:
              type: boolean
            custom_logo_path:
              type: string
            logo_display_mode:
              type: string
            default_logo_path:
              type: string
      403:
        description: Admin access required
    """
    from app.rbac import has_any_role
    if not has_any_role(['administrator', 'settings_admin']):
        return jsonify({'error': 'Settings admin access required'}), 403

    enabled = SystemSettingsModel.get_setting('custom_logo_enabled')
    custom_logo_enabled = enabled and str(enabled).lower() == 'true'

    return jsonify({
        'custom_logo_enabled': custom_logo_enabled,
        'custom_logo_path': SystemSettingsModel.get_setting('custom_logo_path') or '',
        'logo_display_mode': SystemSettingsModel.get_setting('logo_display_mode') or 'logo_and_text',
        'default_logo_path': '/static/img/logo.png'
    }), 200


@api_v1.route('/uploads/<filename>', methods=['GET'])
def serve_upload(filename):
    """
    Serve uploaded files (logos, etc.) from the instance folder
    This ensures files persist across container restarts
    """
    # Sanitize filename to prevent directory traversal
    filename = secure_filename(filename)
    upload_folder = get_logo_upload_folder()

    if not os.path.exists(os.path.join(upload_folder, filename)):
        return jsonify({'error': 'File not found'}), 404

    return send_from_directory(upload_folder, filename)
