"""
Per-link group direction of device accounts (BOTH / IN send only / OUT receive only),
the parked sink groups, fail-closed join and live re-provisioning. Fake OTS, no network.
"""
import datetime

import pytest

from app.models import EventLinkModel, EventSessionModel, EventTeamModel, OTSGroupModel

from tests.test_event_device_accounts import TOKEN, _clean, _event, _team_link  # noqa: F401
from tests.test_event_my_team import _leader_headers, _leader_link, _user
from tests.test_event_device_moves import (  # noqa: F401
    API, PARKED, PARKED_IN, _account, _groups_of, _never_groupless, _parked_pair,
    admin, ots, reader, world)


def _join(client, name='A'):
    return client.post('/api/v1/events/join/' + TOKEN + name)


def _set_direction(db, link, direction):
    link.direction = direction
    db.session.commit()


def _put(client, headers, link, direction):
    return client.put('%s/links/%d' % (API, link.id), headers=headers, json={'direction': direction})


def _account_dir(db, ots, w, username, direction, link=None, team=None):
    """An open device account holding the memberships of its link direction."""
    link = link or w['link_a']
    team = team or w['a']
    s = _account(db, ots, w, username, team=team, link=link)
    g = team.otsGroup.name
    ots.members -= {(username, g, 'IN'), (username, g, 'OUT')}
    eid = w['eid']
    held = {'BOTH': {(g, 'IN'), (g, 'OUT')},
            'IN': {(g, 'IN'), (PARKED % eid, 'OUT')},
            'OUT': {(g, 'OUT'), (PARKED_IN % eid, 'IN')}}[direction]
    ots.members |= {(username, grp, d) for grp, d in held}
    if direction != 'BOTH':
        ots.groups |= {PARKED % eid, PARKED_IN % eid}
    s.direction, s.otsGroupId = direction, team.otsGroupId  # granted state
    db.session.commit()
    return s


def _get(db, model, obj):
    db.session.expire_all()
    return model.query.get(obj.id)


def _only_session():
    sessions = EventSessionModel.query.filter_by(mode='device').all()
    assert len(sessions) == 1
    return sessions[0]


# ---- membership computation at join ----------------------------------------
def test_default_link_is_both_and_join_unchanged(client, db, ots, world):
    assert world['link_a'].direction == 'BOTH'
    r = _join(client)
    assert r.status_code == 200
    u = _only_session().otsUsername
    assert _groups_of(ots, u) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    assert ots.group_creates == 0  # no sink groups needed


def test_in_link_sends_to_team_and_receives_from_sink(client, db, ots, world):
    _set_direction(db, world['link_a'], 'IN')
    assert _join(client).status_code == 200
    u = _only_session().otsUsername
    assert _groups_of(ots, u) == {('grp_A', 'IN'), (PARKED % world['eid'], 'OUT')}
    assert {PARKED % world['eid'], PARKED_IN % world['eid']} <= ots.groups


def test_out_link_receives_team_and_sends_to_sink(client, db, ots, world):
    _set_direction(db, world['link_a'], 'OUT')
    assert _join(client).status_code == 200
    u = _only_session().otsUsername
    assert _groups_of(ots, u) == {('grp_A', 'OUT'), (PARKED_IN % world['eid'], 'IN')}


@pytest.mark.parametrize('direction', ['BOTH', 'IN'])
def test_join_fails_closed_when_a_group_add_fails(client, db, ots, world, direction):
    _set_direction(db, world['link_a'], direction)
    ots.fail_add_groups = {'grp_A'}
    r = _join(client)
    assert r.status_code == 500 and 'error' in r.get_json()
    username = ots.users[0][0]
    assert ots.removed_users == [username]  # account discarded
    assert EventSessionModel.query.filter_by(mode='device').count() == 0
    assert _groups_of(ots, username) == set()
    assert not ots.qr_calls


