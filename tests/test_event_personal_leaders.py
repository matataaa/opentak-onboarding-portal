"""
Personal leaders: portal users designated as leader of a team for one event (by an
admin, or by scanning the team's leader link while logged in). They keep their own
session and rights and get the team's leader page while the designation is valid.
"""
import datetime
from unittest.mock import MagicMock

import pytest
from flask_jwt_extended import create_access_token

from app import jobs
from app.api_v1 import events as events_mod
from app.api_v1 import users as users_mod
from app.extensions import scheduler
from app.models import (EventLeaderSessionModel, EventLinkModel, EventSessionModel, EventTeamLeaderModel,
                        EventTeamModel, SystemSettingsModel, UserModel)
from tests.test_event_device_accounts import _clean, _team_link  # noqa: F401
from tests.test_event_device_moves import API, _account, admin, ots, reader, world  # noqa: F401
from tests.test_event_links import _headers
from tests.test_event_my_team import _leader_headers, _leader_link, _user
from tests.test_event_organizer import hq  # noqa: F401

DENIED_MSG = 'Not available for event leader sessions'


def _bearer(user, **kwargs):
    return {'Authorization': 'Bearer ' + create_access_token(identity=str(user.id), **kwargs)}


@pytest.fixture
def pw(db, world):
    """World plus a normal portal user (callsign ANNA) and an anchor account on team A."""
    user = _user(db, 'personalanna')
    user.callsign = 'ANNA'
    anchor = _user(db, 'anchorleadera')
    world['a'].leaderUserId = anchor.id
    db.session.commit()
    yield dict(world, user=user, h=_bearer(user), anchor=anchor)
    # Designations reference teams/events/users: drop them before the shared cleanup
    db.session.rollback()
    EventTeamLeaderModel.query.delete()
    db.session.commit()


def _designate(client, headers, w, team, user=None):
    return client.post('%s/%d/team-leaders' % (API, w['eid']), headers=headers,
                       json={'teamId': team.id, 'userId': (user or w['user']).id})


def _my_team(client, h, **params):
    return client.get(API + '/my-team', headers=h, query_string=params)


def _designations(client, h):
    return client.get('/api/v1/auth/permissions', headers=h).get_json()['leaderDesignations']


def _ledger(w, user=None):
    return EventLeaderSessionModel.query.filter_by(userId=(user or w['user']).id).order_by(
        EventLeaderSessionModel.id).all()


# ---- admin CRUD / RBAC --------------------------------------------------------------
def test_designation_crud_and_rbac(client, db, pw, admin, reader):
    w = pw
    r = _designate(client, admin, w, w['a'])
    assert r.status_code == 201, r.get_json()
    d = r.get_json()
    assert (d['eventId'], d['eventTeamId'], d['userId'], d['source'], d['status']) == (
        w['eid'], w['a'].id, w['user'].id, 'admin', 'active')
    assert d['user'] == {'id': w['user'].id, 'username': 'personalanna', 'callsign': 'ANNA'}
    assert d['team'] == {'id': w['a'].id, 'name': 'A'} and d['createdBy']
    # the ledger row is created with the designation
    (row,) = _ledger(w)
    assert (row.eventId, row.eventTeamId, row.displayName, row.eventLinkId) == (w['eid'], w['a'].id, 'ANNA', None)

    listed = client.get('%s/%d/team-leaders' % (API, w['eid']), headers=reader)
    assert listed.status_code == 200 and [x['id'] for x in listed.get_json()['teamLeaders']] == [d['id']]
    assert client.get('%s/%d/team-leaders' % (API, w['eid'] + 999), headers=admin).status_code == 404
    # readonly cannot add or remove; a plain user cannot even list
    assert _designate(client, reader, w, w['b']).status_code == 403
    assert client.delete('%s/%d/team-leaders/%d' % (API, w['eid'], d['id']), headers=reader).status_code == 403
    assert client.get('%s/%d/team-leaders' % (API, w['eid']), headers=w['h']).status_code == 403
    # a shared leader session is denied everything here
    leader = _leader_headers(w['anchor'], w['a'], w['event'])
    for r in (client.get('%s/%d/team-leaders' % (API, w['eid']), headers=leader),
              _designate(client, leader, w, w['b']),
              client.delete('%s/%d/team-leaders/%d' % (API, w['eid'], d['id']), headers=leader)):
        assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}

    assert _my_team(client, w['h']).status_code == 200
    r = client.delete('%s/%d/team-leaders/%d' % (API, w['eid'], d['id']), headers=admin)
    assert r.status_code == 200
    # removing ends access at once and stamps the ledger row revoked
    assert _my_team(client, w['h']).status_code == 403
    assert _designations(client, w['h']) == []
    db.session.expire_all()
    assert _ledger(w)[0].revokedAt is not None and EventTeamLeaderModel.query.count() == 0
    assert client.delete('%s/%d/team-leaders/%d' % (API, w['eid'], d['id']), headers=admin).status_code == 404


