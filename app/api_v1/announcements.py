"""
Announcements API endpoints
CRUD operations for announcement management
"""

from flask import request, jsonify, current_app, Response
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.api_v1 import api_v1
from app.models import AnnouncementModel, AnnouncementReadModel, UserModel, UserRoleModel, db
from app.rbac import has_permission, permission_denied, require_permission
from datetime import datetime
import json


def _can_see_email_details():
    """Recipient email addresses and raw delivery errors are hidden from the read-only role"""
    return has_permission('announcements.email_details')


def _get_total_targeted(announcement):
    """Calculate total users targeted by announcement"""
    if announcement.target_type == 'all':
        return UserModel.query.count()
    elif announcement.target_type == 'roles':
        user_ids = set()
        for role in announcement.target_roles:
            for user in role.users:
                user_ids.add(user.id)
        return len(user_ids)
    elif announcement.target_type == 'users':
        return len(announcement.target_users)
    return 0


def _get_targeted_users(announcement):
    """Get list of users targeted by announcement"""
    if announcement.target_type == 'all':
        return UserModel.get_all_users()
    elif announcement.target_type == 'roles':
        users = []
        user_ids = set()
        for role in announcement.target_roles:
            for user in role.users:
                if user.id not in user_ids:
                    users.append(user)
                    user_ids.add(user.id)
        return users
    elif announcement.target_type == 'users':
        return announcement.target_users
    return []


def _queue_announcement_emails(announcement):
    """Queue email delivery for a published announcement (sent by the scheduled job).

    Snapshots the targeted users that have an email address. Does not commit.
    """
    if not announcement.send_email:
        return
    announcement.email_status = 'pending'
    announcement.email_attempts = 0
    announcement.email_next_attempt_at = datetime.now()
    announcement.email_last_error = None
    announcement.email_failures = None
    announcement.email_pending_user_ids = json.dumps(
        [u.id for u in _get_targeted_users(announcement) if u.email]
    )


def _send_announcement_email_to_user(announcement, user, frontend_url):
    """Send the announcement mail to one user. Raises on delivery failure."""
    from app.email import send_html_email

    # Add tracking pixel to content
    tracking_url = f"{frontend_url}/api/v1/announcements/{announcement.id}/pixel/{user.id}.gif"
    content_with_tracking = f'{announcement.content}<img src="{tracking_url}" width="1" height="1" style="display:none;" />'

    send_html_email(
        subject=announcement.title,
        recipients=[user.email],
        message=content_with_tracking,
        title=announcement.title,
        link_url=f"{frontend_url}/dashboard",
        link_title="View in Portal",
        raise_errors=True
    )


def _load_id_list(raw):
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _load_failures(raw):
    return [f for f in _load_id_list(raw) if isinstance(f, dict) and 'userId' in f]


def _email_status_fields(a, can_see_details=False):
    """camelCase email delivery fields shared by the admin serializers.

    Raw error text is only included for users who may see recipient details.
    """
    return {
        'emailStatus': a.email_status,
        'emailAttempts': a.email_attempts or 0,
        'emailLastError': a.email_last_error if can_see_details else None,
        'emailNextAttemptAt': a.email_next_attempt_at.isoformat() if a.email_next_attempt_at else None,
        'emailPendingCount': len(_load_id_list(a.email_pending_user_ids)),
        'emailFailureCount': len(_load_failures(a.email_failures))
    }


def _email_detail_fields(a, can_see_details=False):
    """Pending recipients and dropped recipients resolved to users (tolerates deleted users).

    Email addresses and raw errors are only included when can_see_details.
    """
    pending_ids = _load_id_list(a.email_pending_user_ids)
    failures = _load_failures(a.email_failures)
    ids = set(pending_ids) | {f['userId'] for f in failures}
    users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(ids)).all()} if ids else {}

    def resolve(user_id):
        user = users.get(user_id)
        return {
            'userId': user_id,
            'username': user.username if user else None,
            'email': user.email if user and can_see_details else None
        }

    failure_list = []
    for f in failures:
        entry = resolve(f['userId'])
        entry['error'] = f.get('error') if can_see_details else None
        failure_list.append(entry)

    return {
        'emailPending': [resolve(i) for i in pending_ids],
        'emailFailures': failure_list
    }


# ==================== ADMIN ENDPOINTS ====================