def test_join_fails_closed_when_sink_groups_cannot_be_created(client, db, ots, world):
    _set_direction(db, world['link_a'], 'OUT')
    ots.fail_create_group = True
    r = _join(client)
    assert r.status_code == 500
    assert ots.removed_users == [ots.users[0][0]]
    assert EventSessionModel.query.filter_by(mode='device').count() == 0


# ---- link API ------------------------------------------------------------------
def test_create_link_direction(client, db, ots, world, admin):
    url = '%s/%d/links' % (API, world['eid'])
    team = EventTeamModel(name='C')
    db.session.add(team)
    db.session.commit()
    r = client.post(url, headers=admin, json={'teamId': team.id, 'role': 'player', 'direction': 'X'})
    assert r.status_code == 400
    r = client.post(url, headers=admin, json={'teamId': team.id, 'role': 'player'})
    assert r.status_code == 201 and r.get_json()['direction'] == 'BOTH'
    # stored for leader links too (used for leader devices later)
    r = client.post(url, headers=admin, json={'teamId': team.id, 'role': 'leader', 'direction': 'OUT'})
    assert r.status_code == 201 and r.get_json()['direction'] == 'OUT'
    listed = client.get(url, headers=admin).get_json()['links']
    assert {(l['role'], l['eventTeamId']): l['direction'] for l in listed}[('leader', team.id)] == 'OUT'


def test_put_link_validation_and_rbac(client, db, ots, world, admin, reader):
    link = world['link_a']
    assert _put(client, reader, link, 'IN').status_code == 403
    for bad in ('in', 'X', None, 1):
        assert _put(client, admin, link, bad).status_code == 400
    assert client.put('%s/links/%d' % (API, link.id), headers=admin, json={}).status_code == 400
    assert _put(client, admin, type('L', (), {'id': 999999})(), 'IN').status_code == 404
    assert _get(db, EventLinkModel, link).direction == 'BOTH'


