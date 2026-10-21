"""
Events module API endpoints.

Admin CRUD for reusable teams and time-windowed events. This is the opt-in
events management surface; the public link flow and session handling live
elsewhere.
"""

from flask import request, jsonify
from flask_jwt_extended import jwt_required
from datetime import datetime

from app.api_v1 import api_v1
from app.models import db, EventTeamModel, EventModel, OTSGroupModel, UserModel
from app.rbac import require_role

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