def test_designation_validation(client, db, pw, admin):
    w = pw
    url = '%s/%d/team-leaders' % (API, w['eid'])
    for body in ({}, {'teamId': w['a'].id}, {'teamId': str(w['a'].id), 'userId': w['user'].id},
                 {'teamId': True, 'userId': w['user'].id}, {'teamId': w['a'].id, 'userId': None}):
        assert client.post(url, headers=admin, json=body).status_code == 400, body
    # a team without a link in this event does not take part in it
    lonely = EventTeamModel(name='Lonely')
    db.session.add(lonely)
    db.session.commit()
    assert _designate(client, admin, w, lonely).status_code == 400
    assert client.post(url, headers=admin, json={'teamId': 999999, 'userId': w['user'].id}).status_code == 400
    assert client.post(url, headers=admin, json={'teamId': w['a'].id, 'userId': 999999}).status_code == 400
    expired = _user(db, 'expiredperson')
    expired.expiryDate = datetime.datetime.now() - datetime.timedelta(days=1)
    db.session.commit()
    assert _designate(client, admin, w, w['a'], expired).status_code == 400
    # shared leader (anchor) accounts of any team are refused
    assert _designate(client, admin, w, w['b'], w['anchor']).status_code == 400
    assert _designate(client, admin, w, w['a']).status_code == 201
    assert _designate(client, admin, w, w['a']).status_code == 409
    assert EventTeamLeaderModel.query.count() == 1
    db.session.delete(expired)
    db.session.commit()


# ---- validity -----------------------------------------------------------------------
def test_validity_follows_event_module_and_team(client, db, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['a']).status_code == 201
    r = _my_team(client, w['h'])
    assert r.status_code == 200
    assert r.get_json()['team']['id'] == w['a'].id and r.get_json()['leaderName'] == 'ANNA'
    assert _designations(client, w['h']) == [{'eventId': w['eid'], 'eventName': 'ev', 'teamId': w['a'].id,
                                               'teamName': 'A', 'hq': False}]
    now = datetime.datetime.now()
    for field, bad, good in (('active', False, True),
                             ('validUntil', now - datetime.timedelta(minutes=1), w['event'].validUntil),
                             ('validFrom', now + datetime.timedelta(hours=1), w['event'].validFrom)):
        setattr(w['event'], field, bad)
        db.session.commit()
        assert _my_team(client, w['h']).status_code == 403, field
        assert client.get(API + '/my-team/accounts', headers=w['h']).status_code == 403, field
        assert _designations(client, w['h']) == [], field
        setattr(w['event'], field, good)
        db.session.commit()
        assert _my_team(client, w['h']).status_code == 200, field
    SystemSettingsModel.set_setting('events_enabled', False)
    assert _my_team(client, w['h']).status_code == 403
    assert _designations(client, w['h']) == []
    SystemSettingsModel.set_setting('events_enabled', True)
    # the team leaves the event with its last link
    assert client.delete('%s/links/%d' % (API, w['link_a'].id), headers=admin).status_code == 200
    assert _my_team(client, w['h']).status_code == 403
    assert _designations(client, w['h']) == []


