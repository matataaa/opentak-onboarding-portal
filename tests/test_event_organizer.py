"""
Event organizer (HQ) team: its device accounts get the organizerDirection on the group of
every other team of the event, kept live and removed from granted state. Fake OTS, no network.
"""
import json

import pytest

from app.api_v1 import events as events_mod
from app.models import EventLinkModel, EventModel, EventSessionModel, EventTeamModel, OTSGroupModel

from tests.test_event_device_accounts import TOKEN, _clean, _team_link  # noqa: F401
from tests.test_event_my_team import _leader_headers, _user
from tests.test_event_device_moves import (  # noqa: F401
    API, PARKED, _groups_of, _parked_pair,
    admin, ots, reader, world)


@pytest.fixture
def hq(db, world):
    """World plus an organizer team H (link with direction BOTH) set on the event."""
    h, link_h = _team_link(db, world['event'], 'H', color='White')
    world['event'].organizerTeamId = h.id
    db.session.commit()
    yield dict(world, h=h, link_h=link_h)
    # _clean deletes teams before events: drop the FK reference first
    db.session.rollback()
    EventModel.query.update({'organizerTeamId': None})
    db.session.commit()


def _join(client, name):
    """A new device account through the player link of team <name>."""
    r = client.post('/api/v1/events/join/' + TOKEN + name)
    assert r.status_code == 200, r.get_json()
    client.delete_cookie(events_mod.DEVICE_COOKIE_NAME, domain='localhost',
                         path='/api/v1/events/join/' + TOKEN + name)
    return EventSessionModel.query.order_by(EventSessionModel.id.desc()).first()


def _put_event(client, headers, w, **body):
    return client.put('%s/%d' % (API, w['eid']), headers=headers, json=body)


def _counts(body):
    return body['reprovisioned'], body['reprovisionFailed']


def _reload(db, s):
    db.session.expire_all()
    return EventSessionModel.query.get(s.id)


def _move(client, headers, w, s, team_id):
    return client.post('%s/%d/accounts/%d/move' % (API, w['eid'], s.id), headers=headers,
                       json={'teamId': team_id})


def _close(client, headers, w, s):
    return client.post('%s/%d/accounts/%d/close' % (API, w['eid'], s.id), headers=headers)


def _never_groupless_since(ots, username, start):
    """No state after snapshot index start lacked an IN or an OUT membership."""
    states = [g for u, g in ots.snapshots[start:] if u == username]
    assert states
    for g in states:
        assert any(d == 'IN' for _, d in g) and any(d == 'OUT' for _, d in g), states


HQ_OUT = {('grp_H', 'IN'), ('grp_H', 'OUT'), ('grp_A', 'OUT'), ('grp_B', 'OUT')}


def _fail_adds_for(ots, username):
    orig = ots.add_user_to_group

    def add(user, group, direction='IN'):
        if user == username:
            raise RuntimeError('add failed')
        return orig(user, group, direction)
    ots.add_user_to_group = add
    return orig


