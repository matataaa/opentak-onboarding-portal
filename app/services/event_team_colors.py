"""
Event Team Colour Sync Service

Applies an event team's colour to the ATAK devices (EUDs) of the people
playing in that team, using OTS device profiles (/api/profiles).

Profiles written per EUD (eud_uid is always set; a profile without one would
apply to every device):
- locationTeam = team colour
- atakRoleType = Team Member

Who gets a colour is derived only from portal-owned data (event sessions and
the team's leader account), never from the team's OTS group members, because
that group also contains self-service users.
"""

from datetime import datetime, timedelta
from flask import current_app
from app.models import (
    EventModel, EventLinkModel, EventSessionModel, EventTeamModel, SystemSettingsModel,
)
from app.ots import otsClient

COLOR_KEY = 'locationTeam'
ROLE_KEY = 'atakRoleType'
ROLE_VALUE = 'Team Member'
VALUE_CLASS = 'class java.lang.String'


def _open_events(now):
    """Active events whose window contains now (a missing bound is open)."""
    return [
        e for e in EventModel.query.filter_by(active=True).all()
        if (not e.validFrom or e.validFrom <= now) and (not e.validUntil or now < e.validUntil)
    ]


def _build_desired_state(events):
    """Return {username: (colour, role)} for the given open events."""
    from app.api_v1.events import TAK_TEAM_COLORS  # lazy: avoids a circular import
    event_ids = [e.id for e in events]
    desired = {}

    sessions = EventSessionModel.query.filter(
        EventSessionModel.eventId.in_(event_ids),
        EventSessionModel.mode.in_(['shared', 'individual']),
        EventSessionModel.closedAt.is_(None),
    ).all()
    for session in sessions:
        if session.otsUsername and session.team and session.team.color in TAK_TEAM_COLORS:
            desired[session.otsUsername] = (session.team.color, ROLE_VALUE)

    leader_links = EventLinkModel.query.filter(
        EventLinkModel.eventId.in_(event_ids), EventLinkModel.role == 'leader'
    ).all()
    for team in {link.team for link in leader_links if link.team}:
        if team.leaderUser and team.color in TAK_TEAM_COLORS:
            desired.setdefault(team.leaderUser.username, (team.color, ROLE_VALUE))
    return desired


def read_profiles():
    """Return {preference_key: {eud_uid: value}} for the keys this service manages."""
    existing = {}
    for key in (COLOR_KEY, ROLE_KEY):
        response = otsClient.get_all_pages(otsClient.get_device_profiles, preference_key=key)
        results = response.get('response', {}).get('results', [])
        existing[key] = {p['eud_uid']: p.get('preference_value') for p in results if p.get('eud_uid')}
    return existing


def _delete_profiles(uid, existing, color):
    """Delete the colour/role profiles of an EUD, but only those still holding the
    value this service wrote (so values an admin set by hand are kept)."""
    expected = {COLOR_KEY: color, ROLE_KEY: ROLE_VALUE}
    for key, value in expected.items():
        if existing[key].get(uid) == value:
            otsClient.delete_device_profile(key, uid)
            del existing[key][uid]


def sync_event_team_colors():
    """Write team colour profiles for EUDs of event participants (only differences)."""
    if not SystemSettingsModel.get_setting('events_enabled', False):
        return
    now = datetime.now()
    events = _open_events(now)
    if not events:
        # Leaders have no session, so their colours are removed by this pass. Keep
        # running for 24h after an event ended; a reassigned leader or a deactivated
        # event is likewise only cleaned up within that window.
        recently_ended = EventModel.query.filter(
            EventModel.validUntil >= now - timedelta(hours=24), EventModel.validUntil < now).count()
        if not recently_ended:
            return

    desired = _build_desired_state(events)

    # Leader accounts that are not (or no longer) wanted: only their own colour is removed.
    leader_colors = {
        t.leaderUser.username: t.color
        for t in EventTeamModel.query.filter(EventTeamModel.leaderUserId.isnot(None)).all()
        if t.leaderUser and t.color
    }

    euds = otsClient.get_all_pages(otsClient.get_euds).get('response', {}).get('results', [])
    existing = read_profiles()

    for eud in euds:
        uid = eud.get('uid')
        username = eud.get('username')
        if not uid or not username:
            continue
        try:
            if username in desired:
                color, role = desired[username]
                for key, value in ((COLOR_KEY, color), (ROLE_KEY, role)):
                    if existing[key].get(uid) != value:
                        # OTS upserts on (preference_key, eud_uid)
                        otsClient.create_device_profile(
                            key, value, VALUE_CLASS,
                            enrollment=False, connection=True, active=True, eud_uid=uid)
            elif leader_colors.get(username):
                _delete_profiles(uid, existing, leader_colors[username])
        except Exception as e:
            current_app.logger.error(f"Event team colour sync failed for EUD {uid} ({username}): {e}")


def remove_event_colors_for_username(username, color, existing=None):
    """Delete the team colour/role profiles of all EUDs belonging to username.

    Only values equal to the team colour / role this service writes are removed.
    Pass existing (from read_profiles) to avoid re-reading profiles per call.
    Returns True only if every delete succeeded.
    """
    euds = otsClient.get_all_pages(otsClient.get_euds, username=username).get('response', {}).get('results', [])
    if existing is None:
        existing = read_profiles()
    ok = True
    for eud in euds:
        uid = eud.get('uid')
        if not uid or eud.get('username') != username:
            continue
        try:
            _delete_profiles(uid, existing, color)
        except Exception as e:
            ok = False
            current_app.logger.error(f"Removing event colour for EUD {uid} ({username}) failed: {e}")
    return ok
