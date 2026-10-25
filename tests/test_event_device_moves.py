"""
Admin side of one OTS account per device: list, move/park, close. Fake OTS, no network.
"""
import datetime
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from app import jobs
from app.api_v1 import events as events_mod
from app.extensions import scheduler
from app.models import (EventLinkModel, EventModel, EventSessionModel, OTSGroupModel,
                        SystemSettingsModel)
from app.services import event_team_colors as etc

from tests.test_event_device_accounts import (  # noqa: F401
    FakeOTS as BaseFakeOTS, _clean, _device, _event, _team_link)
from tests.test_event_links import _headers
from tests.test_event_my_team import _leader_headers, _user
from tests.test_event_team_colors import eud

API = '/api/v1/events'
PARKED = 'evt%d_parked'
PARKED_IN = 'evt%d_parked_in'


class FakeOTS(BaseFakeOTS):
    """Adds group membership state, group management and failure injection."""

    def __init__(self):
        super().__init__()
        self.members = set()      # (username, group, direction)
        self.calls = []           # ordered ('add'|'remove', username, group, direction)
        self.groups = set()
        self.fail_add_groups = set()
        self.fail_remove_groups = set()
        self.raise_after_remove = set()   # groups where remove changes state, then errors (like OTS)
        self.remove_status = {}           # group -> HTTP status raised by remove (state unchanged)
        self.snapshots = []               # (username, groups of that user) after every change
        self.fail_create_group = False
        self.deleted_groups = []
        self.group_creates = 0

    def add_user_to_group(self, username, group, direction='IN'):
        if group in self.fail_add_groups:
            raise RuntimeError('add failed')
        self.calls.append(('add', username, group, direction))
        self.members.add((username, group, direction))
        self._snap(username)

    def remove_user_from_group(self, username, group, direction='IN'):
        if group in self.fail_remove_groups:
            raise RuntimeError('remove failed')
        if group in self.remove_status:
            raise Exception({'response': {}, 'status_code': self.remove_status[group]})
        self.calls.append(('remove', username, group, direction))
        self.members.discard((username, group, direction))
        self._snap(username)
        if group in self.raise_after_remove:
            raise RuntimeError('remove committed, then failed')

    def _snap(self, username):
        self.snapshots.append((username, {(g, d) for u, g, d in self.members if u == username}))

    def create_group(self, name, description=None):
        self.group_creates += 1
        if self.fail_create_group:
            raise RuntimeError('create failed')
        if name in self.groups:
            raise Exception({'response': {}, 'status_code': 400})
        self.groups.add(name)

    def get_groups(self, page=None, per_page=None, name=None, active=None, sort_by=None):
        return {'response': {'results': [{'name': g} for g in sorted(self.groups)
                                         if name is None or g == name], 'total_pages': 1}}

    def delete_group(self, group_name):
        if group_name not in self.groups:
            raise Exception({'response': {}, 'status_code': 404})
        self.groups.discard(group_name)
        self.deleted_groups.append(group_name)


