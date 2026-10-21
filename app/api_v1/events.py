"""
Events module API endpoints.

Admin CRUD for reusable teams and time-windowed events. This is the opt-in
events management surface; the public link flow and session handling live
elsewhere.
"""

import re
import hashlib
import secrets

from flask import request, jsonify, current_app
from flask_jwt_extended import jwt_required
from datetime import datetime

from app.api_v1 import api_v1
from app.models import db, EventTeamModel, EventModel, EventLinkModel, OTSGroupModel, UserModel
from app.ots import otsClient
from app.rbac import require_role

OTS_USERNAME_RE = re.compile(r'^[a-z0-9_.]+$')
LINK_ROLES = ('leader', 'player')


def _hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _generate_link_token():
    raw = secrets.token_urlsafe(32)
    return raw, _hash_token(raw)


def _link_url(token):
    base = (current_app.config.get('FRONTEND_URL') or '').rstrip('/')
    return "%s/events/join/%s" % (base, token)


def _link_to_dict(link, raw_token=None):
    data = {
        'id': link.id,
        'eventId': link.eventId,
        'eventTeamId': link.eventTeamId,
        'team': {'id': link.team.id, 'name': link.team.name} if link.team else None,
        'role': link.role,
        'tokenVersion': link.tokenVersion,
        'createdAt': link.createdAt.isoformat() if link.createdAt else None,
    }
    # The raw token is only returned once, when generated or regenerated.
    if raw_token is not None:
        data['token'] = raw_token
        data['url'] = _link_url(raw_token)
    return data

PLAYER_ACCESS_MODES = ('shared', 'individual')
ACCOUNT_CLEANUP_MODES = ('deactivate', 'delete')


def _parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _team_to_dict(team):
    return {
        'id': team.id,
        'name': team.name,
        'color': team.color,
        'otsGroupId': team.otsGroupId,
        'otsGroup': {
            'id': team.otsGroup.id,
            'name': team.otsGroup.name,
            'displayName': team.otsGroup.display_name,
        } if team.otsGroup else None,
        'leaderUserId': team.leaderUserId,
        'leaderUser': {
            'id': team.leaderUser.id,
            'username': team.leaderUser.username,
        } if team.leaderUser else None,
        'createdAt': team.createdAt.isoformat() if team.createdAt else None,
    }


def _event_to_dict(event):
    return {
        'id': event.id,
        'name': event.name,
        'description': event.description,
        'validFrom': event.validFrom.isoformat() if event.validFrom else None,
        'validUntil': event.validUntil.isoformat() if event.validUntil else None,
        'cutoffTime': event.cutoffTime,
        'timezone': event.timezone,
        'playerAccessMode': event.playerAccessMode,
        'accountCleanup': event.accountCleanup,
        'active': event.active,
        'createdAt': event.createdAt.isoformat() if event.createdAt else None,
    }


# ----------------------------------------------------------------------------
# Teams
# ----------------------------------------------------------------------------
@api_v1.route('/events/teams', methods=['GET'])
@jwt_required()
@require_role('events_admin', 'events_readonly')
def list_event_teams():
    teams = EventTeamModel.query.order_by(EventTeamModel.name).all()
    return jsonify({'teams': [_team_to_dict(t) for t in teams]}), 200


@api_v1.route('/events/teams/<int:team_id>', methods=['GET'])
@jwt_required()
@require_role('events_admin', 'events_readonly')
def get_event_team(team_id):
    team = EventTeamModel.query.get(team_id)
    if not team:
        return jsonify({'error': 'Team not found'}), 404
    return jsonify(_team_to_dict(team)), 200


@api_v1.route('/events/teams', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def create_event_team():
    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'error': 'name is required'}), 400
    if EventTeamModel.query.filter_by(name=data['name']).first():
        return jsonify({'error': 'A team with this name already exists'}), 409

    if data.get('otsGroupId') and not OTSGroupModel.query.get(data['otsGroupId']):
        return jsonify({'error': 'Unknown otsGroupId'}), 400
    if data.get('leaderUserId') and not UserModel.get_user_by_id(data['leaderUserId']):
        return jsonify({'error': 'Unknown leaderUserId'}), 400

    team = EventTeamModel(
        name=data['name'],
        color=data.get('color'),
        otsGroupId=data.get('otsGroupId'),
        leaderUserId=data.get('leaderUserId'),
    )
    db.session.add(team)
    db.session.commit()
    return jsonify(_team_to_dict(team)), 201