# ---- join -----------------------------------------------------------------------
def test_hq_device_observes_every_other_team(client, db, ots, hq):
    s = _join(client, 'H')
    assert _groups_of(ots, s.otsUsername) == HQ_OUT
    assert json.loads(s.organizerGrant) == [['grp_A', 'OUT'], ['grp_B', 'OUT']]
    # a team device is unchanged and holds nothing of HQ
    a = _join(client, 'A')
    assert _groups_of(ots, a.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    assert a.organizerGrant is None


def test_hq_both_lets_teams_see_hq(client, db, ots, hq):
    hq['event'].organizerDirection = 'BOTH'
    db.session.commit()
    s = _join(client, 'H')
    assert _groups_of(ots, s.otsUsername) == {(g, d) for g in ('grp_H', 'grp_A', 'grp_B') for d in ('IN', 'OUT')}


def test_sink_rule_is_computed_over_the_full_set(client, db, ots, hq):
    hq['link_h'].direction = 'IN'
    db.session.commit()
    s = _join(client, 'H')
    # OUT on the other teams already covers the receive side: no parked sink
    assert _groups_of(ots, s.otsUsername) == {('grp_H', 'IN'), ('grp_A', 'OUT'), ('grp_B', 'OUT')}


def test_teams_without_a_usable_group_are_skipped(client, db, ots, hq):
    _team_link(db, hq['event'], 'N', group=False)
    hq['b'].otsGroup.ots_missing_since = hq['event'].validFrom
    db.session.commit()
    s = _join(client, 'H')
    assert _groups_of(ots, s.otsUsername) == {('grp_H', 'IN'), ('grp_H', 'OUT'), ('grp_A', 'OUT')}


# ---- propagation ------------------------------------------------------------------
def test_new_team_link_adds_its_group_to_hq_devices(client, db, ots, hq, admin):
    s = _join(client, 'H')
    grp = OTSGroupModel(name='grp_C')
    db.session.add(grp)
    db.session.flush()
    team = EventTeamModel(name='C', otsGroupId=grp.id)
    db.session.add(team)
    db.session.commit()
    url = '%s/%d/links' % (API, hq['eid'])
    r = client.post(url, headers=admin, json={'teamId': team.id, 'role': 'player'})
    assert r.status_code == 201 and _counts(r.get_json()) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == HQ_OUT | {('grp_C', 'OUT')}
    assert ['grp_C', 'OUT'] in json.loads(_reload(db, s).organizerGrant)
    # a second link of the same team changes nothing
    r = client.post(url, headers=admin, json={'teamId': team.id, 'role': 'leader'})
    assert _counts(r.get_json()) == (0, 0)


def test_team_leaving_removes_its_group(client, db, ots, hq, admin):
    hq['link_h'].direction = 'IN'  # receives only through the other teams' groups
    db.session.commit()
    s = _join(client, 'H')
    start = len(ots.snapshots)
    r = client.delete('%s/links/%d' % (API, hq['link_b'].id), headers=admin)
    assert r.status_code == 200 and _counts(r.get_json()) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == {('grp_H', 'IN'), ('grp_A', 'OUT')}
    _never_groupless_since(ots, s.otsUsername, start)
    # the last other team leaves: the receive side falls back to the parked sink, never to nothing
    r = client.delete('%s/links/%d' % (API, hq['link_a'].id), headers=admin)
    assert _counts(r.get_json()) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == {('grp_H', 'IN'), (PARKED % hq['eid'], 'OUT')}
    _never_groupless_since(ots, s.otsUsername, start)


def test_last_link_of_the_organizer_team_cannot_be_deleted(client, db, ots, hq, admin):
    r = client.delete('%s/links/%d' % (API, hq['link_h'].id), headers=admin)
    assert r.status_code == 409
    assert EventLinkModel.query.get(hq['link_h'].id)


def test_organizer_direction_change_is_live_and_idempotent(client, db, ots, hq, admin):
    s = _join(client, 'H')
    body = _put_event(client, admin, hq, organizerDirection='BOTH').get_json()
    assert body['organizerDirection'] == 'BOTH' and _counts(body) == (1, 0)
    assert {('grp_A', 'IN'), ('grp_B', 'IN')} <= _groups_of(ots, s.otsUsername)
    calls = len(ots.calls)
    assert _counts(_put_event(client, admin, hq, organizerDirection='BOTH').get_json()) == (0, 0)
    assert len(ots.calls) == calls
    body = _put_event(client, admin, hq, organizerDirection='IN').get_json()
    assert _counts(body) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == {('grp_H', 'IN'), ('grp_H', 'OUT'), ('grp_A', 'IN'), ('grp_B', 'IN')}


def test_clearing_and_switching_the_organizer_team(client, db, ots, hq, admin):
    h = _join(client, 'H')
    a = _join(client, 'A')
    body = _put_event(client, admin, hq, organizerTeamId=None).get_json()
    assert body['organizerTeamId'] is None and _counts(body) == (1, 0)
    assert _groups_of(ots, h.otsUsername) == {('grp_H', 'IN'), ('grp_H', 'OUT')}
    assert _reload(db, h).organizerGrant is None

    body = _put_event(client, admin, hq, organizerTeamId=hq['h'].id).get_json()
    assert _counts(body) == (1, 0)
    assert _groups_of(ots, h.otsUsername) == HQ_OUT

    # HQ H -> HQ A: H devices lose the organizer groups, A devices gain them
    body = _put_event(client, admin, hq, organizerTeamId=hq['a'].id).get_json()
    assert body['organizerTeamId'] == hq['a'].id and _counts(body) == (2, 0)
    assert _groups_of(ots, h.otsUsername) == {('grp_H', 'IN'), ('grp_H', 'OUT')}
    assert _groups_of(ots, a.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT'), ('grp_H', 'OUT'), ('grp_B', 'OUT')}


def test_failed_propagation_is_counted_and_retried(client, db, ots, hq, admin):
    ok = _join(client, 'H')
    bad = _join(client, 'H')
    orig = _fail_adds_for(ots, bad.otsUsername)
    body = _put_event(client, admin, hq, organizerDirection='BOTH').get_json()
    assert _counts(body) == (1, 1)
    assert _groups_of(ots, bad.otsUsername) == HQ_OUT  # unchanged
    assert json.loads(_reload(db, bad).organizerGrant) == [['grp_A', 'OUT'], ['grp_B', 'OUT']]
    ots.add_user_to_group = orig
    calls = len(ots.calls)
    assert _counts(_put_event(client, admin, hq, organizerDirection='BOTH').get_json()) == (1, 0)
    assert {c[1] for c in ots.calls[calls:]} == {bad.otsUsername}
    assert _groups_of(ots, bad.otsUsername) == _groups_of(ots, ok.otsUsername)


def test_close_after_failed_propagation_removes_exactly_the_granted_groups(client, db, ots, hq, admin):
    s = _join(client, 'H')
    ots.fail_remove_groups = {'grp_A'}
    body = _put_event(client, admin, hq, organizerTeamId=None).get_json()
    assert _counts(body) == (0, 1)
    ots.fail_remove_groups = set()
    assert ('grp_A', 'OUT') in _groups_of(ots, s.otsUsername)  # still sees team A
    # the config no longer says HQ, the granted state does: close removes it all
    assert _close(client, admin, hq, s).status_code == 200
    assert _groups_of(ots, s.otsUsername) == set()


def test_close_removes_organizer_memberships(client, db, ots, hq, admin):
    s = _join(client, 'H')
    assert _close(client, admin, hq, s).status_code == 200
    assert _groups_of(ots, s.otsUsername) == set()


# ---- moves and parking -------------------------------------------------------------
def test_admin_moves_a_device_into_and_out_of_hq(client, db, ots, hq, admin):
    s = _join(client, 'A')
    start = len(ots.snapshots)
    assert _move(client, admin, hq, s, hq['h'].id).status_code == 200
    assert _groups_of(ots, s.otsUsername) == HQ_OUT
    assert _reload(db, s).organizerGrant is not None
    assert _move(client, admin, hq, s, hq['b'].id).status_code == 200
    assert _groups_of(ots, s.otsUsername) == {('grp_B', 'IN'), ('grp_B', 'OUT')}
    assert _reload(db, s).organizerGrant is None
    _never_groupless_since(ots, s.otsUsername, start)


def test_park_and_unpark_of_an_hq_device(client, db, ots, hq, admin):
    s = _join(client, 'H')
    assert _move(client, admin, hq, s, None).status_code == 200
    assert _groups_of(ots, s.otsUsername) == _parked_pair(hq['eid'])
    assert _reload(db, s).organizerGrant is None
    assert _move(client, admin, hq, s, hq['h'].id).status_code == 200
    assert _groups_of(ots, s.otsUsername) == HQ_OUT


@pytest.fixture
def leader_a(db, hq):
    user = _user(db, 'orgleader')
    hq['a'].leaderUserId = user.id
    db.session.commit()
    return _leader_headers(user, hq['a'], hq['event'])


def test_leader_cannot_move_into_hq(client, db, ots, hq, leader_a):
    s = _join(client, 'A')
    body = client.get('%s/my-team/accounts' % API, headers=leader_a).get_json()
    assert hq['h'].id not in {t['id'] for t in body['teams']}
    r = client.post('%s/my-team/accounts/%d/move' % (API, s.id), headers=leader_a, json={'teamId': hq['h'].id})
    assert r.status_code == 400
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    # moving to a normal team still works
    r = client.post('%s/my-team/accounts/%d/move' % (API, s.id), headers=leader_a, json={'teamId': hq['b'].id})
    assert r.status_code == 200


# ---- teams --------------------------------------------------------------------------
def test_team_group_change_reprovisions_hq_devices(client, db, ots, hq, admin):
    s = _join(client, 'H')
    new = OTSGroupModel(name='grp_C')
    db.session.add(new)
    db.session.commit()
    body = client.put('%s/teams/%d' % (API, hq['b'].id), headers=admin, json={'otsGroupId': new.id}).get_json()
    assert _counts(body) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == HQ_OUT - {('grp_B', 'OUT')} | {('grp_C', 'OUT')}


def test_team_delete(client, db, ots, hq, admin):
    r = client.delete('%s/teams/%d' % (API, hq['h'].id), headers=admin)
    assert r.status_code == 409 and 'organizer' in r.get_json()['error']
    assert EventTeamModel.query.get(hq['h'].id)
    s = _join(client, 'H')
    r = client.delete('%s/teams/%d' % (API, hq['b'].id), headers=admin)
    assert r.status_code == 200 and _counts(r.get_json()) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == HQ_OUT - {('grp_B', 'OUT')}


# ---- validation ---------------------------------------------------------------------
def test_event_organizer_validation(client, db, ots, hq, admin, reader):
    assert _put_event(client, reader, hq, organizerDirection='BOTH').status_code == 403
    for bad in ('X', 'out', None, 1):
        assert _put_event(client, admin, hq, organizerDirection=bad).status_code == 400
    no_group, _ = _team_link(db, hq['event'], 'N', group=False)
    outsider = EventTeamModel(name='O')
    db.session.add(outsider)
    db.session.commit()
    for bad in ('1', True, 999999, no_group.id, outsider.id):
        assert _put_event(client, admin, hq, organizerTeamId=bad).status_code == 400
    event = EventModel.query.get(hq['eid'])
    assert (event.organizerTeamId, event.organizerDirection) == (hq['h'].id, 'OUT')
    assert ots.calls == []

    r = client.post(API, headers=admin, json={'name': 'n', 'organizerDirection': 'X'})
    assert r.status_code == 400
    r = client.post(API, headers=admin, json={'name': 'n', 'organizerTeamId': hq['h'].id})
    assert r.status_code == 400
    r = client.post(API, headers=admin, json={'name': 'n', 'organizerDirection': 'BOTH'})
    assert r.status_code == 201
    assert (r.get_json()['organizerTeamId'], r.get_json()['organizerDirection']) == (None, 'BOTH')
    got = client.get('%s/%d' % (API, hq['eid']), headers=reader).get_json()
    assert (got['organizerTeamId'], got['organizerDirection']) == (hq['h'].id, 'OUT')


# ---- self-healing (periodic event job) ------------------------------------------------
def _run_job(app, monkeypatch):
    from app import jobs
    from app.extensions import scheduler
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    jobs.close_expired_event_sessions()


def test_job_heals_a_failed_demotion(app, client, db, ots, hq, admin, monkeypatch):
    s = _join(client, 'H')
    ots.fail_remove_groups = {'grp_A'}
    assert _counts(_put_event(client, admin, hq, organizerTeamId=None).get_json()) == (0, 1)
    assert ('grp_A', 'OUT') in _groups_of(ots, s.otsUsername)
    ots.fail_remove_groups = set()
    _run_job(app, monkeypatch)
    assert _groups_of(ots, s.otsUsername) == {('grp_H', 'IN'), ('grp_H', 'OUT')}
    assert _reload(db, s).organizerGrant is None


def test_job_makes_no_ots_calls_when_in_sync(app, client, db, ots, hq, monkeypatch):
    _join(client, 'H')
    _join(client, 'A')
    calls = len(ots.calls)
    _run_job(app, monkeypatch)
    assert len(ots.calls) == calls


def test_unreadable_organizer_grant_is_treated_as_empty(client, db, ots, hq, admin):
    from app.api_v1.events import session_group_memberships
    s = _join(client, 'A')
    for bad in ('not json', '42', '[["grp_X"]]'):
        s.organizerGrant = bad
        db.session.commit()
        assert session_group_memberships(s) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    assert _close(client, admin, hq, s).status_code == 200
    assert _groups_of(ots, s.otsUsername) == set()