@pytest.fixture
def ots(monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(events_mod, 'otsClient', fake)
    monkeypatch.setattr(etc, 'otsClient', fake)
    monkeypatch.setattr(jobs, 'otsClient', fake)
    return fake


@pytest.fixture
def admin(db):
    return _headers(db, 'mvadmin', 'events_admin')


@pytest.fixture
def reader(db):
    return _headers(db, 'mvreader', 'events_readonly')


@pytest.fixture
def world(db):
    SystemSettingsModel.set_setting('events_enabled', True)
    event = _event(db)
    a, link_a = _team_link(db, event, 'A', color='Red')
    b, link_b = _team_link(db, event, 'B', color='Blue')
    return dict(event=event, a=a, b=b, link_a=link_a, link_b=link_b, eid=event.id)


def _account(db, ots, w, username='evt1_aaaaaaaa', team=None, parked=False, mode='device', link=None):
    team = team or w['a']
    s = _device(db, w['event'], team, username, mode=mode, parked=parked)
    if link:
        s.eventLinkId = link.id
        db.session.commit()
    ots.users.append((username, ['user']))
    if mode == 'device':
        if parked:
            ots.members.update({(username, PARKED % w['eid'], 'OUT'),
                                (username, PARKED_IN % w['eid'], 'IN')})
            ots.groups.update({PARKED % w['eid'], PARKED_IN % w['eid']})
        else:
            g = team.otsGroup.name
            ots.members.update({(username, g, 'IN'), (username, g, 'OUT')})
    return s


def _move(client, headers, w, s, team_id):
    return client.post('%s/%d/accounts/%d/move' % (API, w['eid'], s.id), headers=headers,
                       json={'teamId': team_id})


def _reload(db, s):
    db.session.expire_all()
    return EventSessionModel.query.get(s.id)


def _groups_of(ots, username):
    return {(g, d) for u, g, d in ots.members if u == username}


def _parked_pair(eid):
    return {(PARKED % eid, 'OUT'), (PARKED_IN % eid, 'IN')}


def _never_groupless(ots, username):
    """No intermediate state of the account lacked an IN or an OUT membership."""
    states = [g for u, g in ots.snapshots if u == username]
    assert states
    for g in states:
        assert any(d == 'IN' for _, d in g) and any(d == 'OUT' for _, d in g), states


# ---- list -------------------------------------------------------------------
def test_list_accounts_joins_euds_once_and_hides_secrets(client, db, ots, world, admin):
    s1 = _account(db, ots, world, 'evt1_aaaaaaaa')
    _account(db, ots, world, 'evt1_bbbbbbbb', team=world['b'], parked=True)
    _account(db, ots, world, 'old_shared', mode='shared')
    closed = _account(db, ots, world, 'evt1_cccccccc')
    closed.closedAt = datetime.datetime.now()
    db.session.commit()
    ots.euds = [dict(eud('u1', 'evt1_aaaaaaaa'), callsign='ALPHA', last_event_time='2026-01-01T10:00:00'),
                dict(eud('u2', 'evt1_bbbbbbbb'), callsign='BRAVO'),
                dict(eud('u3', 'someone_else'), callsign='NOPE')]
    calls = []
    orig = ots.get_euds
    ots.get_euds = lambda **kw: (calls.append(kw), orig(**kw))[1]
    r = client.get('%s/%d/accounts' % (API, world['eid']), headers=admin)
    assert r.status_code == 200
    assert len(calls) == 1 and 'username' not in calls[0]
    accounts = {a['otsUsername']: a for a in r.get_json()['accounts']}
    assert set(accounts) == {'evt1_aaaaaaaa', 'evt1_bbbbbbbb', 'old_shared'}
    a = accounts['evt1_aaaaaaaa']
    assert a['id'] == s1.id and a['parked'] is False and a['legacy'] is False
    assert a['team'] == {'id': world['a'].id, 'name': 'A', 'color': 'Red'}
    assert a['euds'] == [{'callsign': 'ALPHA', 'uid': 'u1', 'lastSeen': '2026-01-01T10:00:00'}]
    assert accounts['evt1_bbbbbbbb']['parked'] is True
    assert accounts['old_shared']['legacy'] is True
    assert 'NOPE' not in r.get_data(as_text=True)
    text = r.get_data(as_text=True).lower()
    assert 'password' not in text and 'token' not in text and 'qrstring' not in text


def test_list_survives_eud_failure(client, db, ots, world, admin):
    _account(db, ots, world)
    ots.fail_euds = True
    body = client.get('%s/%d/accounts' % (API, world['eid']), headers=admin).get_json()
    assert body['eudsAvailable'] is False and body['accounts'][0]['euds'] == []


# ---- move -------------------------------------------------------------------
def test_move_team_a_to_b_adds_before_removes(client, db, ots, world, admin):
    s = _account(db, ots, world)
    r = _move(client, admin, world, s, world['b'].id)
    assert r.status_code == 200
    kinds = [c[0] for c in ots.calls]
    assert kinds == ['add', 'add', 'remove', 'remove']
    _never_groupless(ots, s.otsUsername)
    assert _groups_of(ots, s.otsUsername) == {('grp_B', 'IN'), ('grp_B', 'OUT')}
    s = _reload(db, s)
    assert s.eventTeamId == world['b'].id and s.parkedAt is None
    assert r.get_json()['account']['team']['id'] == world['b'].id


def test_park_creates_group_pair_lazily(client, db, ots, world, admin):
    s = _account(db, ots, world)
    assert ots.group_creates == 0
    r = _move(client, admin, world, s, None)
    assert r.status_code == 200
    parked = PARKED % world['eid']
    assert {parked, PARKED_IN % world['eid']} <= ots.groups
    assert _groups_of(ots, s.otsUsername) == _parked_pair(world['eid'])
    assert [c[0] for c in ots.calls] == ['add', 'add', 'remove', 'remove']
    _never_groupless(ots, s.otsUsername)
    s = _reload(db, s)
    assert s.parkedAt is not None and s.eventTeamId == world['a'].id  # last team kept
    # a second parked device reuses the existing group
    s2 = _account(db, ots, world, 'evt1_dddddddd')
    assert _move(client, admin, world, s2, None).status_code == 200
    assert ots.groups == {parked, PARKED_IN % world['eid']}


def test_park_group_creation_failure_changes_nothing(client, db, ots, world, admin):
    s = _account(db, ots, world)
    ots.fail_create_group = True
    r = _move(client, admin, world, s, None)
    assert r.status_code == 502
    assert ots.calls == []
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    assert _reload(db, s).parkedAt is None


def test_unpark_to_team_clears_parked_and_leaves_parked_group(client, db, ots, world, admin):
    s = _account(db, ots, world, team=world['a'], parked=True)
    r = _move(client, admin, world, s, world['b'].id)
    assert r.status_code == 200
    assert _groups_of(ots, s.otsUsername) == {('grp_B', 'IN'), ('grp_B', 'OUT')}
    assert [c[0] for c in ots.calls] == ['add', 'add', 'remove', 'remove']
    _never_groupless(ots, s.otsUsername)
    s = _reload(db, s)
    assert s.parkedAt is None and s.eventTeamId == world['b'].id


def test_unpark_to_last_team(client, db, ots, world, admin):
    s = _account(db, ots, world, team=world['a'], parked=True)
    assert _move(client, admin, world, s, world['a'].id).status_code == 200
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    assert _reload(db, s).parkedAt is None


def test_same_team_and_already_parked_are_noops(client, db, ots, world, admin):
    s = _account(db, ots, world)
    assert _move(client, admin, world, s, world['a'].id).status_code == 200
    p = _account(db, ots, world, 'evt1_pppppppp', parked=True)
    assert _move(client, admin, world, p, None).status_code == 200
    assert ots.calls == [] and ots.group_creates == 0


def test_add_failure_leaves_everything_unchanged(client, db, ots, world, admin):
    s = _account(db, ots, world)
    ots.fail_add_groups = {'grp_B'}
    r = _move(client, admin, world, s, world['b'].id)
    assert r.status_code == 502
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    s = _reload(db, s)
    assert s.eventTeamId == world['a'].id and s.parkedAt is None


def test_remove_failure_after_add_reverts_add_and_keeps_old_groups(client, db, ots, world, admin):
    s = _account(db, ots, world)
    ots.fail_remove_groups = {'grp_A'}
    r = _move(client, admin, world, s, world['b'].id)
    assert r.status_code == 502
    # never groupless, and not left in both: back to exactly the source state
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    s = _reload(db, s)
    assert s.eventTeamId == world['a'].id and s.parkedAt is None


def test_partial_remove_failure_restores_removed_membership(client, db, ots, world, admin):
    s = _account(db, ots, world)
    orig = ots.remove_user_from_group

    # OUT of the source group fails after its IN was already removed
    def flaky2(username, group, direction='IN'):
        if group == 'grp_A' and direction == 'OUT':
            raise RuntimeError('boom')
        return orig(username, group, direction)
    ots.remove_user_from_group = flaky2
    assert _move(client, admin, world, s, world['b'].id).status_code == 502
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}


