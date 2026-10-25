"""Leader side of per-device event accounts: list all, pull into / take out of own team."""
import datetime

import pytest

from app.api_v1 import events as events_mod
from app.models import EventLinkModel, EventSessionModel, SystemSettingsModel, UserModel

from tests.test_event_device_accounts import _device, _event, _team_link
from tests.test_event_device_moves import (  # noqa: F401
    FakeOTS, PARKED, PARKED_IN, _account, _clean, ots)
from tests.test_event_my_team import _leader_headers, _user

BASE = '/api/v1/events/my-team/accounts'


@pytest.fixture
def world(db):
    SystemSettingsModel.set_setting('events_enabled', True)
    event = _event(db)
    a, link_a = _team_link(db, event, 'A', color='Red')
    b, link_b = _team_link(db, event, 'B', color='Blue')
    c, link_c = _team_link(db, event, 'C', color='Green')
    leader = _user(db, 'mtleader')
    a.leaderUserId = leader.id
    db.session.commit()
    h = _leader_headers(leader, a, event)
    return dict(event=event, a=a, b=b, c=c, eid=event.id, leader=leader, h=h)


def _mv(client, w, s, team_id, headers=None):
    return client.post('%s/%d/move' % (BASE, s.id), headers=headers or w['h'],
                       json={'teamId': team_id})


def _reload(db, s):
    db.session.expire_all()
    return EventSessionModel.query.get(s.id)


def test_list_shows_all_teams_parked_legacy_not_other_event(client, db, ots, world):
    w = world
    own = _account(db, ots, w, 'evt1_own00000', team=w['a'])
    oth = _account(db, ots, w, 'evt1_oth00000', team=w['b'])
    par = _account(db, ots, w, 'evt1_par00000', team=w['b'], parked=True)
    leg = _account(db, ots, w, 'evt1_leg00000', team=w['c'], mode='shared')
    closed = _account(db, ots, w, 'evt1_clo00000', team=w['b'])
    closed.closedAt = datetime.datetime.now()
    db.session.commit()
    ev2 = _event(db)
    t2, _ = _team_link(db, ev2, 'Z')
    _device(db, ev2, t2, 'evt2_zzzzzzzz')
    r = client.get(BASE, headers=w['h'])
    assert r.status_code == 200
    d = r.get_json()
    by = {x['otsUsername']: x for x in d['accounts']}
    assert set(by) == {'evt1_own00000', 'evt1_oth00000', 'evt1_par00000', 'evt1_leg00000'}
    assert by['evt1_own00000']['inMyTeam'] is True
    assert by['evt1_oth00000']['inMyTeam'] is False
    assert by['evt1_par00000']['parked'] is True and by['evt1_par00000']['inMyTeam'] is False
    assert by['evt1_leg00000']['legacy'] is True and by['evt1_leg00000']['inMyTeam'] is False
    assert d['myTeamId'] == w['a'].id
    assert {t['id'] for t in d['teams']} == {w['a'].id, w['b'].id, w['c'].id}
    assert 'eudsAvailable' in d
    _assert_no_secrets(d, w)
    # other teams' active rows: no uid / lastSeen / link id
    assert 'eventLinkId' not in by['evt1_oth00000']


def _assert_no_secrets(body, w):
    sentinels = {'nonce-A', 'nonce-B', 'nonce-C', 'leader-tok'}
    sentinels |= {l.tokenHash for l in EventLinkModel.query.all() if l.tokenHash and len(l.tokenHash) >= 8}
    sentinels |= {l.tokenNonce for l in EventLinkModel.query.all() if l.tokenNonce and len(l.tokenNonce) >= 8}
    bad_keys = ('token', 'password', 'nonce', 'qr')

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                assert not any(b in k.lower() for b in bad_keys), k
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            assert not any(sv and sv in o for sv in sentinels), o
    walk(body)


def test_list_euds_joined(client, db, ots, world, monkeypatch):
    w = world
    _account(db, ots, w, 'evt1_own00000', team=w['a'])
    monkeypatch.setattr(events_mod, '_read_eud_index', lambda names: {
        'evt1_own00000': [{'callsign': 'ALPHA', 'uid': 'u1', 'lastSeen': None}]})
    d = client.get(BASE, headers=w['h']).get_json()
    assert d['accounts'][0]['euds'][0]['callsign'] == 'ALPHA'
    assert d['eudsAvailable'] is True


def test_pull_from_other_team_forbidden(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['b'])
    r = _mv(client, w, s, w['a'].id)
    assert r.status_code == 403
    assert 'Only parked accounts can be added to your team' in r.get_json()['error']
    s = _reload(db, s)
    assert s.eventTeamId == w['b'].id and s.parkedAt is None
    assert ots.calls == []


