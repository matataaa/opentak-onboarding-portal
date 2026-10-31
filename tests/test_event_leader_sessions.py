"""
Leader session ledger (event_leader_sessions): every join through a leader link
names the person and gets its own session, which an admin can revoke without
regenerating the link. Also the ATAK role types written for leader and HQ devices.
"""
import datetime

import pytest
from flask_jwt_extended import create_access_token, create_refresh_token, decode_token

from app import jobs
from app.extensions import scheduler
from app.api_v1 import events as events_mod
from app.models import EventLeaderSessionModel, EventLinkModel, EventModel, EventSessionModel, EventTeamModel
from app.services import event_team_colors as etc
from tests.test_event_device_accounts import _clean  # noqa: F401
from tests.test_event_device_moves import API, _account, admin, ots, reader, world  # noqa: F401
from tests.test_event_links import _headers
from tests.test_event_my_team import _leader_claims, _leader_link, _user
from tests.test_event_organizer import hq  # noqa: F401
from tests.test_event_team_colors import eud

REVOKED_MSG = 'Leader session is no longer valid'
DENIED_MSG = 'Not available for event leader sessions'


@pytest.fixture
def lead(db, world):
    """World plus a shared leader account for team A and its leader link."""
    user = _user(db, 'ledgerleader')
    world['a'].leaderUserId = user.id
    db.session.commit()
    link = _leader_link(world['a'], world['event'])
    return dict(world, user=user, link=link, url='%s/join/leader-tok-%d' % (API, world['a'].id))


def _join(client, lead, name='Anna'):
    r = client.post(lead['url'], json={'displayName': name})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _bearer(token):
    return {'Authorization': 'Bearer ' + token}


def _dead(client, access, refresh=None):
    """A revoked/invalid session: 401 centrally, 403 on the my-team endpoints."""
    for path in ('/api/v1/auth/me', '/api/v1/auth/permissions'):
        r = client.get(path, headers=_bearer(access))
        assert r.status_code == 401 and r.get_json() == {'error': REVOKED_MSG}, path
    assert client.get(API + '/my-team', headers=_bearer(access)).status_code == 403
    assert client.get(API + '/my-team/accounts', headers=_bearer(access)).status_code == 403
    if refresh:
        r = client.post('/api/v1/auth/refresh', headers=_bearer(refresh))
        assert r.status_code == 401 and 'access_token' not in (r.get_json() or {})


def _alive(client, access, refresh=None):
    assert client.get(API + '/my-team', headers=_bearer(access)).status_code == 200
    assert client.get('/api/v1/auth/me', headers=_bearer(access)).status_code == 200
    if refresh:
        assert client.post('/api/v1/auth/refresh', headers=_bearer(refresh)).status_code == 200


def _revoke(client, headers, w, sid):
    return client.post('%s/%d/leader-sessions/%d/revoke' % (API, w['eid'], sid), headers=headers)


def _list(client, headers, w):
    return client.get('%s/%d/leader-sessions' % (API, w['eid']), headers=headers)


# ---- join ---------------------------------------------------------------------------
@pytest.mark.parametrize('body', [None, {}, {'displayName': ''}, {'displayName': '   '},
                                  {'displayName': 'x' * 81}, {'displayName': 42},
                                  {'displayName': None}, {'displayName': 'a\nb'}])
def test_join_requires_display_name(client, lead, body):
    r = client.post(lead['url'], json=body) if body is not None else client.post(lead['url'])
    assert r.status_code == 400
    assert r.get_json()['code'] == 'display_name_required'
    assert 'access_token' not in r.get_json()
    assert EventLeaderSessionModel.query.count() == 0


def test_display_name_is_trimmed_and_80_chars_allowed(client, lead):
    _join(client, lead, '  ' + 'x' * 80 + '  ')
    assert [r.displayName for r in EventLeaderSessionModel.query.all()] == ['x' * 80]


def test_player_join_needs_no_display_name(client, ots, lead):
    from tests.test_event_device_accounts import TOKEN
    assert client.post('%s/join/%sA' % (API, TOKEN)).status_code == 200