def test_remove_that_commits_then_errors_is_reverted(client, db, ots, world, admin):
    s = _account(db, ots, world)
    ots.raise_after_remove = {'grp_A'}  # IN removal is done in OTS, but an error comes back
    assert _move(client, admin, world, s, world['b'].id).status_code == 502
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    _never_groupless(ots, s.otsUsername)
    assert _reload(db, s).eventTeamId == world['a'].id


def test_failed_add_step_itself_is_reverted(client, db, ots, world, admin):
    s = _account(db, ots, world)
    orig = ots.add_user_to_group

    def add_then_error(username, group, direction='IN'):
        orig(username, group, direction)  # state changes ...
        if (group, direction) == ('grp_B', 'OUT'):
            raise RuntimeError('timeout after commit')  # ... but the caller sees an error
    ots.add_user_to_group = add_then_error
    assert _move(client, admin, world, s, world['b'].id).status_code == 502
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    _never_groupless(ots, s.otsUsername)


def test_failed_restore_leaves_device_in_both_groups_not_none(client, db, ots, world, admin):
    s = _account(db, ots, world)
    ots.raise_after_remove = {'grp_A'}
    orig = ots.add_user_to_group

    def add(username, group, direction='IN'):
        if group == 'grp_A':
            raise RuntimeError('cannot restore')
        return orig(username, group, direction)
    ots.add_user_to_group = add
    assert _move(client, admin, world, s, world['b'].id).status_code == 502
    # old IN could not be restored, so the new groups are kept instead of leaving it groupless
    assert ('grp_B', 'IN') in _groups_of(ots, s.otsUsername)


