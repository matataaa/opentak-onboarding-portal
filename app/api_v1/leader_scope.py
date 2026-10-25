"""
Deny-by-default scope for event-leader sessions.

An event-leader JWT (access or refresh token carrying the `event_leader_team`
claim, issued by `_join_as_leader` in events.py) belongs to the shared leader
account's REAL local user, so on its own it would pass every `@jwt_required()`
endpoint (profile, password change, data packages, radios, ...). To prevent
that, ONE `before_request` hook on the `api_v1` blueprint rejects every request
made with such a token unless (endpoint, method) is in LEADER_ALLOWED below.

- New routes are therefore denied for leader sessions automatically; adding one
  to the allowlist is a deliberate act.
- The hook only reads the token. Missing/invalid/expired tokens fall through, so
  each endpoint's own decorator still produces its usual 401.
- Tokens without the claim (users, admins, impersonation, kiosk) and API keys
  (X-API-Key, no JWT) are not touched.
- Allowlisted endpoints additionally require the leader session to still be valid
  (events.leader_session_error), else 401, so revocation also blocks /auth/refresh.
- A refresh token is checked the same way (verify_type=False), so it cannot be
  used on anything but /auth/refresh.
"""
from flask import jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request

from app.api_v1 import api_v1

# (endpoint name, HTTP method). Keep minimal; each entry is something the leader
# page needs.
LEADER_ALLOWED = frozenset({
    # The leader page itself.
    ('api_v1.get_my_team', 'GET'),
    ('api_v1.create_my_team_enrollment', 'POST'),
    ('api_v1.list_my_team_accounts', 'GET'),
    ('api_v1.move_my_team_account', 'POST'),
    # Token refresh (the SPA's 401 interceptor); keeps the leader claims and hard expiry.
    ('api_v1.refresh', 'POST'),
    # AuthContext fetches both on every load and logs the user out if either fails.
    # /auth/permissions yields the `event_leader` role the SPA uses to route leaders.
    ('api_v1.get_current_user', 'GET'),
    ('api_v1.get_user_permissions', 'GET'),
})

# Endpoints that run `_leader_scope` themselves and answer revoked sessions with 403.
LEADER_SELF_CHECKED = frozenset({
    'api_v1.get_my_team',
    'api_v1.create_my_team_enrollment',
    'api_v1.list_my_team_accounts',
    'api_v1.move_my_team_account',
})


@api_v1.before_request
def restrict_event_leader_sessions():
    if request.method == 'OPTIONS':
        return None
    try:
        verify_jwt_in_request(optional=True, verify_type=False)
        claims = get_jwt()
    except Exception:
        # Invalid/expired/revoked token: the endpoint's own decorator answers.
        return None
    if not claims or claims.get('event_leader_team') is None:
        return None
    if (request.endpoint, request.method) in LEADER_ALLOWED:
        # Central revocation gate: a revoked/expired leader session gets 401 on every
        # allowlisted endpoint (incl. /auth/refresh), so the SPA logs the leader out
        # and an old token cannot mint a new one.
        from app.api_v1.events import leader_session_error
        if (request.endpoint not in LEADER_SELF_CHECKED
                and leader_session_error(claims, get_jwt_identity())):
            return jsonify({'error': 'Leader session is no longer valid'}), 401
        return None
    return jsonify({'error': 'Not available for event leader sessions'}), 403