def test_join_creates_ledger_row_and_claim_on_access_and_refresh(client, lead):
    d = _join(client, lead, '  Anna  ')
    row = EventLeaderSessionModel.query.one()
    assert (row.eventId, row.eventTeamId, row.eventLinkId) == (lead['eid'], lead['a'].id, lead['link'].id)
    assert row.displayName == 'Anna' and row.userId is None and row.revokedAt is None
    assert row.createdAt is not None
    assert d['leaderName'] == 'Anna'
    assert decode_token(d['access_token'])['event_leader_session'] == row.id
    assert decode_token(d['refresh_token'])['event_leader_session'] == row.id
    r = client.post('/api/v1/auth/refresh', headers=_bearer(d['refresh_token']))
    assert r.status_code == 200
    assert decode_token(r.get_json()['access_token'])['event_leader_session'] == row.id
    body = client.get(API + '/my-team', headers=_bearer(r.get_json()['access_token'])).get_json()
    assert body['leaderName'] == 'Anna'


def test_every_join_is_its_own_session(client, lead):
    a, b = _join(client, lead, 'Anna'), _join(client, lead, 'Anna')
    ids = {decode_token(a['access_token'])['event_leader_session'],
           decode_token(b['access_token'])['event_leader_session']}
    assert len(ids) == 2 and EventLeaderSessionModel.query.count() == 2


# ---- validation of the claim ---------------------------------------------------------
def _tokens(lead, **override):
    claims = _leader_claims(lead['a'], lead['event'])
    claims.update(override)
    claims = {k: v for k, v in claims.items() if v is not None}
    ident = str(lead['user'].id)
    return (create_access_token(identity=ident, additional_claims=claims),
            create_refresh_token(identity=ident, additional_claims=claims))


def test_token_without_session_claim_fails_closed(client, lead):
    """Leader tokens issued before the ledger have no claim: the leader must join again."""
    _dead(client, *_tokens(lead, event_leader_session=None))


def test_session_claim_must_match_a_live_row(client, db, lead):
    _dead(client, *_tokens(lead, event_leader_session=999999))
    # a row of another team / link / event never matches
    other = EventLeaderSessionModel(eventId=lead['eid'], eventTeamId=lead['b'].id,
                                    eventLinkId=lead['link'].id, displayName='x')
    wrong_link = EventLeaderSessionModel(eventId=lead['eid'], eventTeamId=lead['a'].id,
                                         eventLinkId=lead['link_a'].id, displayName='y')
    db.session.add_all([other, wrong_link])
    db.session.commit()
    _dead(client, *_tokens(lead, event_leader_session=other.id))
    _dead(client, *_tokens(lead, event_leader_session=wrong_link.id))


# ---- revoke one / regenerate all ----------------------------------------------------
def test_revoke_one_of_two_sessions_of_the_same_team(client, db, lead, admin):
    anna, bob = _join(client, lead, 'Anna'), _join(client, lead, 'Bob')
    sid = decode_token(anna['access_token'])['event_leader_session']
    version = lead['link'].tokenVersion
    r = _revoke(client, admin, lead, sid)
    assert r.status_code == 200
    body = r.get_json()
    assert body['id'] == sid and body['displayName'] == 'Anna'
    assert body['revokedAt'] and body['revokedBy']
    _dead(client, anna['access_token'], anna['refresh_token'])
    _alive(client, bob['access_token'], bob['refresh_token'])
    db.session.expire_all()
    assert EventLinkModel.query.get(lead['link'].id).tokenVersion == version  # link untouched
    assert _revoke(client, admin, lead, sid).status_code == 409
    assert _revoke(client, admin, lead, 999999).status_code == 404
    r = client.post('%s/%d/leader-sessions/%d/revoke' % (API, lead['eid'] + 999, sid), headers=admin)
    assert r.status_code == 404


def test_regenerating_the_link_still_kills_all_its_sessions(client, db, lead, admin):
    anna, bob = _join(client, lead, 'Anna'), _join(client, lead, 'Bob')
    assert client.post('%s/links/%d/revoke' % (API, lead['link'].id), headers=admin).status_code == 200
    _dead(client, anna['access_token'], anna['refresh_token'])
    _dead(client, bob['access_token'], bob['refresh_token'])
    db.session.expire_all()
    assert all(r.revokedAt is not None for r in EventLeaderSessionModel.query.all())


def test_deleting_the_link_keeps_the_audit_rows(client, db, lead, admin):
    anna = _join(client, lead, 'Anna')
    assert client.delete('%s/links/%d' % (API, lead['link'].id), headers=admin).status_code == 200
    _dead(client, anna['access_token'], anna['refresh_token'])
    db.session.expire_all()
    row = EventLeaderSessionModel.query.one()
    assert row.eventLinkId == lead['link'].id and row.revokedAt is not None
    listed = _list(client, admin, lead).get_json()['leaderSessions']
    assert [s['displayName'] for s in listed] == ['Anna']