def test_local_commit_failure_reverts_ots_change(client, db, ots, world, admin, monkeypatch):
    s = _account(db, ots, world)
    real = db.session.commit
    state = {'n': 0}

    def failing_commit():
        state['n'] += 1
        raise RuntimeError('db down')
    monkeypatch.setattr(db.session, 'commit', failing_commit)
    r = _move(client, admin, world, s, world['b'].id)
    monkeypatch.setattr(db.session, 'commit', real)
    assert r.status_code == 500 and state['n'] >= 1
    assert _groups_of(ots, s.otsUsername) == {('grp_A', 'IN'), ('grp_A', 'OUT')}
    _never_groupless(ots, s.otsUsername)
    assert _reload(db, s).eventTeamId == world['a'].id


def test_move_on_expired_session_is_409(client, db, ots, world, admin):
    s = _account(db, ots, world)
    s.expiresAt = datetime.datetime.now() - timedelta(minutes=1)
    db.session.commit()
    assert _move(client, admin, world, s, world['b'].id).status_code == 409
    assert _move(client, admin, world, s, None).status_code == 409
    assert ots.calls == []


def test_move_rejects_bad_targets(client, db, ots, world, admin):
    s = _account(db, ots, world)
    nogroup, _ = _team_link(db, world['event'], 'C', group=False)
    assert _move(client, admin, world, s, 99999).status_code == 400
    assert _move(client, admin, world, s, nogroup.id).status_code == 400
    assert _move(client, admin, world, s, 'x').status_code == 400
    assert _move(client, admin, world, s, True).status_code == 400
    r = client.post('%s/%d/accounts/%d/move' % (API, world['eid'], s.id), headers=admin, json={})
    assert r.status_code == 400
    world['b'].otsGroup.ots_missing_since = datetime.datetime.utcnow()
    db.session.commit()
    assert _move(client, admin, world, s, world['b'].id).status_code == 400
    assert ots.calls == []


def test_move_rejects_closed_legacy_and_other_event(client, db, ots, world, admin):
    closed = _account(db, ots, world, 'evt1_cccccccc')
    closed.closedAt = datetime.datetime.now()
    legacy = _account(db, ots, world, 'old_shared', mode='shared')
    db.session.commit()
    assert _move(client, admin, world, closed, world['b'].id).status_code == 409
    assert _move(client, admin, world, legacy, world['b'].id).status_code == 409
    other = _event(db)
    s = _account(db, ots, world, 'evt1_zzzzzzzz')
    r = client.post('%s/%d/accounts/%d/move' % (API, other.id, s.id), headers=admin,
                    json={'teamId': world['b'].id})
    assert r.status_code == 404
    r = client.post('%s/%d/accounts/%d/close' % (API, other.id, s.id), headers=admin)
    assert r.status_code == 404
    assert client.get('%s/99999/accounts' % API, headers=admin).status_code == 404
    assert ots.calls == [] and not getattr(ots, 'deactivated', [])