def test_list_minimises_other_team_euds(client, db, ots, world, monkeypatch):
    w = world
    _account(db, ots, w, 'evt1_own00000', team=w['a'])
    _account(db, ots, w, 'evt1_oth00000', team=w['b'])
    _account(db, ots, w, 'evt1_par00000', team=w['b'], parked=True)
    eud = lambda c: [{'callsign': c, 'uid': 'uid-' + c, 'lastSeen': '2026-01-01T00:00:00'}]
    monkeypatch.setattr(events_mod, '_read_eud_index', lambda names: {
        'evt1_own00000': eud('OWN'), 'evt1_oth00000': eud('OTH'), 'evt1_par00000': eud('PAR')})
    by = {x['otsUsername']: x for x in client.get(BASE, headers=w['h']).get_json()['accounts']}
    assert by['evt1_oth00000']['euds'] == [{'callsign': 'OTH'}]
    assert by['evt1_own00000']['euds'][0]['uid'] == 'uid-OWN'
    assert by['evt1_par00000']['euds'][0]['uid'] == 'uid-PAR'


def test_move_response_has_no_link_id(client, db, ots, world):
    w = world
    link = EventLinkModel.query.filter_by(eventTeamId=w['a'].id).first()
    s = _account(db, ots, w, team=w['a'], link=link)
    r = _mv(client, w, s, None)
    assert r.status_code == 200
    assert 'eventLinkId' not in r.get_json()['account']
    _assert_no_secrets(r.get_json(), w)


def test_leader_without_usable_group(client, db, ots, world):
    w = world
    w['a'].otsGroupId = None
    db.session.commit()
    par = _account(db, ots, w, 'evt1_par00000', team=w['b'], parked=True)
    assert _mv(client, w, par, w['a'].id).status_code == 400
    assert not [m for m in ots.members if m[0] == 'evt1_par00000' and m[1] == 'grp_A']
    own = _device(db, w['event'], w['a'], 'evt1_own00000')
    ots.users.append(('evt1_own00000', ['user']))
    assert _mv(client, w, own, w['b'].id).status_code == 200
    assert _reload(db, own).eventTeamId == w['b'].id
    d = client.get(BASE, headers=w['h']).get_json()
    assert w['a'].id not in {t['id'] for t in d['teams']}


def test_teams_sharing_one_group_move_db_only(client, db, ots, world):
    w = world
    w['b'].otsGroupId = w['a'].otsGroupId
    db.session.commit()
    s = _account(db, ots, w, team=w['a'])
    assert _mv(client, w, s, w['b'].id).status_code == 200
    assert _reload(db, s).eventTeamId == w['b'].id
    assert ots.calls == []


def test_pull_from_parked_into_own_team(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['b'], parked=True)
    assert _mv(client, w, s, w['a'].id).status_code == 200
    s = _reload(db, s)
    assert s.parkedAt is None and s.eventTeamId == w['a'].id
    kinds = [c[0] for c in ots.calls]
    assert kinds.index('add') < kinds.index('remove')
    assert {(g, d) for u, g, d in ots.members} == {('grp_A', 'IN'), ('grp_A', 'OUT')}


def test_park_own(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['a'])
    assert _mv(client, w, s, None).status_code == 200
    assert _reload(db, s).parkedAt is not None
    assert ots.members == {('evt1_aaaaaaaa', PARKED % w['eid'], 'OUT'),
                           ('evt1_aaaaaaaa', PARKED_IN % w['eid'], 'IN')}


def test_move_own_to_other_team(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['a'])
    assert _mv(client, w, s, w['b'].id).status_code == 200
    assert _reload(db, s).eventTeamId == w['b'].id
    assert {(g, d) for u, g, d in ots.members} == {('grp_B', 'IN'), ('grp_B', 'OUT')}


def test_own_to_own_is_noop(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['a'])
    assert _mv(client, w, s, w['a'].id).status_code == 200
    assert ots.calls == []


def test_forbidden_other_to_other_and_other_to_park(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['b'])
    assert _mv(client, w, s, w['c'].id).status_code == 403
    assert _mv(client, w, s, None).status_code == 403
    assert ots.calls == [] and _reload(db, s).eventTeamId == w['b'].id


def test_forbidden_parked_to_other_team_or_park(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['b'], parked=True)
    assert _mv(client, w, s, w['c'].id).status_code == 403
    assert _mv(client, w, s, None).status_code == 403
    assert ots.calls == [] and _reload(db, s).parkedAt is not None


def test_closed_and_legacy_409(client, db, ots, world):
    w = world
    leg = _account(db, ots, w, 'evt1_leg00000', team=w['b'], mode='shared')
    clo = _account(db, ots, w, 'evt1_clo00000', team=w['b'])
    clo.closedAt = datetime.datetime.now()
    db.session.commit()
    assert _mv(client, w, leg, w['a'].id).status_code == 409
    assert _mv(client, w, clo, w['a'].id).status_code == 409
    assert ots.calls == []