# ---- admin list / RBAC -------------------------------------------------------------
def test_list_and_revoke_rbac(client, db, lead, admin, reader):
    anna = _join(client, lead, 'Anna')
    sid = decode_token(anna['access_token'])['event_leader_session']
    r = _list(client, reader, lead)
    assert r.status_code == 200
    (row,) = r.get_json()['leaderSessions']
    assert row == {
        'id': sid, 'kind': 'link', 'username': None, 'eventId': lead['eid'], 'eventTeamId': lead['a'].id,
        'team': {'id': lead['a'].id, 'name': 'A'}, 'eventLinkId': lead['link'].id,
        'displayName': 'Anna', 'createdAt': row['createdAt'], 'revokedAt': None,
        'revokedBy': None, 'deviceCount': 0, 'status': 'active',
    }
    assert _list(client, admin, lead).status_code == 200
    assert client.get('%s/%d/leader-sessions' % (API, lead['eid'] + 999), headers=admin).status_code == 404
    # readonly may list but not revoke; a plain user may do neither
    assert _revoke(client, reader, lead, sid).status_code == 403
    plain = {'Authorization': 'Bearer ' + create_access_token(identity=str(_user(db, 'ledgerplain').id))}
    assert _list(client, plain, lead).status_code == 403
    assert _revoke(client, plain, lead, sid).status_code == 403
    # a leader (also an HQ leader) can neither list nor revoke
    leader = _bearer(anna['access_token'])
    for r in (_list(client, leader, lead), _revoke(client, leader, lead, sid)):
        assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}
    _alive(client, anna['access_token'])


# ---- devices created by a leader -----------------------------------------------------
def test_leader_devices_store_session_and_expose_leader_name(client, db, ots, lead, admin):
    anna = _join(client, lead, 'Anna')
    sid = decode_token(anna['access_token'])['event_leader_session']
    h = _bearer(anna['access_token'])
    assert client.post(API + '/my-team/enrollment', headers=h, json={'forLeader': True}).status_code == 200
    assert client.post(API + '/my-team/enrollment', headers=h).status_code == 200
    own = EventSessionModel.query.filter_by(mode='device').order_by(EventSessionModel.id).all()
    assert [(s.role, s.leaderSessionId) for s in own] == [('leader', sid), (None, sid)]
    plain = _account(db, ots, lead, 'evt1_plainacc', team=lead['a'])
    other = _account(db, ots, lead, 'evt1_otherteam', team=lead['b'])
    parked = _account(db, ots, lead, 'evt1_parkedacc', team=lead['a'], parked=True)
    other.leaderSessionId = sid  # created by a leader, now active in another team
    parked.leaderSessionId = sid  # created by a leader, now parked
    db.session.commit()

    accounts = {a['id']: a for a in client.get(
        '%s/%d/accounts' % (API, lead['eid']), headers=admin).get_json()['accounts']}
    assert [accounts[s.id]['leaderName'] for s in own] == ['Anna', 'Anna']
    assert accounts[plain.id]['leaderName'] is None
    assert accounts[other.id]['leaderName'] == 'Anna'

    mine = {a['id']: a for a in client.get(API + '/my-team/accounts', headers=h).get_json()['accounts']}
    assert [mine[s.id]['leaderName'] for s in own] == ['Anna', 'Anna']
    assert accounts[parked.id]['leaderName'] == 'Anna'
    # only the own team's accounts say who enrolled them (not other teams, not parked)
    assert mine[other.id]['leaderName'] is None
    assert mine[parked.id]['leaderName'] is None and mine[parked.id]['parked'] is True

    assert _list(client, admin, lead).get_json()['leaderSessions'][0]['deviceCount'] == 4
    # revoking the leader does not close the devices they created
    assert _revoke(client, admin, lead, sid).get_json()['deviceCount'] == 4
    db.session.expire_all()
    assert EventSessionModel.query.filter_by(leaderSessionId=sid, closedAt=None).count() == 4


def test_leader_move_logs_the_leader_session(client, db, ots, lead, caplog):
    anna = _join(client, lead, 'Anna')
    sid = decode_token(anna['access_token'])['event_leader_session']
    s = _account(db, ots, lead, 'evt1_logged01', team=lead['a'])
    with caplog.at_level('INFO'):
        r = client.post('%s/my-team/accounts/%d/move' % (API, s.id), headers=_bearer(anna['access_token']),
                        json={'teamId': None})
    assert r.status_code == 200
    assert 'leaderSession=%d name=Anna' % sid in caplog.text