def test_move_is_logged_without_secrets(client, db, ots, world, admin, caplog):
    s = _account(db, ots, world)
    with caplog.at_level('INFO'):
        _move(client, admin, world, s, world['b'].id)
    msgs = [r.getMessage() for r in caplog.records if 'Event account move' in r.getMessage()]
    assert len(msgs) == 1
    assert s.otsUsername in msgs[0] and 'from=A' in msgs[0] and 'to=B' in msgs[0]


# ---- RBAC -------------------------------------------------------------------
def test_rbac(client, db, ots, world, admin, reader):
    s = _account(db, ots, world)
    list_url = '%s/%d/accounts' % (API, world['eid'])
    assert client.get(list_url, headers=reader).status_code == 200
    assert client.get(list_url, headers=admin).status_code == 200
    assert client.get(list_url).status_code == 401
    urls = ['%s/%d/accounts/%d/move' % (API, world['eid'], s.id),
            '%s/%d/accounts/%d/close' % (API, world['eid'], s.id),
            '%s/links/%d/close-sessions' % (API, world['link_a'].id)]
    leader = _user(db, 'mvleader')
    world['a'].leaderUserId = leader.id
    db.session.commit()
    leader_h = _leader_headers(leader, world['a'], world['event'])
    for url in urls:
        assert client.post(url, json={'teamId': None}).status_code == 401
        assert client.post(url, headers=reader, json={'teamId': None}).status_code == 403
        assert client.post(url, headers=leader_h, json={'teamId': None}).status_code == 403
    assert client.get(list_url, headers=leader_h).status_code == 403
    assert ots.calls == [] and _reload(db, s).closedAt is None


# ---- close ------------------------------------------------------------------
def test_close_by_admin_deactivates_and_leaves_groups(client, db, ots, world, admin):
    s = _account(db, ots, world)
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 200
    assert ots.deactivated == [s.otsUsername]
    assert _groups_of(ots, s.otsUsername) == set()
    s = _reload(db, s)
    assert s.closedAt is not None and s.closeReason == 'closed by admin'
    # a closed account cannot be closed again
    assert client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin).status_code == 409


def test_close_parked_account_leaves_parked_group_and_deletes_it(client, db, ots, world, admin):
    s = _account(db, ots, world, parked=True)
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 200
    assert _groups_of(ots, s.otsUsername) == set()
    assert sorted(ots.deleted_groups) == sorted([PARKED % world['eid'], PARKED_IN % world['eid']])


def test_close_delete_policy(client, db, ots, world, admin):
    world['event'].accountCleanup = 'delete'
    s = _account(db, ots, world)
    db.session.commit()
    assert client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin).status_code == 200
    assert ots.removed_users == [s.otsUsername]


def test_close_fails_if_account_cannot_be_deactivated(client, db, ots, world, admin):
    s = _account(db, ots, world)
    ots.fail_deactivate = True
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 502
    assert _reload(db, s).closedAt is None


def test_close_group_failure_keeps_session_open_for_the_job(client, db, ots, world, admin):
    s = _account(db, ots, world)
    ots.fail_remove_groups = {'grp_A'}
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 502
    body = r.get_json()
    assert body['accountDisabled'] is True and body['cleanupPending'] is True
    assert ots.deactivated == [s.otsUsername]
    s = _reload(db, s)
    assert s.closedAt is None and s.expiresAt <= datetime.datetime.now()


def test_close_colour_failure_keeps_session_open(client, db, ots, world, admin, monkeypatch):
    s = _account(db, ots, world)
    monkeypatch.setattr('app.services.event_team_colors.read_profiles',
                        lambda: (_ for _ in ()).throw(RuntimeError('down')))
    r = client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin)
    assert r.status_code == 502 and r.get_json()['cleanupPending'] is True
    assert _reload(db, s).closedAt is None


def test_close_job_retries_a_pending_admin_close(db, ots, app, monkeypatch, client, world, admin):
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    s = _account(db, ots, world)
    ots.fail_remove_groups = {'grp_A'}
    assert client.post('%s/%d/accounts/%d/close' % (API, world['eid'], s.id), headers=admin).status_code == 502
    ots.fail_remove_groups = set()
    jobs.close_expired_event_sessions()
    s = _reload(db, s)
    assert s.closedAt is not None and s.closeReason == 'expired'
    assert _groups_of(ots, s.otsUsername) == set()