def test_put_link_reprovisions_open_sessions(client, db, ots, world, admin):
    link = world['link_a']
    s1 = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    s2 = _account_dir(db, ots, world, 'evt1_bbbbbbbb', 'BOTH')
    moved = _account_dir(db, ots, world, 'evt1_dddddddd', 'BOTH', team=world['b'])  # moved to B
    parked = _account(db, ots, world, 'evt1_pppppppp', parked=True, link=link)
    closed = _account(db, ots, world, 'evt1_cccccccc', link=link)
    closed.closedAt = datetime.datetime.now()
    db.session.commit()
    other = _account(db, ots, world, 'evt1_oooooooo', link=world['link_b'], team=world['b'])

    r = _put(client, admin, link, 'IN')
    assert r.status_code == 200
    body = r.get_json()
    assert body['direction'] == 'IN' and body['reprovisioned'] == 3 and body['reprovisionFailed'] == 0
    eid = world['eid']
    for s in (s1, s2):
        assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), (PARKED % eid, 'OUT')}
        _never_groupless(ots, s.otsUsername)
    assert _groups_of(ots, moved.otsUsername) == {('grp_B', 'IN'), (PARKED % eid, 'OUT')}
    assert _groups_of(ots, parked.otsUsername) == _parked_pair(eid)
    assert _groups_of(ots, closed.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    assert _groups_of(ots, other.otsUsername) == {('grp_B', 'IN'), ('grp_B', 'OUT')}
    assert _get(db, EventLinkModel, link).direction == 'IN'
    assert (_get(db, EventSessionModel, s1).direction, _get(db, EventSessionModel, closed).direction) == ('IN', None)

    # IN -> OUT moves both sides; same direction again is a no-op
    body = _put(client, admin, link, 'OUT').get_json()
    assert body['reprovisioned'] == 3
    assert _groups_of(ots, s1.otsUsername) == {('grp_A', 'OUT'), (PARKED_IN % eid, 'IN')}
    _never_groupless(ots, s1.otsUsername)
    calls = len(ots.calls)
    body = _put(client, admin, link, 'OUT').get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (0, 0) and len(ots.calls) == calls


def test_put_link_counts_a_failing_session_and_still_saves(client, db, ots, world, admin):
    ok = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    bad = _account_dir(db, ots, world, 'evt1_bbbbbbbb', 'BOTH')
    orig = ots.add_user_to_group

    def add(username, group, direction='IN'):
        if username == bad.otsUsername:
            raise RuntimeError('add failed')
        return orig(username, group, direction)
    ots.add_user_to_group = add

    r = _put(client, admin, world['link_a'], 'OUT')
    assert r.status_code == 200
    body = r.get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (1, 1)
    assert _get(db, EventLinkModel, world['link_a']).direction == 'OUT'
    assert _groups_of(ots, ok.otsUsername) == {('grp_A', 'OUT'), (PARKED_IN % world['eid'], 'IN')}
    assert _groups_of(ots, bad.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}  # unchanged
    assert _get(db, EventSessionModel, bad).direction == 'BOTH'  # granted state unchanged

    # saving the same direction again retries only the failed account
    ots.add_user_to_group = orig
    calls = len(ots.calls)
    body = _put(client, admin, world['link_a'], 'OUT').get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (1, 0)
    assert {c[1] for c in ots.calls[calls:]} == {bad.otsUsername}
    assert _groups_of(ots, bad.otsUsername) == {('grp_A', 'OUT'), (PARKED_IN % world['eid'], 'IN')}


def test_failed_reprovision_then_move_leaves_nothing_behind(client, db, ots, world, admin):
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    ots.fail_add_groups = {PARKED % world['eid']}
    body = _put(client, admin, world['link_a'], 'IN').get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (0, 1)
    ots.fail_add_groups = set()
    move = '%s/%d/accounts/%d/move' % (API, world['eid'], s.id)
    assert client.post(move, headers=admin, json={'teamId': world['b'].id}).status_code == 200
    # source was the granted BOTH in A, so (A, OUT) is removed as well
    assert _groups_of(ots, s.otsUsername) == {('grp_B', 'IN'), (PARKED % world['eid'], 'OUT')}
    s = _get(db, EventSessionModel, s)
    assert (s.direction, s.otsGroupId) == ('IN', world['b'].otsGroupId)


def test_expired_sessions_are_left_to_the_close_job(client, db, ots, world, admin):
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    s.expiresAt = datetime.datetime.now() - datetime.timedelta(minutes=1)
    db.session.commit()
    body = _put(client, admin, world['link_a'], 'IN').get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (0, 0)
    assert ots.calls == []


def test_put_link_sink_creation_failure_is_reported_not_500(client, db, ots, world, admin):
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    ots.fail_create_group = True
    r = _put(client, admin, world['link_a'], 'IN')
    assert r.status_code == 200
    assert (r.get_json()['reprovisioned'], r.get_json()['reprovisionFailed']) == (0, 1)
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}


# ---- close, move, park ----------------------------------------------------------
@pytest.mark.parametrize('direction', ['IN', 'OUT'])
def test_close_removes_team_and_sink_memberships(client, db, ots, world, admin, direction):
    _set_direction(db, world['link_a'], direction)
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', direction)
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 200
    assert _groups_of(ots, s.otsUsername) == set()
    assert sorted(ots.deleted_groups) == sorted([PARKED % world['eid'], PARKED_IN % world['eid']])


def test_move_park_unpark_keep_link_direction(client, db, ots, world, admin):
    _set_direction(db, world['link_a'], 'IN')
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'IN')
    eid, u = world['eid'], s.otsUsername
    move = '%s/%d/accounts/%d/move' % (API, eid, s.id)

    assert client.post(move, headers=admin, json={'teamId': world['b'].id}).status_code == 200
    assert _groups_of(ots, u) == {('grp_B', 'IN'), (PARKED % eid, 'OUT')}
    assert client.post(move, headers=admin, json={'teamId': None}).status_code == 200
    assert _groups_of(ots, u) == _parked_pair(eid)
    assert client.post(move, headers=admin, json={'teamId': world['a'].id}).status_code == 200
    assert _groups_of(ots, u) == {('grp_A', 'IN'), (PARKED % eid, 'OUT')}
    _never_groupless(ots, u)