@api_v1.route('/admin/announcements', methods=['GET'])
@jwt_required()
@require_permission('announcements.view')
def get_all_announcements():
    """Get all announcements (admin or readonly)"""
    announcements = AnnouncementModel.get_all()
    can_see = _can_see_email_details()

    return jsonify({
        'announcements': [{
            'id': a.id,
            'title': a.title,
            'content': a.content,
            'targetType': a.target_type,
            'status': a.status,
            'sendEmail': a.send_email,
            'scheduledAt': a.scheduled_at.isoformat() if a.scheduled_at else None,
            'sentAt': a.sent_at.isoformat() if a.sent_at else None,
            'createdAt': a.created_at.isoformat(),
            'createdBy': {
                'id': a.creator.id,
                'username': a.creator.username
            } if a.creator else None,
            'targetRoles': [{'id': r.id, 'name': r.name, 'displayName': r.display_name} for r in a.target_roles],
            'targetUsers': [{'id': u.id, 'username': u.username} for u in a.target_users],
            'readCount': len(a.reads),
            'totalTargeted': _get_total_targeted(a),
            **_email_status_fields(a, can_see)
        } for a in announcements]
    }), 200


@api_v1.route('/admin/announcements/<int:announcement_id>', methods=['GET'])
@jwt_required()
@require_permission('announcements.view')
def get_announcement_admin(announcement_id):
    """Get announcement details (admin or readonly)"""
    announcement = AnnouncementModel.get_by_id(announcement_id)
    if not announcement:
        return jsonify({'error': 'Announcement not found'}), 404

    stats = AnnouncementReadModel.get_read_stats(announcement_id)
    can_see = _can_see_email_details()

    return jsonify({
        'id': announcement.id,
        'title': announcement.title,
        'content': announcement.content,
        'targetType': announcement.target_type,
        'status': announcement.status,
        'sendEmail': announcement.send_email,
        'scheduledAt': announcement.scheduled_at.isoformat() if announcement.scheduled_at else None,
        'sentAt': announcement.sent_at.isoformat() if announcement.sent_at else None,
        'createdAt': announcement.created_at.isoformat(),
        'updatedAt': announcement.updated_at.isoformat(),
        'createdBy': {
            'id': announcement.creator.id,
            'username': announcement.creator.username
        } if announcement.creator else None,
        'targetRoles': [{'id': r.id, 'name': r.name, 'displayName': r.display_name} for r in announcement.target_roles],
        'targetUsers': [{'id': u.id, 'username': u.username} for u in announcement.target_users],
        'stats': {
            'totalReads': stats['total_reads'],
            'emailOpens': stats['email_opens'],
            'totalTargeted': _get_total_targeted(announcement)
        },
        'reads': [{
            'userId': r.user_id,
            'username': r.user.username,
            'readAt': r.read_at.isoformat(),
            'emailOpened': r.email_opened,
            'emailOpenedAt': r.email_opened_at.isoformat() if r.email_opened_at else None
        } for r in announcement.reads],
        **_email_status_fields(announcement, can_see),
        **_email_detail_fields(announcement, can_see)
    }), 200