def test_hq_designation_is_flagged(client, db, hq, pw, admin):
    assert _designate(client, admin, pw, hq['h']).status_code == 201
    assert _designations(client, pw['h'])[0]['hq'] is True


# ---- several designations / scope ---------------------------------------------------
def test_ambiguity_and_wrong_pair(client, db, ots, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['a']).status_code == 201
    assert _designate(client, admin, w, w['b']).status_code == 201
    r = _my_team(client, w['h'])
    assert r.status_code == 400 and r.get_json()['code'] == 'leader_team_required'
    assert _my_team(client, w['h'], eventId=w['eid']).status_code == 400
    assert _my_team(client, w['h'], eventId=w['eid'], teamId=w['a'].id).get_json()['team']['id'] == w['a'].id
    assert _my_team(client, w['h'], teamId=w['b'].id).get_json()['team']['id'] == w['b'].id
    assert _my_team(client, w['h'], eventId=w['eid'] + 999, teamId=w['a'].id).status_code == 403
    c, _ = _team_link(db, w['event'], 'C', color='Green')
    assert _my_team(client, w['h'], eventId=w['eid'], teamId=c.id).status_code == 403
    assert _my_team(client, w['h'], eventId='x', teamId=w['a'].id).status_code == 400
    # POST endpoints take the same query parameters
    r = client.post(API + '/my-team/enrollment', headers=w['h'])
    assert r.status_code == 400 and r.get_json()['code'] == 'leader_team_required'
    s = _account(db, ots, w, 'evt1_ambig001', team=w['b'])
    assert client.post('%s/my-team/accounts/%d/move' % (API, s.id), headers=w['h'],
                       json={'teamId': None}).status_code == 400
    r = client.post('%s/my-team/accounts/%d/move' % (API, s.id), headers=w['h'],
                    query_string={'eventId': w['eid'], 'teamId': w['b'].id}, json={'teamId': None})
    assert r.status_code == 200 and r.get_json()['account']['parked'] is True
    assert {d['teamId'] for d in _designations(client, w['h'])} == {w['a'].id, w['b'].id}


def test_normal_user_or_admin_without_designation_is_refused(client, db, ots, pw):
    w = pw
    s = _account(db, ots, w, 'evt1_nodesig1', team=w['a'])
    boss = _headers(db, 'pladmin', 'administrator')
    for h in (w['h'], boss):
        assert _my_team(client, h).status_code == 403
        assert _my_team(client, h, eventId=w['eid'], teamId=w['a'].id).status_code == 403
        assert client.get(API + '/my-team/accounts', headers=h).status_code == 403
        assert client.post(API + '/my-team/enrollment', headers=h, json={'forLeader': True}).status_code == 403
        assert client.post('%s/my-team/accounts/%d/move' % (API, s.id), headers=h,
                           json={'teamId': None}).status_code == 403
    assert EventLeaderSessionModel.query.count() == 0 and ots.calls == []


def test_leader_rules_apply_to_personal_leaders(client, db, ots, hq, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['a']).status_code == 201
    other = _account(db, ots, w, 'evt1_otherb01', team=w['b'])
    own = _account(db, ots, w, 'evt1_owna0001', team=w['a'])
    parked = _account(db, ots, w, 'evt1_parked01', team=w['b'], parked=True)

    def mv(s, team_id):
        return client.post('%s/my-team/accounts/%d/move' % (API, s.id), headers=w['h'], json={'teamId': team_id})

    assert mv(other, w['a'].id).status_code == 403   # active in another team
    assert mv(other, None).status_code == 403
    assert mv(own, hq['h'].id).status_code == 400    # never into HQ
    assert mv(parked, w['a'].id).status_code == 200  # pull a parked one
    assert mv(own, None).status_code == 200          # park an own one
    body = client.get(API + '/my-team/accounts', headers=w['h']).get_json()
    assert body['myTeamId'] == w['a'].id and hq['h'].id not in {t['id'] for t in body['teams']}