@api_v1.route('/events/teams/<int:team_id>', methods=['PUT'])
@jwt_required()
@require_role('events_admin')
def update_event_team(team_id):
    team = EventTeamModel.query.get(team_id)
    if not team:
        return jsonify({'error': 'Team not found'}), 404
    data = request.get_json() or {}

    if 'name' in data:
        if not data['name']:
            return jsonify({'error': 'name cannot be empty'}), 400
        clash = EventTeamModel.query.filter_by(name=data['name']).first()
        if clash and clash.id != team.id:
            return jsonify({'error': 'A team with this name already exists'}), 409
        team.name = data['name']
    if 'color' in data:
        team.color = data['color']
    if 'otsGroupId' in data:
        if data['otsGroupId'] and not OTSGroupModel.query.get(data['otsGroupId']):
            return jsonify({'error': 'Unknown otsGroupId'}), 400
        team.otsGroupId = data['otsGroupId']
    if 'leaderUserId' in data:
        if data['leaderUserId'] and not UserModel.get_user_by_id(data['leaderUserId']):
            return jsonify({'error': 'Unknown leaderUserId'}), 400
        team.leaderUserId = data['leaderUserId']

    db.session.commit()
    return jsonify(_team_to_dict(team)), 200


@api_v1.route('/events/teams/<int:team_id>', methods=['DELETE'])
@jwt_required()
@require_role('events_admin')
def delete_event_team(team_id):
    team = EventTeamModel.query.get(team_id)
    if not team:
        return jsonify({'error': 'Team not found'}), 404
    db.session.delete(team)
    db.session.commit()
    return jsonify({'message': 'Team deleted'}), 200