def test_park_unpark_of_both_link_unchanged(client, db, ots, world, admin):
    s = _account(db, ots, world, 'evt1_aaaaaaaa', link=world['link_a'])
    eid, u = world['eid'], s.otsUsername
    move = '%s/%d/accounts/%d/move' % (API, eid, s.id)
    assert client.post(move, headers=admin, json={'teamId': None}).status_code == 200
    assert _groups_of(ots, u) == _parked_pair(eid)
    assert client.post(move, headers=admin, json={'teamId': world['a'].id}).status_code == 200
    assert _groups_of(ots, u) == {('grp_A', 'IN'), ('grp_A', 'OUT')}


def test_close_after_link_delete_uses_granted_state(client, db, ots, world, admin):
    _set_direction(db, world['link_a'], 'IN')
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'IN')
    assert client.delete('%s/links/%d' % (API, world['link_a'].id), headers=admin).status_code == 200
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 200
    assert _groups_of(ots, s.otsUsername) == set()


def test_close_ignores_a_stale_missing_flag(client, db, ots, world, admin):
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'OUT')
    world['a'].otsGroup.ots_missing_since = datetime.datetime(2026, 1, 1)
    db.session.commit()
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 200
    assert _groups_of(ots, s.otsUsername) == set()


# ---- team group change ------------------------------------------------------------
def test_team_group_change_moves_open_sessions(client, db, ots, world, admin):
    _set_direction(db, world['link_a'], 'IN')
    both = _account(db, ots, world, 'evt1_aaaaaaaa')  # no link: BOTH
    one_way = _account_dir(db, ots, world, 'evt1_bbbbbbbb', 'IN')
    parked = _account(db, ots, world, 'evt1_pppppppp', parked=True)
    # an open session of the same team in another event moves too
    ev2 = _event(db)
    s_ev2 = EventSessionModel(eventId=ev2.id, eventTeamId=world['a'].id, otsUsername='evt9_zzzzzzzz',
                              mode='device', expiresAt=ev2.validUntil)
    db.session.add(s_ev2)
    db.session.commit()
    ots.members |= {('evt9_zzzzzzzz', 'grp_A', 'IN'), ('evt9_zzzzzzzz', 'grp_A', 'OUT')}
    new = OTSGroupModel(name='grp_C')
    db.session.add(new)
    db.session.commit()

    r = client.put('%s/teams/%d' % (API, world['a'].id), headers=admin, json={'otsGroupId': new.id})
    assert r.status_code == 200
    body = r.get_json()
    assert body['otsGroupId'] == new.id
    assert (body['reprovisioned'], body['reprovisionFailed']) == (3, 0)
    eid = world['eid']
    assert _groups_of(ots, both.otsUsername) == {('grp_C', 'IN'), ('grp_C', 'OUT')}
    assert _groups_of(ots, one_way.otsUsername) == {('grp_C', 'IN'), (PARKED % eid, 'OUT')}
    assert _groups_of(ots, 'evt9_zzzzzzzz') == {('grp_C', 'IN'), ('grp_C', 'OUT')}
    assert _groups_of(ots, parked.otsUsername) == _parked_pair(eid)
    _never_groupless(ots, both.otsUsername)
    assert _get(db, EventSessionModel, both).otsGroupId == new.id


def test_failed_team_group_change_is_retried_and_close_removes_old_group(client, db, ots, world, admin):
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    new = OTSGroupModel(name='grp_C')
    db.session.add(new)
    db.session.commit()
    ots.fail_add_groups = {'grp_C'}
    url = '%s/teams/%d' % (API, world['a'].id)
    body = client.put(url, headers=admin, json={'otsGroupId': new.id}).get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (0, 1)
    s = _get(db, EventSessionModel, s)
    assert s.otsGroupId != new.id  # granted group unchanged
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    # close removes the groups it really holds (grp_A), not the team's new one
    calls = len(ots.calls)
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 200
    assert _groups_of(ots, s.otsUsername) == set()
    assert {c[2] for c in ots.calls[calls:]} == {'grp_A'}