def test_job_treats_404_on_group_removal_as_success(db, ots, app, monkeypatch, world):
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    s = _account(db, ots, world, parked=True)
    ots.remove_status = {PARKED % world['eid']: 404, PARKED_IN % world['eid']: 404}
    s.expiresAt = datetime.datetime.now() - timedelta(minutes=1)
    db.session.commit()
    jobs.close_expired_event_sessions()
    s = _reload(db, s)
    assert s.closedAt is not None and s.closeReason == 'expired'


def test_job_other_group_removal_errors_still_retry(db, ots, app, monkeypatch, world):
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    s = _account(db, ots, world, parked=True)
    ots.remove_status = {PARKED % world['eid']: 500}
    s.expiresAt = datetime.datetime.now() - timedelta(minutes=1)
    db.session.commit()
    jobs.close_expired_event_sessions()
    assert _reload(db, s).closedAt is None


def test_close_by_link_only_that_links_open_device_sessions(client, db, ots, world, admin):
    s1 = _account(db, ots, world, 'evt1_aaaaaaaa', link=world['link_a'])
    s2 = _account(db, ots, world, 'evt1_bbbbbbbb', link=world['link_a'], parked=True)
    other = _account(db, ots, world, 'evt1_cccccccc', team=world['b'], link=world['link_b'])
    r = client.post('%s/links/%d/close-sessions' % (API, world['link_a'].id), headers=admin)
    assert r.status_code == 200 and r.get_json() == {'closed': 2, 'failed': 0, 'remaining': 0}
    assert sorted(ots.deactivated) == ['evt1_aaaaaaaa', 'evt1_bbbbbbbb']
    assert _reload(db, s1).closeReason == 'closed by admin' and _reload(db, s2).closedAt
    assert _reload(db, other).closedAt is None
    assert client.post('%s/links/99999/close-sessions' % API, headers=admin).status_code == 404


def test_close_by_link_partial_failure_is_502(client, db, ots, world, admin):
    _account(db, ots, world, 'evt1_aaaaaaaa', link=world['link_a'])
    orig = ots.deactivate_user

    def deactivate(username):
        if username == 'evt1_aaaaaaaa':
            raise RuntimeError('down')
        return orig(username)
    ots.deactivate_user = deactivate
    s2 = _account(db, ots, world, 'evt1_bbbbbbbb', link=world['link_a'])
    r = client.post('%s/links/%d/close-sessions' % (API, world['link_a'].id), headers=admin)
    assert r.status_code == 502 and r.get_json()['closed'] == 1 and r.get_json()['failed'] == 1
    assert r.get_json()['remaining'] == 1
    assert _reload(db, s2).closedAt is not None


def test_close_by_link_is_time_boxed(client, db, ots, world, admin, monkeypatch):
    monkeypatch.setattr(events_mod, 'CLOSE_BATCH_SIZE', 2)
    for i in range(5):
        _account(db, ots, world, 'evt1_u%07d' % i, link=world['link_a'])
    url = '%s/links/%d/close-sessions' % (API, world['link_a'].id)
    assert client.post(url, headers=admin).get_json() == {'closed': 2, 'failed': 0, 'remaining': 3}
    assert client.post(url, headers=admin).get_json() == {'closed': 2, 'failed': 0, 'remaining': 1}
    assert client.post(url, headers=admin).get_json() == {'closed': 1, 'failed': 0, 'remaining': 0}


# ---- parked group lifecycle and the close job -------------------------------
def test_close_job_removes_parked_membership_and_group(db, ots, app, monkeypatch, world):
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    s = _account(db, ots, world, parked=True)
    s.expiresAt = datetime.datetime.now() - timedelta(minutes=1)
    db.session.commit()
    jobs.close_expired_event_sessions()
    s = _reload(db, s)
    assert s.closedAt is not None and s.closeReason == 'expired'
    assert sorted(ots.calls) == sorted([
        ('remove', s.otsUsername, PARKED % world['eid'], 'OUT'),
        ('remove', s.otsUsername, PARKED_IN % world['eid'], 'IN')])
    assert sorted(ots.deleted_groups) == sorted([PARKED % world['eid'], PARKED_IN % world['eid']])