@api_v1.route('/admin/announcements', methods=['POST'])
@jwt_required()
@require_permission('announcements.edit')
def create_announcement():
    """Create a new announcement (admin only)"""
    data = request.get_json()
    current_user_id = int(get_jwt_identity())

    # Publishing now or on a schedule goes out to its audience (and by mail) without another step
    if (data.get('sendImmediately') or data.get('scheduledAt')) and not has_permission('announcements.send'):
        return permission_denied('announcements.send')

    # Validate required fields
    if not data.get('title'):
        return jsonify({'error': 'Title is required'}), 400
    if not data.get('content'):
        return jsonify({'error': 'Content is required'}), 400

    target_type = data.get('targetType', 'all')
    if target_type not in ['all', 'roles', 'users']:
        return jsonify({'error': 'Invalid target type'}), 400

    # Validate target selection
    if target_type == 'roles' and not data.get('roleIds'):
        return jsonify({'error': 'At least one role must be selected'}), 400
    if target_type == 'users' and not data.get('userIds'):
        return jsonify({'error': 'At least one user must be selected'}), 400

    try:
        # Parse scheduled time if provided
        scheduled_at = None
        status = 'draft'

        if data.get('sendImmediately'):
            status = 'sent'
        elif data.get('scheduledAt'):
            scheduled_at = datetime.fromisoformat(data['scheduledAt'].replace('Z', '+00:00'))
            status = 'scheduled'

        announcement = AnnouncementModel(
            title=data['title'],
            content=data['content'],
            target_type=target_type,
            status=status,
            send_email=data.get('sendEmail', False),
            scheduled_at=scheduled_at,
            created_by=current_user_id
        )

        db.session.add(announcement)
        db.session.flush()  # Get ID before adding relationships

        # Add target roles
        if target_type == 'roles' and data.get('roleIds'):
            for role_id in data['roleIds']:
                role = UserRoleModel.get_by_id(role_id)
                if role:
                    announcement.target_roles.append(role)

        # Add target users
        if target_type == 'users' and data.get('userIds'):
            for user_id in data['userIds']:
                user = UserModel.get_user_by_id(user_id)
                if user:
                    announcement.target_users.append(user)

        # If send immediately, publish now; emails are sent by the scheduled job
        if data.get('sendImmediately'):
            announcement.sent_at = datetime.now()
            _queue_announcement_emails(announcement)

        db.session.commit()

        return jsonify({
            'message': 'Announcement created successfully',
            'announcement': {
                'id': announcement.id,
                'title': announcement.title,
                'status': announcement.status
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error creating announcement: {str(e)}")
        return jsonify({'error': f'Failed to create announcement: {str(e)}'}), 400


@api_v1.route('/admin/announcements/<int:announcement_id>', methods=['PUT'])
@jwt_required()
@require_permission('announcements.edit')
def update_announcement(announcement_id):
    """Update an announcement (admin only)"""
    announcement = AnnouncementModel.get_by_id(announcement_id)
    if not announcement:
        return jsonify({'error': 'Announcement not found'}), 404

    # Cannot edit sent announcements
    if announcement.status == 'sent':
        return jsonify({'error': 'Cannot edit sent announcements'}), 400

    data = request.get_json()

    # The job publishes a scheduled announcement as it is then: changing one decides what goes out
    if (announcement.status == 'scheduled' or data.get('scheduledAt')) and not has_permission('announcements.send'):
        return permission_denied('announcements.send')

    if data.get('targetType') and data['targetType'] not in ['all', 'roles', 'users']:
        return jsonify({'error': 'Invalid target type'}), 400

    try:
        if data.get('title'):
            announcement.title = data['title']
        if data.get('content'):
            announcement.content = data['content']
        if 'sendEmail' in data:
            announcement.send_email = data['sendEmail']

        if data.get('targetType'):
            announcement.target_type = data['targetType']

        # Update schedule
        if data.get('scheduledAt'):
            announcement.scheduled_at = datetime.fromisoformat(data['scheduledAt'].replace('Z', '+00:00'))
            announcement.status = 'scheduled'
        elif 'scheduledAt' in data and data['scheduledAt'] is None:
            announcement.scheduled_at = None
            announcement.status = 'draft'

        # Update target roles
        if 'roleIds' in data:
            announcement.target_roles.clear()
            if data['roleIds']:
                for role_id in data['roleIds']:
                    role = UserRoleModel.get_by_id(role_id)
                    if role:
                        announcement.target_roles.append(role)

        # Update target users
        if 'userIds' in data:
            announcement.target_users.clear()
            if data['userIds']:
                for user_id in data['userIds']:
                    user = UserModel.get_user_by_id(user_id)
                    if user:
                        announcement.target_users.append(user)

        db.session.commit()

        return jsonify({'message': 'Announcement updated successfully'}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating announcement: {str(e)}")
        return jsonify({'error': f'Failed to update announcement: {str(e)}'}), 400


@api_v1.route('/admin/announcements/<int:announcement_id>', methods=['DELETE'])
@jwt_required()
@require_permission('announcements.delete')
def delete_announcement(announcement_id):
    """Delete an announcement (admin only)"""
    result = AnnouncementModel.delete_by_id(announcement_id)
    if 'error' in result:
        return jsonify(result), 404

    return jsonify(result), 200


@api_v1.route('/admin/announcements/<int:announcement_id>/send', methods=['POST'])
@jwt_required()
@require_permission('announcements.send')
def send_announcement_now(announcement_id):
    """Send an announcement immediately (admin only)"""
    announcement = AnnouncementModel.get_by_id(announcement_id)
    if not announcement:
        return jsonify({'error': 'Announcement not found'}), 404

    if announcement.status == 'sent':
        return jsonify({'error': 'Announcement already sent'}), 400

    try:
        announcement.status = 'sent'
        announcement.sent_at = datetime.now()
        _queue_announcement_emails(announcement)
        db.session.commit()

        return jsonify({'message': 'Announcement sent successfully'}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error sending announcement: {str(e)}")
        return jsonify({'error': f'Failed to send announcement: {str(e)}'}), 400


# ==================== USER ENDPOINTS ====================

@api_v1.route('/announcements', methods=['GET'])
@jwt_required()
def get_user_announcements():
    """Get announcements for the current user (excludes dismissed)"""
    current_user_id = int(get_jwt_identity())

    announcements = AnnouncementModel.get_user_announcements(current_user_id)

    # Filter out dismissed announcements
    visible_announcements = [
        a for a in announcements
        if not AnnouncementReadModel.is_dismissed(a.id, current_user_id)
    ]

    return jsonify({
        'announcements': [{
            'id': a.id,
            'title': a.title,
            'content': a.content,
            'sentAt': a.sent_at.isoformat() if a.sent_at else None,
            'isRead': AnnouncementReadModel.is_read(a.id, current_user_id)
        } for a in visible_announcements]
    }), 200


@api_v1.route('/announcements/unread-count', methods=['GET'])
@jwt_required()
def get_unread_count():
    """Get count of unread announcements for current user (excludes dismissed)"""
    current_user_id = int(get_jwt_identity())

    announcements = AnnouncementModel.get_user_announcements(current_user_id)

    # Filter out dismissed and count only unread
    unread_count = sum(
        1 for a in announcements
        if not AnnouncementReadModel.is_read(a.id, current_user_id)
        and not AnnouncementReadModel.is_dismissed(a.id, current_user_id)
    )

    return jsonify({'unreadCount': unread_count}), 200


@api_v1.route('/announcements/<int:announcement_id>/read', methods=['POST'])
@jwt_required()
def mark_announcement_read(announcement_id):
    """Mark an announcement as read"""
    current_user_id = int(get_jwt_identity())

    announcement = AnnouncementModel.get_by_id(announcement_id)
    if not announcement:
        return jsonify({'error': 'Announcement not found'}), 404

    AnnouncementReadModel.mark_as_read(announcement_id, current_user_id)

    return jsonify({'message': 'Marked as read'}), 200


@api_v1.route('/announcements/<int:announcement_id>/dismiss', methods=['POST'])
@jwt_required()
def dismiss_announcement(announcement_id):
    """Dismiss an announcement (hide from user's list)"""
    current_user_id = int(get_jwt_identity())

    announcement = AnnouncementModel.get_by_id(announcement_id)
    if not announcement:
        return jsonify({'error': 'Announcement not found'}), 404

    AnnouncementReadModel.dismiss(announcement_id, current_user_id)

    return jsonify({'message': 'Announcement dismissed'}), 200


@api_v1.route('/announcements/history', methods=['GET'])
@jwt_required()
def get_announcement_history():
    """Get all announcements for the current user (including dismissed ones)"""
    current_user_id = int(get_jwt_identity())

    announcements = AnnouncementModel.get_user_announcements(current_user_id)

    return jsonify({
        'announcements': [{
            'id': a.id,
            'title': a.title,
            'content': a.content,
            'sentAt': a.sent_at.isoformat() if a.sent_at else None,
            'isRead': AnnouncementReadModel.is_read(a.id, current_user_id),
            'isDismissed': AnnouncementReadModel.is_dismissed(a.id, current_user_id)
        } for a in announcements]
    }), 200


# ==================== EMAIL TRACKING PIXEL ====================

@api_v1.route('/announcements/<int:announcement_id>/pixel/<int:user_id>.gif', methods=['GET'])
def track_email_open(announcement_id, user_id):
    """Tracking pixel for email opens - returns 1x1 transparent GIF"""
    # Mark as read when email is opened (also tracks email open)
    AnnouncementReadModel.mark_as_read(announcement_id, user_id)
    AnnouncementReadModel.mark_email_opened(announcement_id, user_id)

    # Return 1x1 transparent GIF
    gif_bytes = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'

    return Response(gif_bytes, mimetype='image/gif')