def test_team_group_change_retry_by_submitting_same_group(client, db, ots, world, admin):
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    new = OTSGroupModel(name='grp_C')
    db.session.add(new)
    db.session.commit()
    url = '%s/teams/%d' % (API, world['a'].id)
    ots.fail_add_groups = {'grp_C'}
    assert client.put(url, headers=admin, json={'otsGroupId': new.id}).get_json()['reprovisionFailed'] == 1
    ots.fail_add_groups = set()
    body = client.put(url, headers=admin, json={'otsGroupId': new.id}).get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == {('grp_C', 'IN'), ('grp_C', 'OUT')}


def test_team_group_cannot_be_cleared_with_open_device_accounts(client, db, ots, world, admin):
    _account(db, ots, world, 'evt1_aaaaaaaa')
    r = client.put('%s/teams/%d' % (API, world['a'].id), headers=admin, json={'otsGroupId': None})
    assert r.status_code == 409
    assert _get(db, EventTeamModel, world['a']).otsGroupId is not None
    assert ots.calls == []


def test_team_edit_without_group_change_does_not_touch_ots(client, db, ots, world, admin):
    _account(db, ots, world, 'evt1_aaaaaaaa')
    url = '%s/teams/%d' % (API, world['a'].id)
    r = client.put(url, headers=admin, json={'otsGroupId': world['a'].otsGroupId, 'color': 'Blue'})
    assert r.status_code == 200
    assert (r.get_json()['reprovisioned'], r.get_json()['reprovisionFailed']) == (0, 0)
    r = client.put(url, headers=admin, json={'color': 'Red'})
    assert r.status_code == 200 and 'reprovisioned' not in r.get_json()
    assert ots.calls == []


# ---- leader devices ------------------------------------------------------------------
@pytest.fixture
def leader(db, world):
    user = _user(db, 'dirleader')
    world['a'].leaderUserId = user.id
    db.session.commit()
    link = _leader_link(world['a'], world['event'])
    return dict(user=user, link=link, headers=_leader_headers(user, world['a'], world['event']))


def _enroll(client, leader, **body):
    return client.post('%s/my-team/enrollment' % API, headers=leader['headers'], json=body)


def test_leader_own_device_uses_leader_link_direction(client, db, ots, world, admin, leader):
    _set_direction(db, leader['link'], 'OUT')
    _set_direction(db, world['link_a'], 'IN')
    assert _enroll(client, leader, forLeader=True).status_code == 200
    s = _only_session()
    assert s.role == 'leader' and s.eventLinkId == leader['link'].id
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'OUT'), (PARKED_IN % world['eid'], 'IN')}
    assert (s.direction, s.otsGroupId) == ('OUT', world['a'].otsGroupId)

    # a player-link PUT leaves the leader device alone; a leader-link PUT re-provisions it
    assert _put(client, admin, world['link_a'], 'BOTH').get_json()['reprovisioned'] == 0
    body = _put(client, admin, leader['link'], 'BOTH').get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}


def test_leader_enrollment_for_a_player_uses_player_link(client, db, ots, world, leader):
    _set_direction(db, world['link_a'], 'IN')
    assert _enroll(client, leader).status_code == 200
    s = _only_session()
    assert s.role is None and s.eventLinkId == world['link_a'].id
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), (PARKED % world['eid'], 'OUT')}


# ---- vanished groups, legacy state, deleted links ---------------------------------
def test_ots_status_code_reads_the_404_of_a_missing_group():
    from app.api_v1.groups import _ots_status_code
    # shape raised by OTSClient.request_handler for DELETE /api/groups/members on an unknown group
    e = Exception({'response': {'success': False, 'error': 'Group grp_X not found'}, 'status_code': 404})
    assert _ots_status_code(e) == 404