# ---- enrolment, ledger and the leader's own devices --------------------------------
def test_enrolment_ledger_and_for_me_direction(client, db, ots, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['a']).status_code == 201
    (row,) = _ledger(w)
    w['link_a'].direction = 'OUT'
    db.session.commit()
    # no leader link in the event: the own device joins through the player link, with BOTH
    assert client.post(API + '/my-team/enrollment', headers=w['h'], json={'forLeader': True}).status_code == 200
    assert client.post(API + '/my-team/enrollment', headers=w['h']).status_code == 200
    me, player = EventSessionModel.query.filter_by(mode='device').order_by(EventSessionModel.id).all()
    assert (me.role, me.eventLinkId, me.leaderSessionId, me.direction) == ('leader', w['link_a'].id, row.id, 'BOTH')
    assert (player.role, player.eventLinkId, player.leaderSessionId, player.direction) == (
        None, w['link_a'].id, row.id, 'OUT')
    assert events_mod._session_direction(me) == 'BOTH'
    assert events_mod._session_direction(player) == 'OUT'
    # with a leader link the own device takes that link's direction
    leader_link = _leader_link(w['a'], w['event'])
    leader_link.direction = 'IN'
    db.session.commit()
    assert client.post(API + '/my-team/enrollment', headers=w['h'], json={'forLeader': True}).status_code == 200
    me2 = EventSessionModel.query.filter_by(mode='device').order_by(EventSessionModel.id.desc()).first()
    assert (me2.role, me2.eventLinkId, me2.direction) == ('leader', leader_link.id, 'IN')
    # the same ledger row is reused
    assert [r.id for r in _ledger(w)] == [row.id]
    accounts = client.get('%s/%d/accounts' % (API, w['eid']), headers=admin).get_json()['accounts']
    assert {a['leaderName'] for a in accounts} == {'ANNA'}


def test_admin_leader_sessions_list_and_revoke_account_row(client, db, ots, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['a']).status_code == 201
    assert client.post(API + '/my-team/enrollment', headers=w['h'], json={'forLeader': True}).status_code == 200

    def listed():
        rows = client.get('%s/%d/leader-sessions' % (API, w['eid']), headers=admin).get_json()['leaderSessions']
        return [(r['kind'], r['username'], r['displayName'], r['status'], r['deviceCount']) for r in rows]

    assert listed() == [('account', 'personalanna', 'ANNA', 'active', 1)]
    w['event'].active = False
    db.session.commit()
    assert listed() == [('account', 'personalanna', 'ANNA', 'ended', 1)]
    w['event'].active = True
    db.session.commit()
    (row,) = _ledger(w)
    r = client.post('%s/%d/leader-sessions/%d/revoke' % (API, w['eid'], row.id), headers=admin)
    assert r.status_code == 200 and r.get_json()['status'] == 'revoked' and r.get_json()['kind'] == 'account'
    assert EventTeamLeaderModel.query.count() == 0
    assert _my_team(client, w['h']).status_code == 403
    assert listed() == [('account', 'personalanna', 'ANNA', 'revoked', 1)]
    # designating again gives a new ledger row
    assert _designate(client, admin, w, w['a']).status_code == 201
    assert [s for s in listed() if s[3] == 'active'] == [('account', 'personalanna', 'ANNA', 'active', 0)]


# ---- QR binding ---------------------------------------------------------------------
def _leader_url(w):
    _leader_link(w['a'], w['event'])
    return '%s/join/leader-tok-%d' % (API, w['a'].id)