# ---- deletes with ledger rows ------------------------------------------------------
def test_event_delete_removes_its_ledger_rows(client, db, lead, admin):
    _join(client, lead, 'Anna')
    assert client.delete('%s/%d' % (API, lead['eid']), headers=admin).status_code == 200
    assert EventLeaderSessionModel.query.count() == 0
    assert EventModel.query.get(lead['eid']) is None


def test_team_delete_removes_its_ledger_rows(client, db, lead, admin):
    _join(client, lead, 'Anna')
    keep = EventLeaderSessionModel(eventId=lead['eid'], eventTeamId=lead['b'].id, displayName='Bob')
    db.session.add(keep)
    db.session.commit()
    assert client.delete('%s/teams/%d' % (API, lead['a'].id), headers=admin).status_code == 200
    assert [r.displayName for r in EventLeaderSessionModel.query.all()] == ['Bob']
    assert EventTeamModel.query.get(lead['a'].id) is None


# ---- ATAK role type ----------------------------------------------------------------
def _roles(ots):
    return {uid: v for (k, uid), v in ots.profiles.items() if k == etc.ROLE_KEY}


def _colours(ots):
    return {uid: v for (k, uid), v in ots.profiles.items() if k == etc.COLOR_KEY}


def test_role_types_and_cleanup(app, client, db, ots, hq, admin, monkeypatch):
    w = hq
    player = _account(db, ots, w, 'evt1_player01', team=w['a'])
    lead_dev = _account(db, ots, w, 'evt1_leader01', team=w['a'])
    hq_dev = _account(db, ots, w, 'evt1_hqdev001', team=w['h'])
    hq_lead = _account(db, ots, w, 'evt1_hqlead01', team=w['h'])
    lead_dev.role = hq_lead.role = 'leader'
    db.session.commit()
    ots.euds = [eud('P', player.otsUsername), eud('L', lead_dev.otsUsername),
                eud('H', hq_dev.otsUsername), eud('HL', hq_lead.otsUsername)]
    etc.sync_event_team_colors()
    assert _roles(ots) == {'P': 'Team Member', 'L': 'Team Lead', 'H': 'HQ', 'HL': 'HQ'}

    # moving out of HQ: the HQ role is replaced
    assert client.post('%s/%d/accounts/%d/move' % (API, w['eid'], hq_dev.id), headers=admin,
                       json={'teamId': w['b'].id}).status_code == 200
    etc.sync_event_team_colors()
    assert _roles(ots)['H'] == 'Team Member' and _colours(ots)['H'] == 'Blue'

    # parking the leader device removes Team Lead and its colour
    assert client.post('%s/%d/accounts/%d/move' % (API, w['eid'], lead_dev.id), headers=admin,
                       json={'teamId': None}).status_code == 200
    etc.sync_event_team_colors()
    assert 'L' not in _roles(ots) and 'L' not in _colours(ots)

    # closing the HQ leader device removes HQ and its colour
    assert client.post('%s/%d/accounts/%d/close' % (API, w['eid'], hq_lead.id),
                       headers=admin).status_code == 200
    assert 'HL' not in _roles(ots) and 'HL' not in _colours(ots)

    # event end: the close job removes the rest (Team Member, and Team Member after the move)
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    past = datetime.datetime.now() - datetime.timedelta(minutes=1)
    w['event'].validUntil = past
    EventSessionModel.query.filter_by(closedAt=None).update({'expiresAt': past})
    db.session.commit()
    jobs.close_expired_event_sessions()
    assert _roles(ots) == {} and _colours(ots) == {}