def test_expired_409(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['a'])
    s.expiresAt = datetime.datetime.now() - datetime.timedelta(minutes=1)
    db.session.commit()
    assert _mv(client, w, s, w['b'].id).status_code == 409
    assert ots.calls == []


def test_other_event_session_404(client, db, ots, world):
    w = world
    ev2 = _event(db)
    t2, _ = _team_link(db, ev2, 'Z')
    s = _device(db, ev2, t2, 'evt2_zzzzzzzz')
    assert _mv(client, w, s, w['a'].id).status_code == 404
    assert _mv(client, w, s, None).status_code == 404
    assert ots.calls == []


def test_target_team_of_other_event_rejected(client, db, ots, world):
    w = world
    ev2 = _event(db)
    t2, _ = _team_link(db, ev2, 'Z')
    s = _account(db, ots, w, team=w['a'])
    assert _mv(client, w, s, t2.id).status_code == 400
    assert _mv(client, w, s, 99999).status_code == 400
    assert ots.calls == []


def test_bad_body(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['a'])
    assert client.post('%s/%d/move' % (BASE, s.id), headers=w['h'], json={}).status_code == 400
    assert _mv(client, w, s, 'x').status_code == 400
    assert _mv(client, w, s, True).status_code == 400


def test_unauthorised_callers(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['b'])
    assert client.get(BASE).status_code == 401
    assert client.post('%s/%d/move' % (BASE, s.id), json={'teamId': w['a'].id}).status_code == 401
    from flask_jwt_extended import create_access_token
    plain = {'Authorization': 'Bearer ' + create_access_token(identity=str(w['leader'].id))}
    assert client.get(BASE, headers=plain).status_code == 403
    assert _mv(client, w, s, w['a'].id, headers=plain).status_code == 403
    # a leader of another team (claims for A, identity not A's leader) is rejected
    other = _user(db, 'notleader')
    bad = _leader_headers(other, w['a'], w['event'])
    assert client.get(BASE, headers=bad).status_code == 403
    assert _mv(client, w, s, w['a'].id, headers=bad).status_code == 403
    assert ots.calls == []


def test_admin_without_claims_forbidden(client, db, ots, world):
    from tests.test_event_links import _headers
    h = _headers(db, 'mtadmin', 'events_admin')
    s = _account(db, ots, world, team=world['b'])
    assert client.get(BASE, headers=h).status_code == 403
    assert _mv(client, world, s, world['a'].id, headers=h).status_code == 403


def test_regenerated_or_deleted_leader_link_403(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['b'])
    link = EventLinkModel.query.filter_by(eventTeamId=w['a'].id, role='leader').first()
    link.tokenVersion += 1
    db.session.commit()
    assert client.get(BASE, headers=w['h']).status_code == 403
    assert _mv(client, w, s, w['a'].id).status_code == 403
    db.session.delete(link)
    db.session.commit()
    assert client.get(BASE, headers=w['h']).status_code == 403
    assert _mv(client, w, s, w['a'].id).status_code == 403
    assert ots.calls == []


def test_events_disabled_403(client, db, ots, world):
    SystemSettingsModel.set_setting('events_enabled', False)
    assert client.get(BASE, headers=world['h']).status_code == 403


def test_body_cannot_widen_scope(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['b'])
    r = client.post('%s/%d/move?teamId=%d&eventId=99' % (BASE, s.id, w['c'].id), headers=w['h'],
                    json={'teamId': w['c'].id, 'eventId': 99, 'leaderTeam': w['b'].id})
    assert r.status_code == 403
    assert _reload(db, s).eventTeamId == w['b'].id


def test_ots_failure_reverts_and_keeps_db(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['a'])
    ots.fail_remove_groups = {'grp_A'}
    r = _mv(client, w, s, w['b'].id)
    assert r.status_code == 502
    s = _reload(db, s)
    assert s.eventTeamId == w['a'].id
    assert {(g, d) for u, g, d in ots.members} >= {('grp_A', 'IN'), ('grp_A', 'OUT')}
    assert not {m for m in ots.members if m[1] == 'grp_B'}


def test_ots_add_failure_502_nothing_changed(client, db, ots, world):
    w = world
    s = _account(db, ots, w, team=w['a'])
    ots.fail_add_groups = {'grp_B'}
    assert _mv(client, w, s, w['b'].id).status_code == 502
    assert _reload(db, s).eventTeamId == w['a'].id
    assert {(g, d) for u, g, d in ots.members} == {('grp_A', 'IN'), ('grp_A', 'OUT')}