def _new_group(db, name='grp_C'):
    group = OTSGroupModel(name=name)
    db.session.add(group)
    db.session.commit()
    return group


def test_vanished_granted_group_does_not_block_team_change(client, db, ots, world, admin):
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    ots.remove_status = {'grp_A': 404}  # grp_A was deleted in OTS
    new = _new_group(db)
    body = client.put('%s/teams/%d' % (API, world['a'].id), headers=admin, json={'otsGroupId': new.id}).get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (1, 0)
    assert {('grp_C', 'IN'), ('grp_C', 'OUT')} <= _groups_of(ots, s.otsUsername)
    assert _get(db, EventSessionModel, s).otsGroupId == new.id


def test_vanished_granted_group_does_not_block_move(client, db, ots, world, admin):
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'BOTH')
    ots.remove_status = {'grp_A': 404}
    r = client.post('%s/%d/accounts/%d/move' % (API, world['eid'], s.id), headers=admin,
                    json={'teamId': world['b'].id})
    assert r.status_code == 200
    assert {('grp_B', 'IN'), ('grp_B', 'OUT')} <= _groups_of(ots, s.otsUsername)
    s = _get(db, EventSessionModel, s)
    assert (s.eventTeamId, s.otsGroupId) == (world['b'].id, world['b'].otsGroupId)


def test_direction_without_group_means_granted_nothing(db, world):
    from app.api_v1.events import session_group_memberships
    s = EventSessionModel(eventId=world['eid'], eventTeamId=world['a'].id, otsUsername='evt1_nnnnnnnn',
                          mode='device', direction='IN')
    db.session.add(s)
    db.session.commit()
    assert session_group_memberships(s) == set()
    s.direction = None  # legacy: the team's current group, BOTH
    assert session_group_memberships(s) == {('grp_A', 'IN'), ('grp_A', 'OUT')}


def test_groupless_team_getting_a_group_adds_it_to_legacy_devices(client, db, ots, world, admin):
    s = _account(db, ots, world, 'evt1_aaaaaaaa')  # legacy: direction and otsGroupId NULL
    ots.members.clear()  # joined while the team had no group: holds nothing
    world['a'].otsGroupId = None
    db.session.commit()
    new = _new_group(db)
    body = client.put('%s/teams/%d' % (API, world['a'].id), headers=admin, json={'otsGroupId': new.id}).get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == {('grp_C', 'IN'), ('grp_C', 'OUT')}
    s = _get(db, EventSessionModel, s)
    assert (s.direction, s.otsGroupId) == ('BOTH', new.id)


def test_deleted_one_way_link_keeps_device_one_way(client, db, ots, world, admin):
    _set_direction(db, world['link_a'], 'IN')
    s = _account_dir(db, ots, world, 'evt1_aaaaaaaa', 'IN')
    assert client.delete('%s/links/%d' % (API, world['link_a'].id), headers=admin).status_code == 200
    new = _new_group(db)
    body = client.put('%s/teams/%d' % (API, world['a'].id), headers=admin, json={'otsGroupId': new.id}).get_json()
    assert (body['reprovisioned'], body['reprovisionFailed']) == (1, 0)
    assert _groups_of(ots, s.otsUsername) == {('grp_C', 'IN'), (PARKED % world['eid'], 'OUT')}


def test_account_lists_expose_leader_role(client, db, ots, world, admin, leader):
    assert _enroll(client, leader, forLeader=True).status_code == 200
    accounts = client.get('%s/%d/accounts' % (API, world['eid']), headers=admin).get_json()['accounts']
    assert [a['role'] for a in accounts] == ['leader']
    mine = client.get('%s/my-team/accounts' % API, headers=leader['headers']).get_json()['accounts']
    assert [a['role'] for a in mine] == ['leader']
