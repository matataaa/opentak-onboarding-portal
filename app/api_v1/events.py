"""
Events module API endpoints.

Admin CRUD for reusable teams and time-windowed events. This is the opt-in
events management surface; the public link flow and session handling live
elsewhere.
"""

import re
import json
import time
import base64
import hashlib
import hmac
import secrets
import threading
from collections import deque

from flask import request, jsonify, current_app, g
from flask_jwt_extended import (jwt_required, create_access_token, create_refresh_token,
                                get_jwt, get_jwt_identity, verify_jwt_in_request)
from datetime import datetime, timedelta
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.api_v1 import api_v1
from app.models import db, EventTeamModel, EventModel, EventLinkModel, EventSessionModel, EventLeaderSessionModel, EventTeamLeaderModel, OTSGroupModel, UserModel, OnboardingCodeModel, SystemSettingsModel
from app.ots import otsClient, new_password_error
from app.exceptions import BadRequestError
from app.rbac import require_role

# Access tokens stay short even when the leader session lives (via refresh)
# until the event ends.
LEADER_ACCESS_MAX_SECONDS = 12 * 3600

# One OTS account per device (shared player mode)
# OTS counts every signClient call as a use, so one retry must not brick the account
DEVICE_ENROLLMENT_MAX_USES = 3
DEVICE_ENROLLMENT_TTL_SECONDS = 15 * 60
DEVICE_MAX_PER_TEAM = 100
EVENT_MAX_DEVICE_ACCOUNTS = 500
# Per link per window: a whole team scanning at a briefing must not hit 429;
# the per-team and per-event caps bound the total anyway
DEVICE_THROTTLE_MAX = 30
DEVICE_THROTTLE_WINDOW_SECONDS = 60
# Leader joins per leader link per window (each join writes a leader session row)
LEADER_JOIN_THROTTLE_MAX = 10
DEVICE_USERNAME_ATTEMPTS = 3
DEVICE_COOKIE_NAME = 'event_device_key'

OTS_USERNAME_RE = re.compile(r'^[a-z0-9]{3,32}$')
LINK_ROLES = ('leader', 'player')
LEADER_NAME_MAX = 80
LINK_DIRECTIONS = ('BOTH', 'IN', 'OUT')


def _hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _generate_link_token():
    raw = secrets.token_urlsafe(32)
    return raw, _hash_token(raw)


def _link_secret():
    key = current_app.config.get('SECRET_KEY')
    if not key or str(key) in ('None', 'dev') or len(str(key)) < 16:
        return None
    return str(key).encode('utf-8')


def _key_is_strong():
    return _link_secret() is not None