def test_manual_role_survives_park_close_and_event_end(app, client, db, ots, hq, admin, monkeypatch):
    w = hq
    parked = _account(db, ots, w, 'evt1_manual01', team=w['a'])
    closed = _account(db, ots, w, 'evt1_manual02', team=w['h'])
    ended = _account(db, ots, w, 'evt1_manual03', team=w['b'])
    parked.role = 'leader'
    db.session.commit()
    ots.euds = [eud('M1', parked.otsUsername), eud('M2', closed.otsUsername), eud('M3', ended.otsUsername)]
    etc.sync_event_team_colors()
    assert _roles(ots) == {'M1': 'Team Lead', 'M2': 'HQ', 'M3': 'Team Member'}
    # an admin sets other roles by hand (the sync rewrites them while a device is in a
    # team, so each is set right before the device leaves)
    ots.profiles[(etc.ROLE_KEY, 'M1')] = 'Medic'
    assert client.post('%s/%d/accounts/%d/move' % (API, w['eid'], parked.id), headers=admin,
                       json={'teamId': None}).status_code == 200
    etc.sync_event_team_colors()
    ots.profiles[(etc.ROLE_KEY, 'M2')] = 'Sniper'
    ots.profiles[(etc.ROLE_KEY, 'M3')] = 'RTO'
    assert client.post('%s/%d/accounts/%d/close' % (API, w['eid'], closed.id),
                       headers=admin).status_code == 200
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    past = datetime.datetime.now() - datetime.timedelta(minutes=1)
    w['event'].validUntil = past
    EventSessionModel.query.filter_by(id=ended.id).update({'expiresAt': past})
    db.session.commit()
    jobs.close_expired_event_sessions()
    assert _roles(ots) == {'M1': 'Medic', 'M2': 'Sniper', 'M3': 'RTO'}
    assert _colours(ots) == {}


def test_shared_leader_account_gets_team_lead_or_hq(db, hq):
    w = hq
    la, lh = _user(db, 'roleleadera'), _user(db, 'roleleaderh')
    w['a'].leaderUserId, w['h'].leaderUserId = la.id, lh.id
    db.session.commit()
    _leader_link(w['a'], w['event'])
    _leader_link(w['h'], w['event'])
    desired = etc._build_desired_state([w['event']])
    assert desired['roleleadera'] == ('Red', 'Team Lead')
    assert desired['roleleaderh'] == ('White', 'HQ')


# ---- join throttle / status / HQ ordering ------------------------------------------
def test_leader_joins_are_throttled_per_link(client, db, lead):
    for i in range(events_mod.LEADER_JOIN_THROTTLE_MAX):
        _join(client, lead, 'Leader %d' % i)
    r = client.post(lead['url'], json={'displayName': 'One too many'})
    assert r.status_code == 429 and 'access_token' not in r.get_json()
    assert EventLeaderSessionModel.query.count() == events_mod.LEADER_JOIN_THROTTLE_MAX
    # requests without a valid name are rejected before the throttle and do not count
    events_mod._device_attempts.clear()
    for _ in range(events_mod.LEADER_JOIN_THROTTLE_MAX + 2):
        assert client.post(lead['url'], json={}).status_code == 400
    _join(client, lead, 'Still fine')


def _statuses(client, headers, w):
    return {s['displayName']: s['status'] for s in _list(client, headers, w).get_json()['leaderSessions']}


def test_list_status_is_derived_server_side(client, db, lead, admin):
    anna, _bob = _join(client, lead, 'Anna'), _join(client, lead, 'Bob')
    assert _statuses(client, admin, lead) == {'Anna': 'active', 'Bob': 'active'}
    assert _revoke(client, admin, lead, decode_token(anna['access_token'])['event_leader_session']
                   ).get_json()['status'] == 'revoked'
    assert _statuses(client, admin, lead) == {'Anna': 'revoked', 'Bob': 'active'}
    # whatever leader_session_error refuses shows as ended
    lead['event'].active = False
    db.session.commit()
    assert _statuses(client, admin, lead) == {'Anna': 'revoked', 'Bob': 'ended'}
    lead['event'].active = True
    lead['a'].leaderUserId = None
    db.session.commit()
    assert _statuses(client, admin, lead) == {'Anna': 'revoked', 'Bob': 'ended'}
    lead['a'].leaderUserId = lead['user'].id
    db.session.commit()
    assert _statuses(client, admin, lead)['Bob'] == 'active'
    # regenerating (or deleting) the link stamps its sessions revoked
    assert client.post('%s/links/%d/revoke' % (API, lead['link'].id), headers=admin).status_code == 200
    assert _statuses(client, admin, lead) == {'Anna': 'revoked', 'Bob': 'revoked'}


def test_account_leading_several_teams_gets_hq_from_the_organizer_team(db, hq):
    w = hq
    user = _user(db, 'roleleaderboth')
    w['a'].leaderUserId = w['h'].leaderUserId = user.id
    db.session.commit()
    _leader_link(w['a'], w['event'])  # lower link id than the HQ link
    _leader_link(w['h'], w['event'])
    assert etc._build_desired_state([w['event']])['roleleaderboth'] == ('White', 'HQ')