def test_logged_in_user_scanning_leader_link_is_bound(client, db, pw, admin):
    w = pw
    url = _leader_url(w)
    link = EventLinkModel.query.filter_by(eventTeamId=w['a'].id, role='leader').one()
    r = client.post(url, headers=w['h'])
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['mode'] == 'leader-account'
    assert d['redirect'] == '/events/my-team?eventId=%d&teamId=%d' % (w['eid'], w['a'].id)
    assert 'access_token' not in d and 'refresh_token' not in d
    (des,) = EventTeamLeaderModel.query.all()
    assert (des.source, des.eventLinkId, des.linkVersion, des.linkNonce, des.userId) == (
        'link', link.id, link.tokenVersion, link.tokenNonce, w['user'].id)
    (row,) = _ledger(w)
    assert row.eventLinkId == link.id and row.displayName == 'ANNA'
    assert _my_team(client, w['h']).status_code == 200
    # scanning again reuses both
    assert client.post(url, headers=w['h']).status_code == 200
    assert EventTeamLeaderModel.query.count() == 1 and len(_ledger(w)) == 1
    # regenerating the leader link ends the QR-bound designation
    assert client.post('%s/links/%d/revoke' % (API, link.id), headers=admin).status_code == 200
    assert _my_team(client, w['h']).status_code == 403
    assert _designations(client, w['h']) == []
    statuses = [s['status'] for s in client.get('%s/%d/leader-sessions' % (API, w['eid']),
                                                headers=admin).get_json()['leaderSessions']]
    assert statuses == ['revoked']
    listed = client.get('%s/%d/team-leaders' % (API, w['eid']), headers=admin).get_json()['teamLeaders']
    assert [x['status'] for x in listed] == ['ended']


def test_scanning_keeps_an_admin_designation(client, db, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['a']).status_code == 201
    assert client.post(_leader_url(w), headers=w['h']).get_json()['mode'] == 'leader-account'
    (des,) = EventTeamLeaderModel.query.all()
    assert (des.source, des.eventLinkId) == ('admin', None)


def test_anonymous_leader_join_unchanged(client, db, pw):
    w = pw
    url = _leader_url(w)
    r = client.post(url)
    assert r.status_code == 400 and r.get_json()['code'] == 'display_name_required'
    r = client.post(url, json={'displayName': 'Bob'})
    assert r.status_code == 200 and r.get_json()['access_token'] and r.get_json()['role'] == 'leader'
    assert EventTeamLeaderModel.query.count() == 0


def test_anchor_account_scanning_takes_the_anonymous_flow(client, db, pw):
    w = pw
    url = _leader_url(w)
    h = _bearer(w['anchor'])
    r = client.post(url, headers=h)
    assert r.status_code == 400 and r.get_json()['code'] == 'display_name_required'
    r = client.post(url, headers=h, json={'displayName': 'Anchor'})
    assert r.status_code == 200 and r.get_json()['access_token']
    assert EventTeamLeaderModel.query.count() == 0


def test_shared_leader_token_is_never_bound(app, client, db, pw):
    w = pw
    url = _leader_url(w)
    leader = _leader_headers(w['anchor'], w['a'], w['event'])
    # the leader-scope hook refuses it before the join runs
    r = client.post(url, headers=leader, json={'displayName': 'X'})
    assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}
    assert EventTeamLeaderModel.query.count() == 0
    # and the join itself would treat it as anonymous
    with app.test_request_context(url, method='POST', headers=leader):
        assert events_mod._joining_account() is None
    with app.test_request_context(url, method='POST', headers=w['h']):
        assert events_mod._joining_account().id == w['user'].id


def test_expired_session_token_is_401_not_anonymous(client, db, pw):
    w = pw
    h = _bearer(w['user'], expires_delta=datetime.timedelta(seconds=-10))
    r = client.post(_leader_url(w), headers=h, json={'displayName': 'X'})
    assert r.status_code == 401
    assert EventTeamLeaderModel.query.count() == 0 and EventLeaderSessionModel.query.count() == 0