def test_parked_group_kept_while_device_sessions_open(db, ots, world, app):
    _account(db, ots, world, 'evt1_aaaaaaaa', parked=True)
    with app.app_context():
        assert events_mod.cleanup_parked_group(world['eid']) is False
    assert ots.deleted_groups == []


def test_parked_group_cleanup_failure_is_swallowed(db, ots, world, app):
    ots.groups.update({PARKED % world['eid'], PARKED_IN % world['eid']})
    ots.delete_group = MagicMock(side_effect=RuntimeError('down'))
    assert events_mod.cleanup_parked_group(world['eid']) is False


# ---- group sync skips evt<N>_ groups -----------------------------------------
def test_group_sync_skips_event_groups_and_never_flags_them(client, db, monkeypatch):
    mock = MagicMock()
    names = ['mv_normal', 'evt3_parked', 'evt3_parked_in', 'evt2024_staff']
    mock.get_all_pages.side_effect = lambda fetch, **kw: {'response': {
        'results': [{'name': n} for n in names], 'total': len(names),
        'total_pages': 1, 'current_page': 1, 'per_page': 100}}
    monkeypatch.setattr('app.ots.otsClient', mock)
    h = _headers(db, 'mvsyncadmin', 'administrator')
    # a pre-existing local row of such a name must not be flagged missing either
    db.session.add(OTSGroupModel(name='evt9_parked'))
    db.session.commit()
    try:
        r = client.post('/api/v1/groups/sync', headers=h)
        assert r.status_code == 200
        body = r.get_json()
        assert body['created'] == 2 and body['markedMissing'] == 0
        assert OTSGroupModel.get_by_name('mv_normal') is not None
        assert OTSGroupModel.get_by_name('evt3_parked') is None
        assert OTSGroupModel.get_by_name('evt3_parked_in') is None
        assert OTSGroupModel.get_by_name('evt2024_staff') is not None  # a real group still syncs
        assert OTSGroupModel.get_by_name('evt9_parked').ots_missing_since is None
        listed = client.get('/api/v1/groups', headers=h).get_json()['groups']
        assert not any(g['name'] in ('evt3_parked', 'evt3_parked_in') for g in listed)
    finally:
        OTSGroupModel.query.delete()
        db.session.commit()


# ---- colour interplay -------------------------------------------------------
def test_parking_removes_colour_and_moving_applies_new_colour(client, db, ots, world, admin, monkeypatch):
    monkeypatch.setattr(SystemSettingsModel, 'get_setting', staticmethod(lambda k, d=None: True))
    s = _account(db, ots, world)
    ots.euds = [eud('u1', s.otsUsername)]
    etc.sync_event_team_colors()
    assert ots.profiles[('locationTeam', 'u1')] == 'Red'
    assert ots.profiles[('atakRoleType', 'u1')] == 'Team Member'

    assert _move(client, admin, world, s, None).status_code == 200
    etc.sync_event_team_colors()
    assert ('locationTeam', 'u1') not in ots.profiles and ('atakRoleType', 'u1') not in ots.profiles

    assert _move(client, admin, world, s, world['b'].id).status_code == 200
    etc.sync_event_team_colors()
    assert ots.profiles[('locationTeam', 'u1')] == 'Blue'

    assert _move(client, admin, world, s, world['a'].id).status_code == 200
    etc.sync_event_team_colors()
    assert ots.profiles[('locationTeam', 'u1')] == 'Red'


def test_create_group_rejects_managed_event_names(client, db, monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr('app.ots.otsClient', mock)
    h = _headers(db, 'mvgroupadmin', 'administrator')
    for name in ('evt3_parked', 'evt3_parked_in'):
        r = client.post('/api/v1/groups', headers=h, json={'name': name})
        assert r.status_code == 400
    mock.create_group.assert_not_called()
    # a real group that merely starts with evt<N>_ is allowed
    r = client.post('/api/v1/groups', headers=h, json={'name': 'evt2024_staff'})
    assert r.status_code in (200, 201)
    OTSGroupModel.query.delete()
    db.session.commit()