def _derive_link_token(link):
    """Deterministic token for a link, so an admin can re-show its QR later.

    Only valid while SECRET_KEY is unchanged; the join flow still just looks up
    the sha256 of the presented token.
    """
    msg = b"event-link:v2:%d:%d:%s" % (link.id, link.tokenVersion, link.tokenNonce.encode('ascii'))
    digest = hmac.new(_link_secret(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _link_retrievable(link):
    if not _key_is_strong() or not link.tokenNonce:
        return False
    return hmac.compare_digest(link.tokenHash or '', _hash_token(_derive_link_token(link)))


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
        'direction': link.direction,
        'tokenVersion': link.tokenVersion,
        'retrievable': _link_retrievable(link),
        'onboardingCodeId': link.onboardingCodeId,
        'onboardingCode': {
            'id': link.onboardingCode.id,
            'name': link.onboardingCode.name,
            'onboardingCode': link.onboardingCode.onboardingCode,
        } if link.onboardingCode else None,
        'createdAt': link.createdAt.isoformat() if link.createdAt else None,
    }
    # The raw token is only returned once, when generated or regenerated.
    if raw_token is not None:
        data['token'] = raw_token
        data['url'] = _link_url(raw_token)
    return data

PLAYER_ACCESS_MODES = ('shared', 'individual')
ACCOUNT_CLEANUP_MODES = ('deactivate', 'delete')
# The fixed set of TAK team colours ATAK accepts (the __group/locationTeam value).
TAK_TEAM_COLORS = (
    'White', 'Yellow', 'Orange', 'Magenta', 'Red', 'Maroon', 'Purple',
    'Dark Blue', 'Blue', 'Cyan', 'Teal', 'Green', 'Dark Green', 'Brown',
)


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
            'missingInOts': team.otsGroup.missing_in_ots,
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
        'playerAccessMode': event.playerAccessMode,
        'accountCleanup': event.accountCleanup,
        'active': event.active,
        'organizerTeamId': event.organizerTeamId,
        'organizerDirection': event.organizerDirection,
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

    if data.get('otsGroupId'):
        group = OTSGroupModel.query.get(data['otsGroupId'])
        if not group:
            return jsonify({'error': 'Unknown otsGroupId'}), 400
        if group.missing_in_ots:
            return jsonify({'error': f"Group '{group.name}' was not found in OpenTAK Server"}), 400
    if data.get('leaderUserId') and not UserModel.get_user_by_id(data['leaderUserId']):
        return jsonify({'error': 'Unknown leaderUserId'}), 400
    if data.get('color') and data['color'] not in TAK_TEAM_COLORS:
        return jsonify({'error': 'color must be a TAK team colour'}), 400

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
        if data['color'] and data['color'] not in TAK_TEAM_COLORS:
            return jsonify({'error': 'color must be a TAK team colour'}), 400
        team.color = data['color']
    if 'otsGroupId' in data:
        if data['otsGroupId']:
            group = OTSGroupModel.query.get(data['otsGroupId'])
            if not group:
                return jsonify({'error': 'Unknown otsGroupId'}), 400
            if group.missing_in_ots and group.id != team.otsGroupId:
                return jsonify({'error': f"Group '{group.name}' was not found in OpenTAK Server"}), 400
        elif _reprovision_query(eventTeamId=team.id).count():
            return jsonify({'error': 'Team has open device accounts; it needs an OTS group until they are closed'}), 409
        if (data['otsGroupId'] or None) != team.otsGroupId:
            # Legacy sessions hold the current group (or nothing): record it before it changes
            EventSessionModel.query.filter_by(
                eventTeamId=team.id, mode='device', closedAt=None, otsGroupId=None, direction=None).update(
                {'otsGroupId': team.otsGroupId} if team.otsGroupId else {'direction': 'BOTH'},
                synchronize_session=False)
        team.otsGroupId = data['otsGroupId']
    if 'leaderUserId' in data:
        if data['leaderUserId'] and not UserModel.get_user_by_id(data['leaderUserId']):
            return jsonify({'error': 'Unknown leaderUserId'}), 400
        team.leaderUserId = data['leaderUserId']

    db.session.commit()
    body = _team_to_dict(team)
    if 'otsGroupId' in data:
        # Open device accounts (all events) follow the team's group, and so do the
        # organizer (HQ) devices of the events the team takes part in; submitting
        # the same group again retries the ones that failed before.
        sessions = _reprovision_query(eventTeamId=team.id).all() if team.otsGroupId else []
        for event in _organized_events_of_team(team.id):
            sessions += _organizer_query(event).all()
        unique = {s.id: s for s in sessions}
        body['reprovisioned'], body['reprovisionFailed'] = _reprovision_sessions(
            [unique[i] for i in sorted(unique)])
    return jsonify(body), 200


def _organized_events_of_team(team_id):
    """Events with an organizer (HQ) team in which team_id has a link."""
    return EventModel.query.filter(
        EventModel.organizerTeamId.isnot(None),
        EventModel.id.in_(db.session.query(EventLinkModel.eventId).filter(EventLinkModel.eventTeamId == team_id)),
    ).order_by(EventModel.id).all()


def _reprovision_organizers(events):
    """Re-provision the organizer (HQ) devices of events; returns (reprovisioned, failed)."""
    done, failed = 0, 0
    for event in events:
        d, f = _reprovision_sessions(_organizer_query(event).all())
        done, failed = done + d, failed + f
    return done, failed


def _delete_closed_sessions(**filters):
    """Bulk-delete closed sessions, logging those whose OTS cleanup was given up."""
    base = EventSessionModel.query.filter_by(**filters).filter(EventSessionModel.closedAt.isnot(None))
    for sess in base.filter_by(closeReason='expired (OTS cleanup failed)').all():
        current_app.logger.warning(
            'Deleting event session %s with failed OTS cleanup; check OTS account %s manually',
            sess.id, sess.otsUsername)
    base.delete(synchronize_session=False)


def _detach_sessions_from_links(link_criterion):
    """Null eventLinkId of any remaining session pointing at links about to be bulk-deleted."""
    link_ids = db.session.query(EventLinkModel.id).filter(link_criterion)
    EventSessionModel.query.filter(EventSessionModel.eventLinkId.in_(link_ids)).update(
        {'eventLinkId': None}, synchronize_session=False)


@api_v1.route('/events/teams/<int:team_id>', methods=['DELETE'])
@jwt_required()
@require_role('events_admin')
def delete_event_team(team_id):
    team = EventTeamModel.query.get(team_id)
    if not team:
        return jsonify({'error': 'Team not found'}), 404
    # Open sessions are the cleanup ledger for OTS accounts/profiles: keep them.
    if EventSessionModel.query.filter_by(eventTeamId=team_id, closedAt=None).count():
        return jsonify({'error': 'Team has open sessions; end the event(s) first (set the end time to now or earlier), wait for the session cleanup to run, then delete the team'}), 409
    if EventModel.query.filter_by(organizerTeamId=team_id).count():
        return jsonify({'error': 'Team is the organizer (HQ) team of an event; choose another organizer team for that event first'}), 409
    organized = _organized_events_of_team(team_id)
    # FKs do not cascade: remove children explicitly, in one transaction.
    _delete_closed_sessions(eventTeamId=team_id)
    _detach_sessions_from_links(EventLinkModel.eventTeamId == team_id)
    EventTeamLeaderModel.query.filter_by(eventTeamId=team_id).delete(synchronize_session=False)
    EventLeaderSessionModel.query.filter_by(eventTeamId=team_id).delete(synchronize_session=False)
    EventLinkModel.query.filter_by(eventTeamId=team_id).delete(synchronize_session=False)
    db.session.delete(team)
    db.session.commit()
    body = {'message': 'Team deleted'}
    if organized:
        # The team left those events: their organizer devices lose its group
        body['reprovisioned'], body['reprovisionFailed'] = _reprovision_organizers(organized)
    return jsonify(body), 200


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
    password_error = new_password_error(password)
    if password_error:
        return jsonify({'error': password_error}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    if not OTS_USERNAME_RE.match(username):
        return jsonify({'error': 'Username must be 3-32 characters, letters and numbers only'}), 400
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

    if player_mode == 'shared' and not data.get('validUntil'):
        return jsonify({'error': 'validUntil is required when playerAccessMode is shared'}), 400
    organizer_direction = data.get('organizerDirection', 'OUT')
    if organizer_direction not in LINK_DIRECTIONS:
        return jsonify({'error': 'organizerDirection must be one of %s' % (LINK_DIRECTIONS,)}), 400
    if data.get('organizerTeamId') is not None:
        return jsonify({'error': 'organizerTeamId can only be set once the team has a link in this event'}), 400

    try:
        event = EventModel(
            name=data['name'],
            description=data.get('description'),
            validFrom=_parse_dt(data.get('validFrom')),
            validUntil=_parse_dt(data.get('validUntil')),
            playerAccessMode=player_mode,
            accountCleanup=cleanup,
            active=data.get('active', True),
            organizerDirection=organizer_direction,
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

    # Shared mode needs an end time (device accounts expire with the event). Only
    # checked when mode or end time is being changed, so other edits stay possible.
    if 'playerAccessMode' in data or 'validUntil' in data:
        try:
            new_until = _parse_dt(data['validUntil']) if 'validUntil' in data else event.validUntil
        except ValueError:
            return jsonify({'error': 'Invalid date format'}), 400
        new_mode = data.get('playerAccessMode', event.playerAccessMode)
        if new_mode == 'shared' and new_until is None:
            return jsonify({'error': 'validUntil is required when playerAccessMode is shared'}), 400
    if 'organizerDirection' in data and data['organizerDirection'] not in LINK_DIRECTIONS:
        return jsonify({'error': 'organizerDirection must be one of %s' % (LINK_DIRECTIONS,)}), 400
    organizer_id = data.get('organizerTeamId')
    if organizer_id is not None:
        if isinstance(organizer_id, bool) or not isinstance(organizer_id, int):
            return jsonify({'error': 'organizerTeamId must be an integer or null'}), 400
        if organizer_id not in {t.id for t in _event_team_options(event)}:
            return jsonify({'error': 'organizerTeamId must be a team with a link in this event and a usable OTS group'}), 400

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
            # Keep open sessions in step with the new end time. When it is cleared
            # (None) their expiresAt stays as is: the cleanup job never closes a
            # session without expiresAt, which would orphan the OTS accounts.
            if event.validUntil is not None:
                EventSessionModel.query.filter_by(eventId=event.id, closedAt=None).update(
                    {'expiresAt': event.validUntil}, synchronize_session=False)
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
        if 'organizerTeamId' in data:
            event.organizerTeamId = organizer_id
        if 'organizerDirection' in data:
            event.organizerDirection = data['organizerDirection']
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400

    db.session.commit()
    body = _event_to_dict(event)
    if 'organizerTeamId' in data or 'organizerDirection' in data:
        # Live for the open device accounts of the old and new organizer team;
        # saving the same values again retries the ones that failed before.
        body['reprovisioned'], body['reprovisionFailed'] = _reprovision_organizers([event])
    return jsonify(body), 200


@api_v1.route('/events/<int:event_id>', methods=['DELETE'])
@jwt_required()
@require_role('events_admin')
def delete_event(event_id):
    event = EventModel.query.get(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    # Open sessions are the cleanup ledger for OTS accounts/profiles: keep them.
    if EventSessionModel.query.filter_by(eventId=event_id, closedAt=None).count():
        return jsonify({'error': 'Event has open sessions; end the event first (set its end time to now or earlier), wait for the session cleanup to run, then delete it'}), 409
    # FKs do not cascade: remove children explicitly, in one transaction.
    _delete_closed_sessions(eventId=event_id)
    _detach_sessions_from_links(EventLinkModel.eventId == event_id)
    EventTeamLeaderModel.query.filter_by(eventId=event_id).delete(synchronize_session=False)
    EventLeaderSessionModel.query.filter_by(eventId=event_id).delete(synchronize_session=False)
    EventLinkModel.query.filter_by(eventId=event_id).delete(synchronize_session=False)
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
    event = EventModel.query.get(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    data = request.get_json() or {}

    team_id = data.get('teamId')
    role = (data.get('role') or '').strip().lower()
    if not team_id or role not in LINK_ROLES:
        return jsonify({'error': 'teamId and a valid role (leader/player) are required'}), 400
    if not EventTeamModel.query.get(team_id):
        return jsonify({'error': 'Unknown teamId'}), 400
    if EventLinkModel.query.filter_by(eventId=event_id, eventTeamId=team_id, role=role).first():
        return jsonify({'error': 'A link for this team and role already exists; regenerate it to get a new one'}), 409

    onboarding_code_id = data.get('onboardingCodeId')
    if onboarding_code_id and not OnboardingCodeModel.get_onboarding_code_by_id(onboarding_code_id):
        return jsonify({'error': 'Unknown onboardingCodeId'}), 400
    # Applies to the device accounts of shared mode; stored for leader links too
    direction = data.get('direction', 'BOTH')
    if direction not in LINK_DIRECTIONS:
        return jsonify({'error': 'direction must be one of %s' % (LINK_DIRECTIONS,)}), 400

    # Placeholder hash (unique, random) until the id is known and the token can be derived.
    raw, token_hash = _generate_link_token()
    link = EventLinkModel(
        eventId=event_id,
        eventTeamId=team_id,
        role=role,
        tokenHash=token_hash,
        tokenVersion=1,
        tokenNonce=secrets.token_urlsafe(16),
        onboardingCodeId=onboarding_code_id,
        direction=direction,
    )
    db.session.add(link)
    if _key_is_strong():
        db.session.flush()
        raw = _derive_link_token(link)
        link.tokenHash = _hash_token(raw)
    else:
        current_app.logger.warning('SECRET_KEY is weak or unset: event link QR codes cannot be re-shown later')
    db.session.commit()
    body = _link_to_dict(link, raw_token=raw)
    if event.organizerTeamId:
        # A team new to the event: the organizer (HQ) devices get its group
        body['reprovisioned'], body['reprovisionFailed'] = _reprovision_organizers([event])
    return jsonify(body), 201


@api_v1.route('/events/links/<int:link_id>/revoke', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def revoke_event_link(link_id):
    """Regenerate the token: the old printed link/QR stops working."""
    link = EventLinkModel.query.get(link_id)
    if not link:
        return jsonify({'error': 'Link not found'}), 404
    link.tokenVersion = (link.tokenVersion or 0) + 1
    link.tokenNonce = secrets.token_urlsafe(16)
    if _key_is_strong():
        raw = _derive_link_token(link)
        link.tokenHash = _hash_token(raw)
    else:
        current_app.logger.warning('SECRET_KEY is weak or unset: event link QR codes cannot be re-shown later')
        raw, link.tokenHash = _generate_link_token()
    # The new version already ends them; record it in the leader session ledger
    _revoke_link_leader_sessions(link.id)
    db.session.commit()
    return jsonify(_link_to_dict(link, raw_token=raw)), 200


def _revoke_link_leader_sessions(link_id):
    """Mark the open leader sessions of a link revoked (the caller commits)."""
    EventLeaderSessionModel.query.filter_by(eventLinkId=link_id, revokedAt=None).update(
        {'revokedAt': datetime.now(), 'revokedBy': _actor_id()}, synchronize_session=False)


def _actor_id():
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None


@api_v1.route('/events/links/<int:link_id>/url', methods=['GET'])
@jwt_required()
@require_role('events_admin')
def get_event_link_url(link_id):
    """Re-derive the join URL of a link (only for links created with a strong SECRET_KEY)."""
    link = EventLinkModel.query.get(link_id)
    if not link:
        return jsonify({'error': 'Link not found'}), 404
    if not _link_retrievable(link):
        return jsonify({'error': 'This link cannot be shown again; regenerate it to get a retrievable link'}), 409
    return jsonify({'url': _link_url(_derive_link_token(link))}), 200


@api_v1.route('/events/links/<int:link_id>', methods=['PUT'])
@jwt_required()
@require_role('events_admin')
def update_event_link(link_id):
    """Change the direction of a link; its open device accounts are re-provisioned in OTS.

    The new direction is saved even when some accounts could not be updated
    (counted in reprovisionFailed; saving again retries them); devices pick the
    change up when they reconnect. A leader link's direction applies to the
    leader's own devices (session role 'leader'), which join through it.
    """
    link = EventLinkModel.query.get(link_id)
    if not link:
        return jsonify({'error': 'Link not found'}), 404
    data = request.get_json(silent=True) or {}
    direction = data.get('direction')
    if direction not in LINK_DIRECTIONS:
        return jsonify({'error': 'direction must be one of %s' % (LINK_DIRECTIONS,)}), 400

    if direction != link.direction:
        link.direction = direction
        db.session.commit()
    # Every open account of this link whose granted groups differ, so saving the
    # same direction again retries the ones that failed before.
    body = _link_to_dict(link)
    body['reprovisioned'], body['reprovisionFailed'] = _reprovision_sessions(
        _reprovision_query(eventLinkId=link.id).all())
    return jsonify(body), 200


@api_v1.route('/events/links/<int:link_id>', methods=['DELETE'])
@jwt_required()
@require_role('events_admin')
def delete_event_link(link_id):
    link = EventLinkModel.query.get(link_id)
    if not link:
        return jsonify({'error': 'Link not found'}), 404
    event = link.event
    if (event and event.organizerTeamId == link.eventTeamId and EventLinkModel.query.filter_by(
            eventId=link.eventId, eventTeamId=link.eventTeamId).count() == 1):
        return jsonify({'error': 'This is the last link of the organizer (HQ) team; choose another organizer team for the event first'}), 409
    # Device sessions outlive their link (they are the OTS cleanup ledger)
    EventSessionModel.query.filter_by(eventLinkId=link.id).update(
        {'eventLinkId': None}, synchronize_session=False)
    _revoke_link_leader_sessions(link.id)
    db.session.delete(link)
    db.session.commit()
    body = {'message': 'Link deleted'}
    if event and event.organizerTeamId:
        # If that was the team's last link it left the event: the organizer devices lose its group
        body['reprovisioned'], body['reprovisionFailed'] = _reprovision_organizers([event])
    return jsonify(body), 200


# ----------------------------------------------------------------------------
# Leader sessions: the people who joined through a leader link
# ----------------------------------------------------------------------------
def _leader_session_status(row, event, link):
    """'revoked', 'ended' (what leader_session_error would refuse otherwise) or 'active'.

    The shared account a session was issued for is not stored, so reassigning the
    team's leader to another account is not detected here (only a cleared leader).
    """
    if row.revokedAt is not None:
        return 'revoked'
    if row.userId is not None:
        # A personal leader (own account): active while its designation gives access
        designation = EventTeamLeaderModel.query.filter_by(
            eventId=row.eventId, eventTeamId=row.eventTeamId, userId=row.userId).first()
        return 'active' if designation and _designation_valid(designation, event) else 'ended'
    team = row.team
    if (not event or _event_window_error(event) or not link or link.role != 'leader'
            or link.eventId != row.eventId or link.eventTeamId != row.eventTeamId
            or not team or team.leaderUserId is None):
        return 'ended'
    return 'active'


def _leader_session_to_dict(row, device_count=0, status=None):
    user = UserModel.get_user_by_id(row.userId) if row.userId is not None else None
    return {
        'id': row.id,
        # 'link': someone on the shared leader account; 'account': a personal leader
        'kind': 'link' if row.userId is None else 'account',
        'username': user.username if user else None,
        'eventId': row.eventId,
        'eventTeamId': row.eventTeamId,
        'team': {'id': row.team.id, 'name': row.team.name} if row.team else None,
        'eventLinkId': row.eventLinkId,
        'displayName': row.displayName,
        'createdAt': row.createdAt.isoformat() if row.createdAt else None,
        'revokedAt': row.revokedAt.isoformat() if row.revokedAt else None,
        'revokedBy': row.revokedBy,
        'deviceCount': device_count,
        'status': status or _leader_session_status(
            row, EventModel.query.get(row.eventId),
            EventLinkModel.query.get(row.eventLinkId) if row.eventLinkId else None),
    }


@api_v1.route('/events/<int:event_id>/leader-sessions', methods=['GET'])
@jwt_required()
@require_role('events_admin', 'events_readonly')
def list_event_leader_sessions(event_id):
    event = EventModel.query.get(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    rows = EventLeaderSessionModel.query.filter_by(eventId=event_id).order_by(EventLeaderSessionModel.id).all()
    # Open device accounts each leader session created
    counts = dict(db.session.query(EventSessionModel.leaderSessionId, db.func.count(EventSessionModel.id)).filter(
        EventSessionModel.eventId == event_id, EventSessionModel.leaderSessionId.isnot(None),
        EventSessionModel.closedAt.is_(None)).group_by(EventSessionModel.leaderSessionId).all())
    links = {link.id: link for link in EventLinkModel.query.filter_by(eventId=event_id).all()}
    return jsonify({'leaderSessions': [
        _leader_session_to_dict(r, counts.get(r.id, 0), _leader_session_status(r, event, links.get(r.eventLinkId)))
        for r in rows]}), 200


@api_v1.route('/events/<int:event_id>/leader-sessions/<int:session_id>/revoke', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def revoke_event_leader_session(event_id, session_id):
    """End one leader's session; the link and the devices they created stay as they are."""
    row = EventLeaderSessionModel.query.filter_by(id=session_id, eventId=event_id).first()
    if not row:
        return jsonify({'error': 'Leader session not found'}), 404
    if row.revokedAt is not None:
        return jsonify({'error': 'Leader session is already revoked'}), 409
    if row.userId is not None:
        # A personal leader: revoking removes the designation itself
        _end_designation(row.eventId, row.eventTeamId, row.userId)
    row.revokedAt = datetime.now()
    row.revokedBy = _actor_id()
    db.session.commit()
    current_app.logger.info('Event leader session revoked: actor=%s event=%s%s',
                            get_jwt_identity(), event_id, _leader_log(row))
    count = EventSessionModel.query.filter_by(leaderSessionId=row.id, closedAt=None).count()
    return jsonify(_leader_session_to_dict(row, count)), 200


# ----------------------------------------------------------------------------
# Personal leaders: portal users designated as leader of a team for an event
# ----------------------------------------------------------------------------
def _event_has_team(event_id, team_id):
    """True if the team takes part in the event (has a link in it)."""
    return db.session.query(EventLinkModel.id).filter_by(eventId=event_id, eventTeamId=team_id).first() is not None


def _designation_valid(designation, event=None):
    """True while a personal leader designation gives access to the team's leader page.

    The single rule for personal leaders: events enabled, the event active and
    open, the team taking part in it (a link in the event) and, for source
    'link', the leader link it was bound through still a leader link of that
    event and team with the same version and nonce, so regenerating or deleting
    the link ends it. The user must exist, not be expired and not be a shared
    leader (anchor) account.
    """
    if not SystemSettingsModel.get_setting('events_enabled', False):
        return False
    user = UserModel.get_user_by_id(designation.userId)
    if not user or _user_expired(user) or _is_anchor_account(user.id):
        return False
    if event is None or event.id != designation.eventId:
        event = EventModel.query.get(designation.eventId)
    if not event or _event_window_error(event) or not _event_has_team(designation.eventId, designation.eventTeamId):
        return False
    if designation.source == 'admin':
        return True
    if designation.source != 'link' or designation.eventLinkId is None:
        return False
    link = EventLinkModel.query.get(designation.eventLinkId)
    return bool(link and link.role == 'leader' and link.eventId == designation.eventId
                and link.eventTeamId == designation.eventTeamId
                and link.tokenVersion == designation.linkVersion
                and link.tokenNonce and designation.linkNonce
                and hmac.compare_digest(link.tokenNonce.encode(), designation.linkNonce.encode()))


def _user_expired(user):
    return user.expiryDate is not None and user.expiryDate < datetime.now()


def _impersonator_log():
    """' impersonatedBy=<admin>' when the current token is an impersonation, else ''."""
    try:
        claims = get_jwt()
    except Exception:
        return ''
    if not claims.get('impersonated_by'):
        return ''
    return ' impersonatedBy=%s' % (claims.get('impersonated_by_username') or claims['impersonated_by'])


def _is_anchor_account(user_id):
    """True if the user is the shared leader account of any team."""
    return EventTeamModel.query.filter_by(leaderUserId=user_id).first() is not None


def _ensure_account_leader_session(designation):
    """The open ledger row of a personal leader designation; created if missing (caller commits).

    Its id is stored on the devices the leader creates. displayName is the
    user's callsign, else the username.
    """
    row = EventLeaderSessionModel.query.filter_by(
        eventId=designation.eventId, eventTeamId=designation.eventTeamId, userId=designation.userId,
        revokedAt=None).order_by(EventLeaderSessionModel.id.desc()).first()
    if row is None:
        user = UserModel.get_user_by_id(designation.userId)
        name = ((user.callsign or user.username) if user else None) or 'user %d' % designation.userId
        row = EventLeaderSessionModel(
            eventId=designation.eventId, eventTeamId=designation.eventTeamId, userId=designation.userId,
            eventLinkId=designation.eventLinkId if designation.source == 'link' else None,
            displayName=name[:LEADER_NAME_MAX])
        db.session.add(row)
    return row


def _end_designation(event_id, team_id, user_id, revoked_by=None):
    """Remove a personal leader designation and revoke its open ledger rows (caller commits)."""
    EventTeamLeaderModel.query.filter_by(eventId=event_id, eventTeamId=team_id, userId=user_id).delete(
        synchronize_session=False)
    EventLeaderSessionModel.query.filter_by(
        eventId=event_id, eventTeamId=team_id, userId=user_id, revokedAt=None).update(
        {'revokedAt': datetime.now(), 'revokedBy': revoked_by if revoked_by is not None else _actor_id()},
        synchronize_session=False)


def remove_user_leader_designations(user_id, revoked_by=None):
    """Before a user is deleted: drop their designations, revoke their ledger rows (caller commits).

    The ledger rows themselves stay (no FK on userId) as the audit trail.
    """
    EventTeamLeaderModel.query.filter_by(userId=user_id).delete(synchronize_session=False)
    EventLeaderSessionModel.query.filter_by(userId=user_id, revokedAt=None).update(
        {'revokedAt': datetime.now(), 'revokedBy': revoked_by}, synchronize_session=False)


def leader_designations_for(identity):
    """The valid personal leader designations of a user, for /auth/permissions."""
    try:
        user_id = int(identity)
    except (TypeError, ValueError):
        return []
    if not SystemSettingsModel.get_setting('events_enabled', False):
        return []
    result = []
    for d in EventTeamLeaderModel.query.filter_by(userId=user_id).order_by(EventTeamLeaderModel.id).all():
        if _designation_valid(d):
            result.append({
                'eventId': d.eventId, 'eventName': d.event.name if d.event else None,
                'teamId': d.eventTeamId, 'teamName': d.team.name if d.team else None,
                'hq': bool(d.event and d.event.organizerTeamId == d.eventTeamId),
            })
    return result


def _designation_to_dict(designation, event=None):
    user = designation.user
    return {
        'id': designation.id,
        'eventId': designation.eventId,
        'eventTeamId': designation.eventTeamId,
        'team': {'id': designation.team.id, 'name': designation.team.name} if designation.team else None,
        'userId': designation.userId,
        'user': {'id': user.id, 'username': user.username, 'callsign': user.callsign} if user else None,
        'source': designation.source,
        'eventLinkId': designation.eventLinkId,
        'createdAt': designation.createdAt.isoformat() if designation.createdAt else None,
        'createdBy': designation.createdBy,
        'status': 'active' if _designation_valid(designation, event) else 'ended',
    }


@api_v1.route('/events/<int:event_id>/team-leaders', methods=['GET'])
@jwt_required()
@require_role('events_admin', 'events_readonly')
def list_event_team_leaders(event_id):
    event = EventModel.query.get(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    rows = EventTeamLeaderModel.query.filter_by(eventId=event_id).order_by(EventTeamLeaderModel.id).all()
    return jsonify({'teamLeaders': [_designation_to_dict(d, event) for d in rows]}), 200


TEAM_LEADER_CANDIDATES_MAX = 20


@api_v1.route('/events/<int:event_id>/team-leader-candidates', methods=['GET'])
@jwt_required()
@require_role('events_admin')
def list_event_team_leader_candidates(event_id):
    """Users that can be designated as team leader (search on username/callsign, at most 20).

    Leaves out expired users, shared leader (anchor) accounts and, with teamId,
    users already designated for that team in this event.
    """
    if not EventModel.query.get(event_id):
        return jsonify({'error': 'Event not found'}), 404
    search = (request.args.get('search') or '').strip()
    if len(search) < 2:
        return jsonify({'error': 'search must be at least 2 characters'}), 400
    team_id = request.args.get('teamId', type=int)
    pattern = '%%%s%%' % search
    query = UserModel.query.filter(
        or_(UserModel.username.ilike(pattern), UserModel.callsign.ilike(pattern)),
        or_(UserModel.expiryDate.is_(None), UserModel.expiryDate >= datetime.now()),
        ~UserModel.id.in_(db.session.query(EventTeamModel.leaderUserId).filter(
            EventTeamModel.leaderUserId.isnot(None))),
    )
    if team_id is not None:
        query = query.filter(~UserModel.id.in_(db.session.query(EventTeamLeaderModel.userId).filter_by(
            eventId=event_id, eventTeamId=team_id)))
    users = query.order_by(UserModel.username).limit(TEAM_LEADER_CANDIDATES_MAX).all()
    return jsonify({'users': [{'id': u.id, 'username': u.username, 'callsign': u.callsign} for u in users]}), 200


@api_v1.route('/events/<int:event_id>/team-leaders', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def create_event_team_leader_designation(event_id):
    """Designate an existing portal user as leader of a team for this event."""
    event = EventModel.query.get(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    data = request.get_json(silent=True) or {}
    team_id, user_id = data.get('teamId'), data.get('userId')
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (team_id, user_id)):
        return jsonify({'error': 'teamId and userId are required (integers)'}), 400
    if not EventTeamModel.query.get(team_id) or not _event_has_team(event_id, team_id):
        return jsonify({'error': 'teamId must be a team with a link in this event'}), 400
    user = UserModel.get_user_by_id(user_id)
    if not user:
        return jsonify({'error': 'Unknown userId'}), 400
    if _user_expired(user):
        return jsonify({'error': 'This user account has expired'}), 400
    if _is_anchor_account(user.id):
        return jsonify({'error': 'This is the shared leader account of a team; choose a personal account'}), 400
    if EventTeamLeaderModel.query.filter_by(eventId=event_id, eventTeamId=team_id, userId=user_id).first():
        return jsonify({'error': 'This user is already a leader of this team in this event'}), 409

    designation = EventTeamLeaderModel(eventId=event_id, eventTeamId=team_id, userId=user_id,
                                       source='admin', createdBy=_actor_id())
    try:
        db.session.add(designation)
        db.session.flush()
        _ensure_account_leader_session(designation)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'This user is already a leader of this team in this event'}), 409
    current_app.logger.info('Event team leader designated: actor=%s event=%s team=%s user=%s',
                            get_jwt_identity(), event_id, team_id, user.username)
    return jsonify(_designation_to_dict(designation, event)), 201


@api_v1.route('/events/<int:event_id>/team-leaders/<int:designation_id>', methods=['DELETE'])
@jwt_required()
@require_role('events_admin')
def delete_event_team_leader_designation(event_id, designation_id):
    """Remove a personal leader: their access to the team page ends at once."""
    designation = EventTeamLeaderModel.query.filter_by(id=designation_id, eventId=event_id).first()
    if not designation:
        return jsonify({'error': 'Team leader not found'}), 404
    team_id, user_id = designation.eventTeamId, designation.userId
    _end_designation(event_id, team_id, user_id)
    db.session.commit()
    current_app.logger.info('Event team leader removed: actor=%s event=%s team=%s user=%s',
                            get_jwt_identity(), event_id, team_id, user_id)
    return jsonify({'message': 'Team leader removed'}), 200


# ----------------------------------------------------------------------------
# Public join flow (no prior login; the link token is the credential)
# ----------------------------------------------------------------------------
def _event_window_error(event):
    """Return an error response if the event is not currently open, else None."""
    if not event or not event.active:
        return jsonify({'error': 'This event is not active'}), 403
    now = datetime.now()
    if event.validFrom and now < event.validFrom:
        return jsonify({'error': 'This event has not started yet'}), 403
    if event.validUntil and now > event.validUntil:
        return jsonify({'error': 'This event has ended'}), 403
    return None


@api_v1.route('/events/join/<token>', methods=['POST'])
def join_event(token):
    """Exchange an event link token for access. Public: the token is the credential."""
    if not SystemSettingsModel.get_setting('events_enabled', False):
        return jsonify({'error': 'Events are not enabled'}), 403

    link = EventLinkModel.query.filter_by(tokenHash=_hash_token(token)).first()
    if not link:
        return jsonify({'error': 'Invalid or revoked link'}), 404

    event = link.event
    team = link.team
    window_error = _event_window_error(event)
    if window_error:
        return window_error

    if link.role == 'leader':
        user = _joining_account()
        if user is not None:
            return _bind_leader_account(event, team, link, user)
        return _join_as_leader(event, team, link)

    if link.role == 'player':
        if event.playerAccessMode == 'shared':
            return _join_as_player_shared(event, team, link)
        return _join_as_player_individual(event, team, link)

    return jsonify({'error': 'Unsupported link role'}), 400


def _join_as_player_individual(event, team, link):
    """Individual mode: point the player at the normal registration with the
    event-specific onboarding code on this link (reuses self-service)."""
    code = link.onboardingCode
    if not code:
        return jsonify({'error': 'No registration code is configured for this team'}), 409
    return jsonify({
        'role': 'player',
        'mode': 'individual',
        'team': {'id': team.id, 'name': team.name, 'color': team.color},
        'event': {'id': event.id, 'name': event.name},
        'onboardingCode': code.onboardingCode,
        'redirectPath': '/register/%s' % code.onboardingCode,
    }), 200


def _event_remaining_seconds(event, default_seconds):
    if event.validUntil:
        return (event.validUntil - datetime.now()).total_seconds()
    return default_seconds


def _new_device_username(event):
    alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
    # Deliberately outside the portal username namespace: never a local UserModel.
    return 'evt%s_%s' % (event.id, ''.join(secrets.choice(alphabet) for _ in range(8)))


# Creation attempts (including failed ones) per throttle key within the sliding
# window. In-process only: this relies on the single gunicorn worker (-w 1) the
# portal already requires for APScheduler; with several workers the effective
# limit would be per worker, and it resets on restart.
_device_attempts = {}
_device_attempts_lock = threading.Lock()


def _throttle_hit(key, max_attempts=None):
    """Record an attempt for key; return True if the window is already full."""
    now = time.monotonic()
    with _device_attempts_lock:
        attempts = _device_attempts.setdefault(key, deque())
        while attempts and now - attempts[0] > DEVICE_THROTTLE_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= (DEVICE_THROTTLE_MAX if max_attempts is None else max_attempts):
            return True
        attempts.append(now)
        return False


def _device_limit_error(event, team, link, leader=False):
    """Return a 429 response if a cap or the creation throttle is hit, else None.

    Caps count open device sessions: per team within the event, plus an overall
    per-event safety cap. The throttle counts attempts per link; leader presses
    use their own key so they never eat the player link budget.
    """
    open_sessions = EventSessionModel.query.filter_by(eventId=event.id, mode='device', closedAt=None)
    if (open_sessions.count() >= EVENT_MAX_DEVICE_ACCOUNTS
            or open_sessions.filter_by(eventTeamId=team.id).count() >= DEVICE_MAX_PER_TEAM):
        current_app.logger.warning('Event %s team %s reached the device account cap', event.id, team.id)
        return jsonify({'error': 'Too many requests, try again later'}), 429
    if leader:
        key = ('leader', event.id, team.id)
    elif link is not None:
        key = ('link', link.id)
    else:
        key = ('team', event.id, team.id)
    if _throttle_hit(key):
        current_app.logger.warning('Device account throttle hit for event %s team %s', event.id, team.id)
        return jsonify({'error': 'Too many requests, try again later'}), 429
    return None


def _ots_username_exists(username):
    """True/False if OTS knows the username, None if that could not be determined."""
    try:
        body = otsClient.get_users(username=username)
        results = body.get('response', {}).get('results', []) if isinstance(body, dict) else []
        return any(u.get('username') == username for u in results)
    except Exception:
        current_app.logger.exception('Could not look up OTS user %s', username)
        return None


def _drop_device_row(candidate, candidate_session):
    try:
        db.session.delete(candidate_session)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to drop session of %s', candidate)


def _create_device_account(event, team, link=None, client_key=None, leader=False, role=None,
                           leader_session_id=None):
    """Create one OTS account (plus its session row) for a single device.

    The session row is written first so the DB is always the cleanup ledger for
    the OTS account. Returns (session, None) or (None, error_response). The
    password is random and never leaves this function. role='leader' marks a
    leader's own device (link is then the leader link); leader_session_id records
    which leader session created it.
    """
    if not team.otsGroup or team.otsGroup.missing_in_ots:
        # Without a group the device would share its position on __ANON__
        return None, (jsonify({'error': 'This team has no OpenTAK Server group configured; ask the organiser',
                               'code': 'team_has_no_group'}), 409)
    limit_error = _device_limit_error(event, team, link, leader=leader)
    if limit_error:
        return None, limit_error

    session = None
    last_exc = None
    for _attempt in range(DEVICE_USERNAME_ATTEMPTS):
        candidate = _new_device_username(event)
        if EventSessionModel.query.filter_by(otsUsername=candidate).first():
            continue
        candidate_session = EventSessionModel(
            eventId=event.id,
            eventTeamId=team.id,
            otsUsername=candidate,
            mode='device',
            expiresAt=event.validUntil,
            clientKeyHash=_hash_token(client_key) if client_key else None,
            eventLinkId=link.id if link else None,
            role=role,
            leaderSessionId=leader_session_id,
        )
        try:
            db.session.add(candidate_session)
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Failed to record device session for event %s', event.id)
            return None, (jsonify({'error': 'Could not provision team access'}), 500)
        try:
            otsClient.create_user(candidate, secrets.token_urlsafe(18), roles=['user'])
            session = candidate_session
            break
        except Exception as e:
            last_exc = e
            if isinstance(e, BadRequestError):
                # HTTP 400: OTS did not create the user, so there is nothing to delete
                # (and delete_user must never run: the name may belong to someone else).
                # Only our own ledger row is dropped.
                _drop_device_row(candidate, candidate_session)
                if _ots_username_exists(candidate):
                    continue  # name taken by someone else: retry with a new one
                current_app.logger.error('Failed to create device account for event %s: %s', event.id, e)
                return None, (jsonify({'error': 'Could not provision team access'}), 500)
            # Unknown outcome: the account may or may not exist in OTS
            current_app.logger.error('Failed to create device account for event %s: %s', event.id, e)
            _discard_device_account(candidate, candidate_session)
            return None, (jsonify({'error': 'Could not provision team access'}), 500)
    if not session:
        current_app.logger.error('Failed to create device account for event %s: %s', event.id, last_exc)
        return None, (jsonify({'error': 'Could not provision team access'}), 500)

    username = session.otsUsername
    try:
        otsClient.activate_user(username)
        # Fail closed: a device without its memberships could reach (or be seen by) the wrong groups
        direction = _session_direction(session)
        target, organizer = _desired_memberships(session, team, direction)
        if target is None:
            raise RuntimeError('Team %s has no usable OTS group' % team.id)
        _prepare_memberships(event.id, target)
        if not _apply_group_change(username, set(), target):
            raise RuntimeError('Could not add device account %s to its groups' % username)
        _record_grant(session, team, direction, organizer)
        db.session.commit()
        return session, None
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to finish device account %s, removing it', username)
        _discard_device_account(username, session)
        return None, (jsonify({'error': 'Could not provision team access'}), 500)


def _discard_device_account(username, session=None):
    """Remove a half-provisioned device account.

    The OTS account is deleted first; the session row (the cleanup ledger) is
    only dropped if that worked. Otherwise it stays and the close job
    deactivates the account when the event ends.
    """
    try:
        otsClient.delete_user(username)
    except Exception:
        current_app.logger.exception(
            'Failed to remove OTS account %s; keeping its session for the close job', username)
        return
    if session is None:
        return
    try:
        db.session.delete(session)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to drop session of %s', username)


def _issue_device_enrollment(event, username, reissue=False):
    """Create a short-lived, single-use ATAK enrollment string for a device account.

    OTS keeps one token row per username and never resets its use counter when the
    token is recreated, so a re-issue deletes the old token first.
    Returns (atak_info, None) or (None, error_response).
    """
    remaining = _event_remaining_seconds(event, DEVICE_ENROLLMENT_TTL_SECONDS)
    if remaining <= 0:
        return None, (jsonify({'error': 'This event has ended'}), 403)
    exp = int(time.time() + min(DEVICE_ENROLLMENT_TTL_SECONDS, remaining))

    try:
        if reissue:
            # OTS answers 200 when there is no token to delete, so an exception means the
            # old row may still exist: creating now would not reset its use counter.
            otsClient.delete_atak_qr_string(username)
        atak = otsClient.create_atak_qr_string(
            username=username, exp=exp, max_uses=DEVICE_ENROLLMENT_MAX_USES)
        atak_payload = atak.get('response', atak) if isinstance(atak, dict) else {}
    except Exception:
        current_app.logger.exception('Failed to generate ATAK enrollment for %s', username)
        return None, (jsonify({'error': 'Could not generate TAK enrollment'}), 500)

    return {
        'qrString': atak_payload.get('qr_string') if isinstance(atak_payload, dict) else None,
        'expiresAt': exp,
    }, None


def _join_as_player_shared(event, team, link):
    if not event.validUntil:
        return jsonify({'error': 'This event has no end time configured'}), 409

    cookie_key = request.cookies.get(DEVICE_COOKIE_NAME)
    session = None
    if cookie_key:
        session = EventSessionModel.query.filter_by(
            eventId=event.id, eventLinkId=link.id, mode='device', closedAt=None,
            clientKeyHash=_hash_token(cookie_key)).first()
        if session and not session.otsUsername:
            session = None

    new_key = None
    reissue = session is not None
    if session is None:
        new_key = secrets.token_urlsafe(32)
        session, error = _create_device_account(event, team, link, client_key=new_key)
        if error:
            return error
    username = session.otsUsername

    atak_info, error = _issue_device_enrollment(event, username, reissue=reissue)
    if error:
        if not reissue:
            _discard_device_account(username, session)
        return error

    # iTAK enrollment is best-effort for shared mode (the OTS endpoint returns
    # plain text the client does not yet handle); never fail the join over it.
    itak_payload = None
    try:
        itak = otsClient.get_itak_qr_string(username)
        itak_payload = itak.get('response', itak) if isinstance(itak, dict) else itak
    except Exception:
        current_app.logger.info('iTAK enrollment not available for %s', username)

    response = jsonify({
        'role': 'player',
        'mode': 'shared',
        'team': {'id': team.id, 'name': team.name, 'color': team.color},
        'event': {'id': event.id, 'name': event.name},
        'expiresAt': event.validUntil.isoformat() if event.validUntil else None,
        'atak': atak_info,
        'itak': itak_payload,
    })
    if new_key:
        secure = request.is_secure or request.headers.get('X-Forwarded-Proto', '').lower() == 'https'
        response.set_cookie(
            DEVICE_COOKIE_NAME, new_key,
            max_age=max(1, int(_event_remaining_seconds(event, DEVICE_ENROLLMENT_TTL_SECONDS))),
            path=request.path, httponly=True, samesite='Lax', secure=secure)
    return response, 200


def _joining_account():
    """The portal user of the normal session sending this join, else None (anonymous).

    Not a normal user: no token, a shared leader session (leader claim), a kiosk
    session, or a shared leader (anchor) account itself; those take the anonymous
    flow. An invalid or expired token is answered with 401 (raised here), so the
    SPA refreshes and retries instead of trading its normal session for a shared one.
    """
    verify_jwt_in_request(optional=True)
    claims = get_jwt()
    if not claims or claims.get('event_leader_team') is not None or claims.get('kiosk_session'):
        return None
    try:
        user = UserModel.get_user_by_id(int(get_jwt_identity()))
    except (TypeError, ValueError):
        return None
    if not user or _is_anchor_account(user.id):
        return None
    return user


def _bind_leader_account(event, team, link, user):
    """A logged-in user scanned a leader link: make them a personal leader of the team.

    No tokens are issued; their own session stays. The designation (source
    'link') is bound to the link's current version and nonce; an existing admin
    designation is kept as it is.
    """
    if _user_expired(user):
        return jsonify({'error': 'Your account has expired; ask an organiser to renew it'}), 403
    if get_jwt().get('impersonated_by'):
        return jsonify({'error': 'Not while impersonating a user; designate team leaders in the admin team leaders screen instead'}), 403
    if _throttle_hit(('leader-join', link.id), LEADER_JOIN_THROTTLE_MAX):
        current_app.logger.warning('Leader join throttle hit for link %s', link.id)
        return jsonify({'error': 'Too many leader joins on this link, try again in a minute'}), 429
    if not link.tokenNonce:
        # Legacy link, as in _join_as_leader: give it a nonce to bind to
        link.tokenNonce = secrets.token_urlsafe(16)
    designation = EventTeamLeaderModel.query.filter_by(
        eventId=event.id, eventTeamId=team.id, userId=user.id).first()
    if designation is None:
        designation = EventTeamLeaderModel(eventId=event.id, eventTeamId=team.id, userId=user.id,
                                           source='link', createdBy=user.id)
        db.session.add(designation)
    if designation.source == 'link':
        designation.eventLinkId = link.id
        designation.linkVersion = link.tokenVersion
        designation.linkNonce = link.tokenNonce
    try:
        _ensure_account_leader_session(designation)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Could not register you as team leader, try again'}), 409
    current_app.logger.info('Event leader account bound: team %s event %s user=%s source=%s%s',
                            team.id, event.id, user.username, designation.source, _impersonator_log())
    return jsonify({
        'role': 'leader',
        'mode': 'leader-account',
        'redirect': '/events/my-team?eventId=%d&teamId=%d' % (event.id, team.id),
        'team': {'id': team.id, 'name': team.name, 'color': team.color},
        'event': {'id': event.id, 'name': event.name},
    }), 200


def _join_as_leader(event, team, link):
    leader = team.leaderUser if team else None
    if not leader:
        return jsonify({'error': 'No leader account is configured for this team'}), 409
    # Several people share the leader account: each names themself and gets their
    # own ledger row, so one of them can be revoked without regenerating the link.
    display_name = (request.get_json(silent=True) or {}).get('displayName')
    display_name = display_name.strip() if isinstance(display_name, str) else ''
    if not display_name or len(display_name) > LEADER_NAME_MAX or not display_name.isprintable():
        return jsonify({'error': 'displayName is required (1-%d characters)' % LEADER_NAME_MAX,
                        'code': 'display_name_required'}), 400
    if _throttle_hit(('leader-join', link.id), LEADER_JOIN_THROTTLE_MAX):
        current_app.logger.warning('Leader join throttle hit for link %s', link.id)
        return jsonify({'error': 'Too many leader joins on this link, try again in a minute'}), 429

    if event.validUntil:
        hard_seconds = (event.validUntil - datetime.now()).total_seconds()
    else:
        hard_seconds = LEADER_ACCESS_MAX_SECONDS
    if hard_seconds <= 0:
        return jsonify({'error': 'This event has ended'}), 403

    if not link.tokenNonce:
        # Legacy link: give it a nonce so sessions can be bound to it. tokenHash is
        # untouched, so the join URL keeps working (and stays non-retrievable).
        link.tokenNonce = secrets.token_urlsafe(16)
        db.session.commit()

    leader_session = EventLeaderSessionModel(
        eventId=event.id, eventTeamId=team.id, eventLinkId=link.id, displayName=display_name)
    db.session.add(leader_session)
    db.session.commit()

    until_epoch = int(time.time() + hard_seconds)
    claims = {
        'username': leader.username,
        'event_leader_team': team.id,
        'event_leader_event': event.id,
        'event_leader_until': until_epoch,
        'event_leader_link': link.id,
        'event_leader_link_version': link.tokenVersion,
        'event_leader_link_nonce': link.tokenNonce,
        'event_leader_session': leader_session.id,
    }
    access_token = create_access_token(
        identity=str(leader.id),
        additional_claims=claims,
        expires_delta=timedelta(seconds=min(hard_seconds, LEADER_ACCESS_MAX_SECONDS)),
    )
    refresh_token = create_refresh_token(
        identity=str(leader.id),
        additional_claims=claims,
        expires_delta=timedelta(seconds=hard_seconds),
    )

    current_app.logger.info(
        'Event leader join: team %s (%s) for event %s leaderSession=%s name=%s',
        team.id, leader.username, event.id, leader_session.id, display_name
    )
    return jsonify({
        'role': 'leader',
        'access_token': access_token,
        'refresh_token': refresh_token,
        'expiresAt': event.validUntil.isoformat() if event.validUntil else None,
        'team': {'id': team.id, 'name': team.name, 'color': team.color},
        'event': {'id': event.id, 'name': event.name},
        'user': {'id': leader.id, 'username': leader.username},
        'leaderName': display_name,
    }), 200


# ----------------------------------------------------------------------------
# Leader own-team view (bounded leader session only)
# ----------------------------------------------------------------------------
def leader_session_error(claims, identity):
    """Central revocation check for a leader session (access or refresh token).

    Returns None while the session is valid, else (message, 403). The feature
    switch, the event window, the team's current leader, the leader link the
    session came from and the session's ledger row (event_leader_sessions) are
    re-checked on every call, so deactivation, reassignment, regenerating/deleting
    the leader link and revoking the one session end access immediately. Tokens
    without the event_leader_session claim (issued before the ledger) fail. Used by
    `_leader_scope` and by the leader-scope before_request hook.
    """
    if not SystemSettingsModel.get_setting('events_enabled', False):
        return 'Events are not enabled', 403
    team_id = claims.get('event_leader_team')
    event_id = claims.get('event_leader_event')
    link_id = claims.get('event_leader_link')
    link_version = claims.get('event_leader_link_version')
    link_nonce = claims.get('event_leader_link_nonce')
    session_id = claims.get('event_leader_session')
    if (team_id is None or event_id is None or link_id is None or link_version is None
            or not link_nonce or session_id is None):
        return 'Forbidden', 403

    link = EventLinkModel.query.get(link_id)
    if (not link or link.role != 'leader' or link.eventId != event_id
            or link.eventTeamId != team_id or link.tokenVersion != link_version
            or not link.tokenNonce
            or not hmac.compare_digest(link.tokenNonce.encode(), str(link_nonce).encode())):
        return 'Forbidden', 403

    leader_session = EventLeaderSessionModel.query.get(session_id)
    if (not leader_session or leader_session.revokedAt is not None
            or leader_session.eventId != event_id or leader_session.eventTeamId != team_id
            or leader_session.eventLinkId != link_id):
        return 'Forbidden', 403

    event = EventModel.query.get(event_id)
    team = EventTeamModel.query.get(team_id)
    if not event or not team:
        return 'Forbidden', 403
    if _event_window_error(event):
        return 'This event is not open', 403
    try:
        is_leader = team.leaderUserId is not None and team.leaderUserId == int(identity)
    except (TypeError, ValueError):
        is_leader = False
    if not is_leader:
        return 'Forbidden', 403
    return None


def _leader_scope():
    """Resolve (event, team, None) for a leader, else (None, None, error).

    Two kinds of leader, with the same rules from here on:
    - a shared leader session (token with the leader claims): event and team come
      only from the claims set at join time; validity is decided by
      `leader_session_error`;
    - a normal user session of a personal leader: a valid designation
      (`_designation_valid`); the query parameters eventId and teamId pick one
      when the user has several (400 when ambiguous, 403 when none matches).
    Both leave the leader session ledger row (g.leader_session) and the link the
    leader's own devices join through (g.leader_device_link_id) for the endpoint.
    """
    claims = get_jwt()
    if claims.get('event_leader_team') is None:
        return _personal_leader_scope(claims)
    problem = leader_session_error(claims, get_jwt_identity())
    if problem:
        return None, None, (jsonify({'error': problem[0]}), problem[1])
    g.leader_session = EventLeaderSessionModel.query.get(claims['event_leader_session'])
    g.leader_device_link_id = claims['event_leader_link']
    return (EventModel.query.get(claims['event_leader_event']),
            EventTeamModel.query.get(claims['event_leader_team']), None)


def _personal_leader_scope(claims):
    """_leader_scope for a normal user session (no leader claims): only via a valid designation."""
    forbidden = (jsonify({'error': 'You are not a leader of a team in an open event'}), 403)
    if claims.get('kiosk_session'):
        return None, None, forbidden
    try:
        user_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None, None, forbidden
    wanted = {}
    for key in ('eventId', 'teamId'):
        raw = request.args.get(key)
        if raw in (None, ''):
            continue
        try:
            wanted[key] = int(raw)
        except ValueError:
            return None, None, (jsonify({'error': '%s must be an integer' % key}), 400)
    matches = [d for d in EventTeamLeaderModel.query.filter_by(userId=user_id).order_by(EventTeamLeaderModel.id).all()
               if wanted.get('eventId', d.eventId) == d.eventId and wanted.get('teamId', d.eventTeamId) == d.eventTeamId
               and _designation_valid(d)]
    if not matches:
        return None, None, forbidden
    if len(matches) > 1:
        return None, None, (jsonify({'error': 'You lead several teams; pass eventId and teamId',
                                     'code': 'leader_team_required'}), 400)
    designation = matches[0]
    g.leader_session = _ensure_account_leader_session(designation)
    db.session.commit()
    g.leader_device_link_id = _leader_device_link_id(designation)
    return designation.event, designation.team, None


def _leader_device_link_id(designation):
    """The link a personal leader's own devices join through: the team's leader link in
    the event, else its player link (see _session_direction for the direction)."""
    for role in ('leader', 'player'):
        link = EventLinkModel.query.filter_by(
            eventId=designation.eventId, eventTeamId=designation.eventTeamId, role=role).first()
        if link:
            return link.id
    return None


def _current_leader_session():
    """The ledger row of the current leader (only after _leader_scope passed)."""
    return g.leader_session


def _leader_log(leader_session):
    return ' leaderSession=%s name=%s%s' % (leader_session.id, leader_session.displayName, _impersonator_log())


@api_v1.route('/events/my-team', methods=['GET'])
@jwt_required()
def get_my_team():
    event, team, error = _leader_scope()
    if error:
        return error

    link = EventLinkModel.query.filter_by(
        eventId=event.id, eventTeamId=team.id, role='player').first()
    player_link = None
    reason = None
    if not link:
        reason = 'missing'
    elif not _link_retrievable(link):
        reason = 'notRetrievable'
    else:
        player_link = {'url': _link_url(_derive_link_token(link))}

    data = {
        'event': {
            'id': event.id,
            'name': event.name,
            'validUntil': event.validUntil.isoformat() if event.validUntil else None,
        },
        'team': {'id': team.id, 'name': team.name, 'color': team.color},
        'mode': event.playerAccessMode,
        'expiresAt': event.validUntil.isoformat() if event.validUntil else None,
        'playerLink': player_link,
        'atakAvailable': event.playerAccessMode == 'shared',
        'leaderName': _current_leader_session().displayName,
    }
    if reason:
        data['reason'] = reason
    return jsonify(data), 200


@api_v1.route('/events/my-team/enrollment', methods=['POST'])
@jwt_required()
def create_my_team_enrollment():
    event, team, error = _leader_scope()
    if error:
        return error
    if event.playerAccessMode != 'shared':
        return jsonify({'error': 'ATAK enrollment is only available in shared mode'}), 409

    if not event.validUntil:
        return jsonify({'error': 'This event has no end time configured'}), 409

    data = request.get_json(silent=True) or {}
    if data.get('forLeader') is True:
        # The leader's own device: joins through (and takes the direction of) the leader link
        link = EventLinkModel.query.get(g.leader_device_link_id) if g.leader_device_link_id else None
        role = 'leader'
    else:
        link = EventLinkModel.query.filter_by(
            eventId=event.id, eventTeamId=team.id, role='player').first()
        role = None
    leader_session = _current_leader_session()
    session, error = _create_device_account(event, team, link, leader=True, role=role,
                                            leader_session_id=leader_session.id)
    if error:
        return error
    atak_info, error = _issue_device_enrollment(event, session.otsUsername)
    if error:
        _discard_device_account(session.otsUsername, session)
        return error
    current_app.logger.info('Leader enrollment issued: team %s event %s account=%s%s',
                            team.id, event.id, session.otsUsername, _leader_log(leader_session))
    return jsonify({'atak': atak_info}), 200


def record_event_registration(username, onboarding_code):
    """Track an individual-mode player registration so the team colour can be applied.

    Called after assign_ots_groups in the registration flows; the caller commits.
    Players may register before the event starts. Never raises into the
    registration flow.
    """
    try:
        now = datetime.now()
        with db.session.no_autoflush:
            links = EventLinkModel.query.filter_by(
                onboardingCodeId=onboarding_code.id, role='player').all()
            # A code may sit on several links; use one whose event is still usable.
            for link in links:
                event = link.event
                if (event and event.active and event.playerAccessMode == 'individual'
                        and (not event.validUntil or now <= event.validUntil)):
                    db.session.add(EventSessionModel(
                        eventId=link.eventId, eventTeamId=link.eventTeamId, otsUsername=username,
                        mode='individual', expiresAt=event.validUntil))
                    return
    except Exception:
        current_app.logger.exception('Failed to record event registration for %s', username)


# ----------------------------------------------------------------------------
# Admin: device accounts of an event (list, move between teams, park, close)
# ----------------------------------------------------------------------------
# Per-event OTS groups for parked devices. OTS publishes a user's CoT to
# `<group>.OUT` for each IN membership (and to __ANON__.OUT when the user has no
# IN membership at all), and a device only receives from groups where it has OUT.
# So a parked device holds OUT on `evt<E>_parked` (receive-only: nobody writes
# there) and IN on `evt<E>_parked_in` (nobody has OUT there, so what the device
# sends reaches no one). Names matching EVENT_GROUP_RE are managed by the portal
# and skipped by the OTS group sync, so they have no local OTSGroupModel row.
EVENT_GROUP_RE = re.compile(r'^evt\d+_parked(_in)?$')


def _parked_group_name(event_id):
    return 'evt%d_parked' % event_id


def _parked_in_group_name(event_id):
    return 'evt%d_parked_in' % event_id


def _parked_memberships(event_id):
    return {(_parked_group_name(event_id), 'OUT'), (_parked_in_group_name(event_id), 'IN')}


def _session_direction(session):
    """The desired direction of a device session: that of the link it joined through
    (a leader's own device joins through the team's leader link; BOTH when it had to
    join through the player link); without a link the granted direction (BOTH for
    legacy sessions)."""
    link = EventLinkModel.query.get(session.eventLinkId) if session.eventLinkId else None
    if link and link.direction in LINK_DIRECTIONS:
        if session.role == 'leader' and link.role != 'leader':
            # A personal leader's own device joins through the player link when the
            # team has no leader link in the event: never the player direction
            return 'BOTH'
        return link.direction
    # Link deleted: keep what the device was granted
    return session.direction if session.direction in LINK_DIRECTIONS else 'BOTH'


def session_group_memberships(session):
    """The (group name, direction) pairs a device session was granted in OTS.

    Computed from the granted state stored on the session (parked, otsGroupId,
    direction, organizerGrant), never from the current link, team or event
    config, so it is the source of every change and of the close job. Both NULL
    = legacy: BOTH and the team's current group; a direction without a group =
    granted nothing. missing_in_ots is ignored on purpose: a stale flag must not
    skip a removal, and removing from a vanished group is harmless.
    """
    organizer = _granted_organizer(session)
    if session.parkedAt:
        return _parked_memberships(session.eventId) | organizer
    if session.otsGroupId:
        group = OTSGroupModel.query.get(session.otsGroupId)
    elif session.direction is None:
        group = session.team.otsGroup if session.team else None
    else:
        return organizer
    if not group:
        return organizer
    return _group_memberships(group, session.eventId, session.direction or 'BOTH', organizer)


def _granted_organizer(session):
    """The organizer (HQ) memberships stored on a session as granted.

    Stored as group names, not ids (unlike otsGroupId): the OTS membership API
    is keyed by name, group names never change in the portal, and a deleted
    local group row must not hide a removal. Unreadable data is logged and
    treated as empty, so the close job can never get stuck on it.
    """
    if not session.organizerGrant:
        return set()
    try:
        return {(str(name), str(side)) for name, side in json.loads(session.organizerGrant)}
    except (ValueError, TypeError):
        current_app.logger.error('Unreadable organizerGrant on event session %s; check OTS account %s manually',
                                 session.id, session.otsUsername)
        return set()


def _organizer_grant_json(pairs):
    return json.dumps(sorted([name, side] for name, side in pairs)) if pairs else None


def _direction_sides(direction):
    return ('IN', 'OUT') if direction == 'BOTH' else (direction,)


def _organizer_memberships(event, team):
    """What a device of team gets as the event's organizer (HQ): the organizerDirection
    on the group of every other team of the event. Empty unless team is the organizer."""
    if not event or not team or event.organizerTeamId != team.id:
        return set()
    return {(t.otsGroup.name, side) for t in _event_team_options(event) if t.id != team.id
            for side in _direction_sides(event.organizerDirection)}


def _desired_memberships(session, team, direction):
    """(target memberships, organizer part) of a non-parked device session in team.

    (None, None) if the team has no usable group.
    """
    group = team.otsGroup if team else None
    if not group or group.missing_in_ots:
        return None, None
    organizer = _organizer_memberships(session.event, team)
    return _group_memberships(group, session.eventId, direction, organizer), organizer


def _record_grant(session, team, direction, organizer):
    """Store on the session what it was just granted in team (see session_group_memberships)."""
    session.otsGroupId, session.direction = team.otsGroupId, direction
    session.organizerGrant = _organizer_grant_json(organizer)


def _group_memberships(group, event_id, direction='BOTH', organizer=frozenset()):
    """Memberships of a device in a team group for a link direction, plus its organizer ones.

    A device always holds both an IN and an OUT membership: a side it has
    nowhere else goes to a parked sink group (nobody has IN on evt<E>_parked,
    nobody has OUT on evt<E>_parked_in), so it never falls back to __ANON__.
    """
    pairs = {(group.name, side) for side in _direction_sides(direction)} | set(organizer)
    if not any(side == 'IN' for _, side in pairs):
        pairs.add((_parked_in_group_name(event_id), 'IN'))
    if not any(side == 'OUT' for _, side in pairs):
        pairs.add((_parked_group_name(event_id), 'OUT'))
    return pairs


def _prepare_memberships(event_id, memberships):
    """Create the parked groups in OTS when the memberships use them (raises on failure).

    Returns True if they were ensured.
    """
    sinks = (_parked_group_name(event_id), _parked_in_group_name(event_id))
    if not any(name in sinks for name, _ in memberships):
        return False
    _ensure_parked_groups(event_id)
    return True


def _ensure_group(name, description):
    try:
        otsClient.create_group(name, description)
        current_app.logger.info("Created parked group '%s' in OTS", name)
    except Exception:
        # Most likely it exists already; anything else fails the lookup too
        body = otsClient.get_groups(name=name)
        results = body.get('response', {}).get('results', []) if isinstance(body, dict) else []
        if not any(isinstance(g, dict) and g.get('name') == name for g in results):
            raise


def _ensure_parked_groups(event_id):
    """Create the event's two parked groups in OTS if missing (raises on failure)."""
    _ensure_group(_parked_group_name(event_id),
                  'Parked devices of event %d (managed by the portal)' % event_id)
    _ensure_group(_parked_in_group_name(event_id),
                  'Parked devices of event %d, send side (managed by the portal)' % event_id)
    return _parked_memberships(event_id)


def cleanup_parked_group(event_id):
    """Delete the event's parked OTS groups once no open device session is left (best-effort)."""
    if EventSessionModel.query.filter_by(eventId=event_id, mode='device', closedAt=None).count():
        return False
    from app.api_v1.groups import _ots_status_code
    deleted = False
    for name in (_parked_group_name(event_id), _parked_in_group_name(event_id)):
        try:
            otsClient.delete_group(name)
            current_app.logger.info("Deleted parked group '%s' from OTS", name)
            deleted = True
        except Exception as e:
            if _ots_status_code(e) == 404:
                continue  # never created, or already gone
            current_app.logger.warning("Could not delete parked group '%s' from OTS: %s", name, e)
    return deleted


def _apply_group_change(username, source, target):
    """Move an account from the source to the target memberships in OTS.

    Adds first, then removes, so the device always keeps an IN and an OUT
    membership (without IN, OTS would publish its CoT to __ANON__). If any call
    fails, every step that may have taken effect (including the one that raised:
    OTS can change state and still return an error) is reverted best-effort and
    False is returned. Calls are idempotent. Reverts re-add removed memberships
    first; added ones are only removed once all re-adds worked, so a failed
    revert leaves the device in both groups rather than in none. A remove that
    answers 404 (the group no longer exists in OTS) counts as done.
    """
    pending = []
    try:
        for pair in sorted(target - source):
            pending.append(('add', pair))
            otsClient.add_user_to_group(username, pair[0], pair[1])
        for pair in sorted(source - target):
            pending.append(('remove', pair))
            try:
                otsClient.remove_user_from_group(username, pair[0], pair[1])
            except Exception as e:
                from app.api_v1.groups import _ots_status_code
                if _ots_status_code(e) != 404:
                    raise
                pending.pop()  # group is gone, so is the membership: nothing to revert
        return True
    except Exception:
        current_app.logger.exception('Group change for %s failed, reverting', username)
    readded = True
    for action, pair in pending:
        if action == 'remove':
            try:
                otsClient.add_user_to_group(username, pair[0], pair[1])
            except Exception:
                readded = False
                current_app.logger.exception(
                    'Could not restore %s of %s in group %s', pair[1], username, pair[0])
    if not readded:
        current_app.logger.error(
            'Not removing the added groups of %s: restoring the old ones failed', username)
        return False
    for action, pair in pending:
        if action == 'add':
            try:
                otsClient.remove_user_from_group(username, pair[0], pair[1])
            except Exception:
                current_app.logger.exception(
                    'Could not revert add of %s in group %s (%s)', username, pair[0], pair[1])
    return False


def _reprovision_query(**filters):
    """Open, unparked, unexpired device sessions (expired ones belong to the close job)."""
    return EventSessionModel.query.filter_by(mode='device', closedAt=None, parkedAt=None, **filters).filter(
        or_(EventSessionModel.expiresAt.is_(None), EventSessionModel.expiresAt >= datetime.now())
    ).order_by(EventSessionModel.id)


def _organizer_query(event):
    """Open device sessions of event that are organizer (HQ) devices or still hold organizer grants."""
    criterion = EventSessionModel.organizerGrant.isnot(None)
    if event.organizerTeamId:
        criterion = or_(criterion, EventSessionModel.eventTeamId == event.organizerTeamId)
    return _reprovision_query(eventId=event.id).filter(criterion)


def _reprovision_sessions(sessions):
    """Bring device sessions from their granted to their desired groups in OTS, best effort.

    Desired = the session's link direction in its team's current group, plus
    the organizer (HQ) memberships when its team is the event's organizer.
    Returns (reprovisioned, failed); sessions already in place are not counted,
    so calling this again retries only the failed ones. A failed session keeps
    its old memberships and granted state (see _apply_group_change).
    """
    done, failed = 0, 0
    prepared = set()
    for session in sessions:
        username = session.otsUsername
        direction = _session_direction(session)
        source = session_group_memberships(session)
        target, organizer = _desired_memberships(session, session.team, direction)
        if target == source:
            grant = (session.team.otsGroupId, direction, _organizer_grant_json(organizer))
            if (session.otsGroupId, session.direction, session.organizerGrant) != grant:
                _record_grant(session, session.team, direction, organizer)
                db.session.commit()
            continue
        if target is not None:
            try:
                if session.eventId not in prepared and _prepare_memberships(session.eventId, target):
                    prepared.add(session.eventId)
                if _apply_group_change(username, source, target):
                    try:
                        _record_grant(session, session.team, direction, organizer)
                        db.session.commit()
                        done += 1
                        continue
                    except Exception:
                        db.session.rollback()
                        current_app.logger.exception('Could not save the new groups of %s, reverting', username)
                        _apply_group_change(username, target, source)
            except Exception:
                db.session.rollback()
                current_app.logger.exception('Could not re-provision the OTS groups of %s', username)
        failed += 1
        current_app.logger.error('Re-provisioning the OTS groups of %s failed; it keeps its old groups', username)
    return done, failed


def reconcile_device_sessions():
    """Re-provision open device sessions whose granted groups differ from the desired ones.

    Run by the periodic event job, so a failed live change (e.g. a former HQ
    device whose removals failed) heals without an admin. Sessions already in
    place, or without a usable team group, are skipped: no OTS calls in the
    normal case. Idempotent, like saving again in the API. Returns (done, failed).
    """
    pending = []
    for session in _reprovision_query().all():
        target, _organizer = _desired_memberships(session, session.team, _session_direction(session))
        if target is not None and target != session_group_memberships(session):
            pending.append(session)
    return _reprovision_sessions(pending)


def _read_eud_index(usernames):
    """{username: [eud, ...]} from ONE EUD listing; None if OTS could not be read."""
    try:
        results = otsClient.get_all_pages(otsClient.get_euds, sort_by='id').get('response', {}).get('results', [])
    except Exception:
        current_app.logger.exception('Could not read EUDs from OTS')
        return None
    index = {}
    for eud in results:
        name = eud.get('username')
        if name in usernames:
            index.setdefault(name, []).append({
                'callsign': eud.get('callsign'),
                'uid': eud.get('uid'),
                'lastSeen': eud.get('last_event_time'),
            })
    return index


def _leader_names(sessions):
    """{leader session id: displayName} for the leader sessions that created these devices."""
    ids = {s.leaderSessionId for s in sessions if s.leaderSessionId}
    if not ids:
        return {}
    return {row.id: row.displayName for row in
            EventLeaderSessionModel.query.filter(EventLeaderSessionModel.id.in_(ids)).all()}


def _account_to_dict(session, euds=None, leader_names=None):
    if leader_names is None:
        leader_names = _leader_names([session])
    data = {
        'id': session.id,
        'otsUsername': session.otsUsername,
        'legacy': session.mode != 'device',
        'team': {'id': session.team.id, 'name': session.team.name, 'color': session.team.color}
        if session.team else None,
        'parked': session.parkedAt is not None,
        'createdAt': session.createdAt.isoformat() if session.createdAt else None,
        'expiresAt': session.expiresAt.isoformat() if session.expiresAt else None,
        'eventLinkId': session.eventLinkId,
        'role': session.role,
        # Who (leader session display name) created the device; None if not a leader
        'leaderName': leader_names.get(session.leaderSessionId),
    }
    if euds is not None:
        data['euds'] = euds
    return data


@api_v1.route('/events/<int:event_id>/accounts', methods=['GET'])
@jwt_required()
@require_role('events_admin', 'events_readonly')
def list_event_accounts(event_id):
    if not EventModel.query.get(event_id):
        return jsonify({'error': 'Event not found'}), 404
    sessions = EventSessionModel.query.filter(
        EventSessionModel.eventId == event_id,
        EventSessionModel.mode.in_(['device', 'shared']),
        EventSessionModel.closedAt.is_(None),
    ).order_by(EventSessionModel.id).all()
    index = _read_eud_index({s.otsUsername for s in sessions if s.otsUsername}) if sessions else {}
    names = _leader_names(sessions)
    return jsonify({
        'accounts': [_account_to_dict(s, (index or {}).get(s.otsUsername, []), names) for s in sessions],
        'eudsAvailable': index is not None,
    }), 200


def _open_device_session(event_id, session_id):
    """The open device session of this event, or an error response."""
    session = EventSessionModel.query.filter_by(id=session_id, eventId=event_id).first()
    if not session:
        return None, (jsonify({'error': 'Account not found'}), 404)
    if session.mode != 'device':
        return None, (jsonify({'error': 'Legacy shared accounts cannot be managed here'}), 409)
    if session.closedAt:
        return None, (jsonify({'error': 'Account is already closed'}), 409)
    return session, None


def _perform_account_move(session, target_team, log_extra=''):
    """Move an open device session to target_team (None = park) in OTS and the DB.

    Shared by the admin and leader endpoints; the caller has already authorised
    the move and validated the target. Returns a Flask response.
    """
    was_parked = session.parkedAt is not None
    if (target_team is None and was_parked) or (
            target_team is not None and not was_parked and target_team.id == session.eventTeamId):
        return jsonify({'account': _account_to_dict(session)}), 200
    if session.expiresAt and session.expiresAt < datetime.now():
        return jsonify({'error': 'This account has expired'}), 409

    username = session.otsUsername
    source = session_group_memberships(session)
    direction = _session_direction(session)
    organizer = None
    if target_team is None:
        target = _parked_memberships(session.eventId)
    else:
        target, organizer = _desired_memberships(session, target_team, direction)
        if target is None:
            return jsonify({'error': 'This team has no usable OTS group'}), 400
    try:
        _prepare_memberships(session.eventId, target)
    except Exception:
        current_app.logger.exception('Could not prepare parked group for event %s', session.eventId)
        return jsonify({'error': 'Could not create the parked groups in OpenTAK Server'}), 502

    from_name = 'parked' if was_parked else (session.team.name if session.team else None)
    if not _apply_group_change(username, source, target):
        return jsonify({'error': 'OpenTAK Server group update failed; nothing was changed'}), 502

    try:
        if target_team is None:
            session.parkedAt = datetime.now()
            session.organizerGrant = None  # parked devices hold the parked groups only
        else:
            session.parkedAt = None
            session.eventTeamId = target_team.id
            _record_grant(session, target_team, direction, organizer)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to save move of %s, reverting OTS groups', username)
        _apply_group_change(username, target, source)
        return jsonify({'error': 'Could not save the change'}), 500

    current_app.logger.info(
        'Event account move: actor=%s%s event=%s account=%s from=%s to=%s',
        get_jwt_identity(), log_extra, session.eventId, username, from_name,
        'parked' if target_team is None else target_team.name)
    return jsonify({'account': _account_to_dict(session)}), 200


@api_v1.route('/events/<int:event_id>/accounts/<int:session_id>/move', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def move_event_account(event_id, session_id):
    """Move a device account to another team's OTS group, or park it (teamId null).

    The device only picks the change up when it reconnects.
    """
    session, error = _open_device_session(event_id, session_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if 'teamId' not in data:
        return jsonify({'error': 'teamId is required (a team id, or null to park)'}), 400
    team_id = data['teamId']
    if team_id is not None and (isinstance(team_id, bool) or not isinstance(team_id, int)):
        return jsonify({'error': 'teamId must be an integer or null'}), 400

    target_team = None
    if team_id is not None:
        target_team = EventTeamModel.query.get(team_id)
        if not target_team:
            return jsonify({'error': 'Unknown teamId'}), 400
        if not target_team.otsGroup or target_team.otsGroup.missing_in_ots:
            return jsonify({'error': 'This team has no usable OTS group'}), 400

    return _perform_account_move(session, target_team)


CLOSE_BATCH_SIZE = 25  # sessions closed per close-by-link call (each costs several OTS calls)


def _close_device_sessions(sessions, reason='closed by admin'):
    """Close open device sessions right away (admin action). Returns (closed, failed, pending).

    A session is only marked closed once its OTS account is deactivated/deleted
    AND colour and group cleanup worked. failed: the account is still usable.
    pending: the account is disabled but cleanup is incomplete; the session stays
    open and its expiry is moved to now so the close job retries it.
    """
    from app.jobs import cleanup_event_session
    from app.services.event_team_colors import read_profiles
    existing = None
    if sessions:
        try:
            existing = read_profiles()
        except Exception:
            current_app.logger.exception('Could not read OTS profiles for account close')
    closed, failed, pending = 0, 0, 0
    event_ids = set()
    for session in sessions:
        try:
            event_id, username = session.eventId, session.otsUsername
            account_ok, colour_ok, groups_ok = cleanup_event_session(session, existing)
            if not account_ok:
                failed += 1
                current_app.logger.error('Admin close of %s failed: OTS account still usable', username)
                continue
            if not (colour_ok and groups_ok):
                pending += 1
                now = datetime.now()
                if not session.expiresAt or session.expiresAt > now:
                    session.expiresAt = now
                db.session.commit()
                current_app.logger.warning(
                    'Admin close of %s: account disabled, OTS cleanup incomplete; the close job retries',
                    username)
                continue
            session.closeReason = reason
            session.closedAt = datetime.now()
            db.session.commit()
            closed += 1
            event_ids.add(event_id)
            current_app.logger.info('Event account closed: actor=%s event=%s account=%s',
                                    get_jwt_identity(), event_id, username)
        except Exception:
            db.session.rollback()
            failed += 1
            current_app.logger.exception('Admin close of session %s failed', session.id)
    for event_id in event_ids:
        cleanup_parked_group(event_id)
    return closed, failed, pending


@api_v1.route('/events/<int:event_id>/accounts/<int:session_id>/close', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def close_event_account(event_id, session_id):
    session, error = _open_device_session(event_id, session_id)
    if error:
        return error
    closed, _failed, pending = _close_device_sessions([session])
    if pending:
        return jsonify({
            'error': 'The account is disabled but its OpenTAK Server cleanup is incomplete; it stays open and is retried automatically',
            'accountDisabled': True, 'cleanupPending': True}), 502
    if not closed:
        return jsonify({'error': 'Could not close the account in OpenTAK Server; it is still open'}), 502
    return jsonify({'message': 'Account closed', 'account': _account_to_dict(session)}), 200


@api_v1.route('/events/links/<int:link_id>/close-sessions', methods=['POST'])
@jwt_required()
@require_role('events_admin')
def close_event_link_sessions(link_id):
    """Close the open device accounts created through this link, at most CLOSE_BATCH_SIZE per call.

    `remaining` is the number of this link's accounts still open afterwards; call again while > 0.
    """
    if not EventLinkModel.query.get(link_id):
        return jsonify({'error': 'Link not found'}), 404
    query = EventSessionModel.query.filter_by(
        eventLinkId=link_id, mode='device', closedAt=None).order_by(EventSessionModel.id)
    closed, failed, pending = _close_device_sessions(query.limit(CLOSE_BATCH_SIZE).all())
    body = {'closed': closed, 'failed': failed + pending, 'remaining': query.count()}
    if pending:
        body['cleanupPending'] = pending
    if failed or pending:
        body['error'] = 'Some accounts could not be fully closed in OpenTAK Server and are still open'
        return jsonify(body), 502
    return jsonify(body), 200


# ----------------------------------------------------------------------------
# Leader: all device accounts of the event; pull into / take out of the own team
# ----------------------------------------------------------------------------
def _event_team_options(event):
    """Teams of this event (those with a link) that have a usable OTS group."""
    ids = {row[0] for row in db.session.query(EventLinkModel.eventTeamId).filter(
        EventLinkModel.eventId == event.id, EventLinkModel.eventTeamId.isnot(None)).all()}
    teams = EventTeamModel.query.filter(EventTeamModel.id.in_(ids)).order_by(EventTeamModel.name).all() if ids else []
    return [t for t in teams if t.otsGroup and not t.otsGroup.missing_in_ots]


@api_v1.route('/events/my-team/accounts', methods=['GET'])
@jwt_required()
def list_my_team_accounts():
    """All open accounts of the leader's event (every team, parked, legacy shared read-only)."""
    event, team, error = _leader_scope()
    if error:
        return error
    sessions = EventSessionModel.query.filter(
        EventSessionModel.eventId == event.id,
        EventSessionModel.mode.in_(['device', 'shared']),
        EventSessionModel.closedAt.is_(None),
    ).order_by(EventSessionModel.id).all()
    index = _read_eud_index({s.otsUsername for s in sessions if s.otsUsername}) if sessions else {}
    names = _leader_names(sessions)
    accounts = []
    for s in sessions:
        row = _account_to_dict(s, (index or {}).get(s.otsUsername, []), names)
        row.pop('eventLinkId', None)
        row['inMyTeam'] = (not row['legacy'] and not row['parked'] and s.eventTeamId == team.id)
        if not row['inMyTeam'] and not row['parked']:
            # other teams' active accounts: callsign/team/status only
            row['euds'] = [{'callsign': e.get('callsign')} for e in row.get('euds', [])]
        if not row['inMyTeam']:
            row['leaderName'] = None  # who enrolled a device is only shown for the own team
        accounts.append(row)
    return jsonify({
        'accounts': accounts,
        'eudsAvailable': index is not None,
        'myTeamId': team.id,
        # Move targets: never the organizer (HQ) team, whose devices see every team
        'teams': [{'id': t.id, 'name': t.name, 'color': t.color} for t in _event_team_options(event)
                  if t.id != event.organizerTeamId],
    }), 200


@api_v1.route('/events/my-team/accounts/<int:session_id>/move', methods=['POST'])
@jwt_required()
def move_my_team_account(session_id):
    """Leader: take an own-team account out (park, or to another team) or pull a parked one in.

    Allowed is derived from the session's current state and the leader's claimed
    team only: own -> park, own -> another team, parked -> own team. Accounts that
    are active in another team cannot be touched.
    """
    event, team, error = _leader_scope()
    if error:
        return error
    session, error = _open_device_session(event.id, session_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if 'teamId' not in data:
        return jsonify({'error': 'teamId is required (a team id, or null to park)'}), 400
    team_id = data['teamId']
    if team_id is not None and (isinstance(team_id, bool) or not isinstance(team_id, int)):
        return jsonify({'error': 'teamId must be an integer or null'}), 400

    in_my_team = session.parkedAt is None and session.eventTeamId == team.id
    pulling = session.parkedAt is not None and team_id == team.id
    if not (in_my_team or pulling):
        if team_id == team.id:
            return jsonify({'error': 'Only parked accounts can be added to your team; ask the other team to remove it first'}), 403
        return jsonify({'error': 'You can only move accounts of your own team or add parked accounts to it'}), 403

    if team_id is not None and team_id == event.organizerTeamId:
        return jsonify({'error': 'Only an admin can move accounts into the organizer (HQ) team'}), 400
    target_team = None
    if team_id is not None:
        target_team = next((t for t in _event_team_options(event) if t.id == team_id), None)
        if not target_team:
            return jsonify({'error': 'Unknown teamId'}), 400
    response = _perform_account_move(
        session, target_team, log_extra=' leaderTeam=%s%s' % (team.name, _leader_log(_current_leader_session())))
    body = response[0].get_json(silent=True) if isinstance(response, tuple) else None
    if body and 'account' in body:
        body['account'].pop('eventLinkId', None)
        return jsonify(body), response[1]
    return response