def test_account_binding_is_throttled(client, db, pw):
    w = pw
    url = _leader_url(w)
    for _ in range(events_mod.LEADER_JOIN_THROTTLE_MAX):
        assert client.post(url, headers=w['h']).status_code == 200
    assert client.post(url, headers=w['h']).status_code == 429


# ---- deletes ------------------------------------------------------------------------
def test_user_delete_with_designations(client, db, pw, admin, monkeypatch):
    w = pw
    victim = _user(db, 'personalvictim')
    assert _designate(client, admin, w, w['a'], victim).status_code == 201
    monkeypatch.setattr(users_mod, 'OTSClient', lambda *a, **k: MagicMock())
    r = client.delete('/api/v1/users/%d' % victim.id, headers=_headers(db, 'pluseradmin', 'user_admin'))
    assert r.status_code == 200, r.get_json()
    db.session.expire_all()
    assert UserModel.query.filter_by(username='personalvictim').first() is None
    assert EventTeamLeaderModel.query.count() == 0
    (row,) = EventLeaderSessionModel.query.all()  # the audit row stays, revoked
    assert row.revokedAt is not None


def test_expired_account_job_with_designations(app, client, db, ots, pw, admin, monkeypatch):
    w = pw
    victim = _user(db, 'personalexpired')
    assert _designate(client, admin, w, w['a'], victim).status_code == 201
    victim.expiryDate = datetime.datetime.now() - datetime.timedelta(days=1)
    db.session.commit()
    ots.users.append((victim.username, ['user']))
    SystemSettingsModel.set_setting('remove_expired_accounts_enabled', True)
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    try:
        jobs.remove_expired_accounts()
    finally:
        SystemSettingsModel.set_setting('remove_expired_accounts_enabled', False)
    db.session.expire_all()
    assert UserModel.query.filter_by(username='personalexpired').first() is None
    assert EventTeamLeaderModel.query.count() == 0


def test_event_and_team_delete_with_designations(client, db, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['b']).status_code == 201
    assert client.delete('%s/teams/%d' % (API, w['b'].id), headers=admin).status_code == 200
    assert EventTeamLeaderModel.query.count() == 0
    assert _designate(client, admin, w, w['a']).status_code == 201
    assert client.delete('%s/%d' % (API, w['eid']), headers=admin).status_code == 200
    assert EventTeamLeaderModel.query.count() == 0 and EventLeaderSessionModel.query.count() == 0


# ---- expired users / anchor accounts / impersonation -------------------------------
def test_expired_user_loses_and_cannot_gain_leader_access(client, db, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['a']).status_code == 201
    assert _my_team(client, w['h']).status_code == 200
    w['user'].expiryDate = datetime.datetime.now() - datetime.timedelta(minutes=1)
    db.session.commit()
    assert _my_team(client, w['h']).status_code == 403
    assert _designations(client, w['h']) == []
    statuses = {r['kind']: r['status'] for r in client.get(
        '%s/%d/leader-sessions' % (API, w['eid']), headers=admin).get_json()['leaderSessions']}
    assert statuses == {'account': 'ended'}
    listed = client.get('%s/%d/team-leaders' % (API, w['eid']), headers=admin).get_json()['teamLeaders']
    assert [x['status'] for x in listed] == ['ended']
    # scanning a leader link: a clear 403, not the anonymous flow
    r = client.post(_leader_url(w), headers=w['h'], json={'displayName': 'X'})
    assert r.status_code == 403 and 'expired' in r.get_json()['error'] and 'access_token' not in r.get_json()
    assert EventTeamLeaderModel.query.count() == 1
    w['user'].expiryDate = None
    db.session.commit()
    assert _my_team(client, w['h']).status_code == 200