@api_v1.route('/events/teams/<int:team_id>/leader', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def create_event_team_leader(team_id):
    """Create a shared leader account (OTS + local) and assign it to the team."""
    team = EventTeamModel.query.get(team_id)
    if not team:
        return jsonify({'error': 'Team not found'}), 404
    if team.leaderUserId:
        return jsonify({'error': 'Team already has a leader account; clear it first'}), 409

    data = request.get_json() or {}
    username = (data.get('username') or '').strip().lower()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'error': 'username and password are required'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    if not OTS_USERNAME_RE.match(username):
        return jsonify({'error': 'Username can contain only letters, numbers, underscores and periods'}), 400
    if UserModel.get_user_by_username(username):
        return jsonify({'error': 'A user with this username already exists'}), 409

    # OTS is the source of truth for accounts, so create there first.
    try:
        otsClient.create_user(username, password, roles=['user'])
    except Exception:
        current_app.logger.exception('Failed to create leader account in OTS: %s', username)
        return jsonify({'error': 'Failed to create leader account in OTS'}), 400

    user = UserModel.create_user(username=username)
    if isinstance(user, dict):
        # Local creation failed after OTS succeeded; roll back OTS to avoid drift.
        try:
            otsClient.deactivate_user(username)
        except Exception:
            current_app.logger.exception('Failed to roll back OTS leader account: %s', username)
        return jsonify({'error': 'Failed to create leader account'}), 500

    team.leaderUserId = user.id
    db.session.commit()
    return jsonify(_team_to_dict(team)), 201


# ----------------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------------
@api_v1.route('/events', methods=['GET'])
@jwt_required()
@require_role('events_admin', 'events_readonly')
def list_events():
    events = EventModel.query.order_by(EventModel.id.desc()).all()
    return jsonify({'events': [_event_to_dict(e) for e in events]}), 200


@api_v1.route('/events/<int:event_id>', methods=['GET'])
@jwt_required()
@require_role('events_admin', 'events_readonly')
def get_event(event_id):
    event = EventModel.query.get(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    return jsonify(_event_to_dict(event)), 200


@api_v1.route('/events', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def create_event():
    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'error': 'name is required'}), 400

    player_mode = data.get('playerAccessMode', 'individual')
    if player_mode not in PLAYER_ACCESS_MODES:
        return jsonify({'error': 'playerAccessMode must be one of %s' % (PLAYER_ACCESS_MODES,)}), 400
    cleanup = data.get('accountCleanup', 'deactivate')
    if cleanup not in ACCOUNT_CLEANUP_MODES:
        return jsonify({'error': 'accountCleanup must be one of %s' % (ACCOUNT_CLEANUP_MODES,)}), 400

    try:
        event = EventModel(
            name=data['name'],
            description=data.get('description'),
            validFrom=_parse_dt(data.get('validFrom')),
            validUntil=_parse_dt(data.get('validUntil')),
            cutoffTime=data.get('cutoffTime'),
            timezone=data.get('timezone'),
            playerAccessMode=player_mode,
            accountCleanup=cleanup,
            active=data.get('active', True),
        )
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400

    db.session.add(event)
    db.session.commit()
    return jsonify(_event_to_dict(event)), 201


@api_v1.route('/events/<int:event_id>', methods=['PUT'])
@jwt_required()
@require_role('events_admin')
def update_event(event_id):
    event = EventModel.query.get(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    data = request.get_json() or {}

    try:
        if 'name' in data:
            if not data['name']:
                return jsonify({'error': 'name cannot be empty'}), 400
            event.name = data['name']
        if 'description' in data:
            event.description = data['description']
        if 'validFrom' in data:
            event.validFrom = _parse_dt(data['validFrom'])
        if 'validUntil' in data:
            event.validUntil = _parse_dt(data['validUntil'])
        if 'cutoffTime' in data:
            event.cutoffTime = data['cutoffTime']
        if 'timezone' in data:
            event.timezone = data['timezone']
        if 'playerAccessMode' in data:
            if data['playerAccessMode'] not in PLAYER_ACCESS_MODES:
                return jsonify({'error': 'playerAccessMode must be one of %s' % (PLAYER_ACCESS_MODES,)}), 400
            event.playerAccessMode = data['playerAccessMode']
        if 'accountCleanup' in data:
            if data['accountCleanup'] not in ACCOUNT_CLEANUP_MODES:
                return jsonify({'error': 'accountCleanup must be one of %s' % (ACCOUNT_CLEANUP_MODES,)}), 400
            event.accountCleanup = data['accountCleanup']
        if 'active' in data:
            event.active = bool(data['active'])
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400

    db.session.commit()
    return jsonify(_event_to_dict(event)), 200


@api_v1.route('/events/<int:event_id>', methods=['DELETE'])
@jwt_required()
@require_role('events_admin')
def delete_event(event_id):
    event = EventModel.query.get(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    db.session.delete(event)
    db.session.commit()
    return jsonify({'message': 'Event deleted'}), 200


# ----------------------------------------------------------------------------
# Access links (one per event/team/role; only the token hash is stored)
# ----------------------------------------------------------------------------
@api_v1.route('/events/<int:event_id>/links', methods=['GET'])
@jwt_required()
@require_role('events_admin', 'events_readonly')
def list_event_links(event_id):
    if not EventModel.query.get(event_id):
        return jsonify({'error': 'Event not found'}), 404
    links = EventLinkModel.query.filter_by(eventId=event_id).all()
    return jsonify({'links': [_link_to_dict(l) for l in links]}), 200


@api_v1.route('/events/<int:event_id>/links', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def create_event_link(event_id):
    if not EventModel.query.get(event_id):
        return jsonify({'error': 'Event not found'}), 404
    data = request.get_json() or {}

    team_id = data.get('teamId')
    role = (data.get('role') or '').strip().lower()
    if not team_id or role not in LINK_ROLES:
        return jsonify({'error': 'teamId and a valid role (leader/player) are required'}), 400
    if not EventTeamModel.query.get(team_id):
        return jsonify({'error': 'Unknown teamId'}), 400
    if EventLinkModel.query.filter_by(eventId=event_id, eventTeamId=team_id, role=role).first():
        return jsonify({'error': 'A link for this team and role already exists; revoke it to get a new one'}), 409

    raw, token_hash = _generate_link_token()
    link = EventLinkModel(
        eventId=event_id,
        eventTeamId=team_id,
        role=role,
        tokenHash=token_hash,
        tokenVersion=1,
    )
    db.session.add(link)
    db.session.commit()
    return jsonify(_link_to_dict(link, raw_token=raw)), 201


@api_v1.route('/events/links/<int:link_id>/revoke', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def revoke_event_link(link_id):
    """Regenerate the token: the old printed link/QR stops working."""
    link = EventLinkModel.query.get(link_id)
    if not link:
        return jsonify({'error': 'Link not found'}), 404
    raw, token_hash = _generate_link_token()
    link.tokenHash = token_hash
    link.tokenVersion = (link.tokenVersion or 0) + 1
    db.session.commit()
    return jsonify(_link_to_dict(link, raw_token=raw)), 200


@api_v1.route('/events/links/<int:link_id>', methods=['DELETE'])
@jwt_required()
@require_role('events_admin')
def delete_event_link(link_id):
    link = EventLinkModel.query.get(link_id)
    if not link:
        return jsonify({'error': 'Link not found'}), 404
    db.session.delete(link)
    db.session.commit()
    return jsonify({'message': 'Link deleted'}), 200