def test_designation_of_a_later_anchor_account_is_invalid(client, db, pw, admin):
    w = pw
    assert _designate(client, admin, w, w['a']).status_code == 201
    w['b'].leaderUserId = w['user'].id
    db.session.commit()
    assert _my_team(client, w['h']).status_code == 403
    assert _designations(client, w['h']) == []
    w['b'].leaderUserId = None
    db.session.commit()
    assert _my_team(client, w['h']).status_code == 200


def test_impersonation_cannot_bind_and_is_logged(client, db, ots, pw, admin, caplog):
    w = pw
    imp = _bearer(w['user'], additional_claims={'impersonated_by': 1, 'impersonated_by_username': 'bossadmin'})
    r = client.post(_leader_url(w), headers=imp)
    assert r.status_code == 403 and 'admin team leaders screen' in r.get_json()['error']
    assert EventTeamLeaderModel.query.count() == 0
    # an admin-made designation still works while impersonating, and the log says who
    assert _designate(client, admin, w, w['a']).status_code == 201
    with caplog.at_level('INFO'):
        assert client.post(API + '/my-team/enrollment', headers=imp).status_code == 200
    assert 'leaderSession=%d name=ANNA impersonatedBy=bossadmin' % _ledger(w)[0].id in caplog.text


# ---- user picker --------------------------------------------------------------------
def _candidates(client, headers, w, **params):
    return client.get('%s/%d/team-leader-candidates' % (API, w['eid']), headers=headers, query_string=params)


def test_team_leader_candidates(client, db, pw, admin, reader):
    w = pw
    other = _user(db, 'personalbert')
    other.callsign = 'BERT'
    expired = _user(db, 'personalold')
    expired.expiryDate = datetime.datetime.now() - datetime.timedelta(days=1)
    db.session.commit()
    try:
        r = _candidates(client, admin, w, search='personal')
        assert r.status_code == 200
        rows = r.get_json()['users']
        names = {u['username'] for u in rows}
        assert {'personalanna', 'personalbert'} <= names and 'personalold' not in names
        assert all(set(u) == {'id', 'username', 'callsign'} for u in rows)
        # callsign search; anchor accounts never show up
        assert [u['username'] for u in _candidates(client, admin, w, search='bert').get_json()['users']] == [
            'personalbert']
        assert _candidates(client, admin, w, search='anchorleader').get_json()['users'] == []
        # already designated for the team: left out with teamId only
        assert _designate(client, admin, w, w['a']).status_code == 201
        names = {u['username'] for u in _candidates(client, admin, w, search='personal',
                                                    teamId=w['a'].id).get_json()['users']}
        assert 'personalanna' not in names and 'personalbert' in names
        names = {u['username'] for u in _candidates(client, admin, w, search='personal',
                                                    teamId=w['b'].id).get_json()['users']}
        assert 'personalanna' in names
        for bad in ('', 'p', ' p '):
            assert _candidates(client, admin, w, search=bad).status_code == 400
        assert client.get('%s/%d/team-leader-candidates' % (API, w['eid']), headers=admin).status_code == 400
        assert client.get('%s/%d/team-leader-candidates?search=pe' % (API, w['eid'] + 999),
                          headers=admin).status_code == 404
        # write role only; never for a shared leader session or a plain user
        assert _candidates(client, reader, w, search='personal').status_code == 403
        assert _candidates(client, w['h'], w, search='personal').status_code == 403
        r = _candidates(client, _leader_headers(w['anchor'], w['a'], w['event']), w, search='personal')
        assert r.status_code == 403 and r.get_json() == {'error': DENIED_MSG}
    finally:
        db.session.rollback()
        db.session.delete(UserModel.query.filter_by(username='personalold').one())
        db.session.commit()


def test_team_leader_candidates_are_capped(client, db, pw, admin):
    w = pw
    for i in range(events_mod.TEAM_LEADER_CANDIDATES_MAX + 3):
        _user(db, 'capuser%02d' % i)
    assert len(_candidates(client, admin, w, search='capuser').get_json()['users']) == \
        events_mod.TEAM_LEADER_CANDIDATES_MAX
