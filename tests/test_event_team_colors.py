"""
Tests for applying event team colours to ATAK devices via OTS device profiles
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import scheduler
from app.models import (
    db as _db, EventModel, EventLinkModel, EventSessionModel, EventTeamModel,
    GroupUserAssociation, OTSGroupModel, OnboardingCodeModel, SystemSettingsModel, UserModel,
)
from app.services import event_team_colors as etc
from app import jobs


class FakeOTS:
    """Stand-in for OTSClient holding EUDs and device profiles in memory."""

    def __init__(self, euds=(), profiles=None):
        self.euds = list(euds)
        self.profiles = dict(profiles or {})  # (key, uid) -> value
        self.created = []
        self.deleted = []
        self.fail_uids = set()
        self.profile_reads = 0

    def get_euds(self, page=None, per_page=None, username=None):
        euds = [e for e in self.euds if username is None or e['username'] == username]
        return {'response': {'results': euds, 'total_pages': 1}}

    def get_device_profiles(self, page=None, per_page=None, preference_key=None):
        self.profile_reads += 1
        results = [
            {'preference_key': k, 'eud_uid': uid, 'preference_value': v}
            for (k, uid), v in self.profiles.items() if k == preference_key
        ]
        return {'response': {'results': results, 'total_pages': 1}}

    def get_all_pages(self, fetch, per_page=100, **filters):
        return fetch(page=1, per_page=per_page, **filters)

    def create_device_profile(self, key, value, value_class, enrollment=False, connection=True,
                              active=True, tool=None, eud_uid=None):
        if eud_uid in self.fail_uids:
            raise Exception('boom')
        self.created.append((key, value, eud_uid))
        self.profiles[(key, eud_uid)] = value

    def delete_device_profile(self, key, eud_uid):
        if not eud_uid:
            raise ValueError("eud_uid is required to delete a device profile")
        self.deleted.append((key, eud_uid))
        del self.profiles[(key, eud_uid)]

    def deactivate_user(self, username):
        self.deactivated = getattr(self, 'deactivated', []) + [username]

    def delete_user(self, username):
        self.removed_users = getattr(self, 'removed_users', []) + [username]


@pytest.fixture
def ots(monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(etc, 'otsClient', fake)
    monkeypatch.setattr(jobs, 'otsClient', fake)
    return fake


@pytest.fixture
def events_enabled(monkeypatch):
    monkeypatch.setattr(SystemSettingsModel, 'get_setting',
                        staticmethod(lambda key, default=None: True))


TABLES = (EventSessionModel, EventLinkModel, EventModel, EventTeamModel,
          GroupUserAssociation, OTSGroupModel, OnboardingCodeModel, UserModel)


def _pks(model):
    col = list(model.__table__.primary_key.columns)
    return set(_db.session.query(*col).all())


@pytest.fixture
def data(app):
    """Test data (jobs commit); only rows created during the test are removed."""
    with app.app_context():
        before = {m: _pks(m) for m in TABLES}
        yield _db
        _db.session.rollback()
        for m in TABLES:
            cols = list(m.__table__.primary_key.columns)
            for pk in _pks(m) - before[m]:
                m.query.filter(*[c == v for c, v in zip(cols, pk)]).delete(synchronize_session=False)
        _db.session.commit()


def make_event(db, active=True, start=-1, end=1, mode='shared'):
    now = datetime.now()
    event = EventModel(name='zz_event', active=active, playerAccessMode=mode,
                       validFrom=now + timedelta(hours=start) if start is not None else None,
                       validUntil=now + timedelta(hours=end) if end is not None else None)
    db.session.add(event)
    db.session.flush()
    return event


def make_team(db, name='zz_red', color='Red', leader=None):
    team = EventTeamModel(name=name, color=color, leaderUserId=leader.id if leader else None)
    db.session.add(team)
    db.session.flush()
    return team


def make_user(db, username):
    user = UserModel(username=username)
    db.session.add(user)
    db.session.flush()
    return user


def make_session(db, event, team, username, mode='shared', expires=None):
    s = EventSessionModel(eventId=event.id, eventTeamId=team.id, otsUsername=username,
                          mode=mode, expiresAt=expires)
    db.session.add(s)
    db.session.flush()
    return s


def eud(uid, username):
    return {'uid': uid, 'username': username}


def test_shared_session_gets_colour_and_role(data, ots, events_enabled):
    event = make_event(data)
    team = make_team(data)
    make_session(data, event, team, 'evt1_t1')
    ots.euds = [eud('u1', 'evt1_t1')]
    etc.sync_event_team_colors()
    assert ots.profiles == {('locationTeam', 'u1'): 'Red', ('atakRoleType', 'u1'): 'Team Member'}


def test_desired_state_shared_leader_individual(data):
    event = make_event(data)
    leader = make_user(data, 'zz_leader')
    team = make_team(data, color='Blue', leader=leader)
    make_session(data, event, team, 'shared_acc', mode='shared')
    make_session(data, event, team, 'zz_player', mode='individual')
    make_session(data, event, team, 'closed_acc').closedAt = datetime.now()
    data.session.add(EventLinkModel(eventId=event.id, eventTeamId=team.id, role='leader', tokenHash='h1'))
    data.session.flush()
    desired = etc._build_desired_state([event])
    assert desired == {n: ('Blue', 'Team Member') for n in ('shared_acc', 'zz_player', 'zz_leader')}


def test_leader_without_leader_link_gets_nothing(data):
    event = make_event(data)
    leader = make_user(data, 'zz_leader')
    make_team(data, leader=leader)
    assert etc._build_desired_state([event]) == {}


def test_no_write_when_state_matches(data, ots, events_enabled):
    event = make_event(data)
    make_session(data, event, make_team(data), 'evt1_t1')
    ots.euds = [eud('u1', 'evt1_t1')]
    ots.profiles = {('locationTeam', 'u1'): 'Red', ('atakRoleType', 'u1'): 'Team Member'}
    etc.sync_event_team_colors()
    assert ots.created == [] and ots.deleted == []


def test_write_on_colour_change(data, ots, events_enabled):
    event = make_event(data)
    make_session(data, event, make_team(data, color='Green'), 'evt1_t1')
    ots.euds = [eud('u1', 'evt1_t1')]
    ots.profiles = {('locationTeam', 'u1'): 'Red', ('atakRoleType', 'u1'): 'Team Member'}
    etc.sync_event_team_colors()
    assert ots.created == [('locationTeam', 'Green', 'u1')]


def test_group_member_outside_session_gets_nothing(data, ots, events_enabled):
    event = make_event(data)
    team = make_team(data)
    group = OTSGroupModel(name='zz_group')
    data.session.add(group)
    data.session.flush()
    team.otsGroupId = group.id
    member = make_user(data, 'zz_selfservice')
    data.session.add(GroupUserAssociation(group_id=group.id, user_id=member.id))
    make_session(data, event, team, 'evt1_t1')
    data.session.flush()
    ots.euds = [eud('u2', 'zz_selfservice')]
    etc.sync_event_team_colors()
    assert ots.created == [] and ots.profiles == {}


def test_empty_uid_is_skipped(data, ots, events_enabled):
    event = make_event(data)
    make_session(data, event, make_team(data), 'evt1_t1')
    ots.euds = [eud('', 'evt1_t1'), eud(None, 'evt1_t1')]
    etc.sync_event_team_colors()
    assert ots.created == []


def test_one_failing_eud_does_not_stop_others(data, ots, events_enabled):
    event = make_event(data)
    make_session(data, event, make_team(data), 'evt1_t1')
    ots.euds = [eud('bad', 'evt1_t1'), eud('good', 'evt1_t1')]
    ots.fail_uids = {'bad'}
    etc.sync_event_team_colors()
    assert ('locationTeam', 'good') in ots.profiles and ('locationTeam', 'bad') not in ots.profiles


def test_no_ots_calls_when_disabled_or_no_open_event(data, ots, monkeypatch):
    calls = []
    monkeypatch.setattr(ots, 'get_euds', lambda *a, **k: calls.append(1) or {'response': {'results': []}})
    event = make_event(data)
    make_session(data, event, make_team(data), 'evt1_t1')
    monkeypatch.setattr(SystemSettingsModel, 'get_setting', staticmethod(lambda k, d=None: False))
    etc.sync_event_team_colors()
    assert calls == []
    # positive control: enabled and open event makes the stub get called
    monkeypatch.setattr(SystemSettingsModel, 'get_setting', staticmethod(lambda k, d=None: True))
    etc.sync_event_team_colors()
    assert calls == [1]
    calls.clear()
    # inactive event, and an event that ended more than 24h ago: no calls
    event.active = False
    etc.sync_event_team_colors()
    event.active = True
    event.validUntil = datetime.now() - timedelta(hours=25)
    event.validFrom = None
    data.session.flush()
    etc.sync_event_team_colors()
    assert calls == []


def test_leader_cleanup_runs_for_24h_after_event_end(data, ots, events_enabled):
    event = make_event(data, start=-30, end=-2)
    leader = make_user(data, 'zz_leader')
    make_team(data, leader=leader)
    ots.euds = [eud('L', 'zz_leader')]
    ots.profiles = {('locationTeam', 'L'): 'Red', ('atakRoleType', 'L'): 'Team Member'}
    etc.sync_event_team_colors()
    assert ots.profiles == {}


def test_no_leader_cleanup_after_24h(data, ots, events_enabled):
    make_event(data, start=-60, end=-30)
    leader = make_user(data, 'zz_leader')
    make_team(data, leader=leader)
    ots.euds = [eud('L', 'zz_leader')]
    ots.profiles = {('locationTeam', 'L'): 'Red', ('atakRoleType', 'L'): 'Team Member'}
    etc.sync_event_team_colors()
    assert len(ots.profiles) == 2


def test_leader_removal_only_deletes_expected_values(data, ots, events_enabled):
    event = make_event(data)
    leader = make_user(data, 'zz_leader')
    team = make_team(data, leader=leader)
    make_session(data, event, team, 'evt1_t1')
    ots.euds = [eud('L1', 'zz_leader'), eud('L2', 'zz_leader')]
    ots.profiles = {
        ('locationTeam', 'L1'): 'Red', ('atakRoleType', 'L1'): 'Team Member',
        ('locationTeam', 'L2'): 'Blue', ('atakRoleType', 'L2'): 'Team Lead',
    }
    etc.sync_event_team_colors()
    assert ots.profiles == {('locationTeam', 'L2'): 'Blue', ('atakRoleType', 'L2'): 'Team Lead'}


def test_invalid_team_colour_is_skipped(data):
    event = make_event(data)
    make_session(data, event, make_team(data, color='Chartreuse'), 'evt1_t1')
    assert etc._build_desired_state([event]) == {}


def test_real_delete_device_profile_requires_uid():
    from app.ots import OTSClient
    client = OTSClient('http://localhost:8080', 'u', 'p')
    for uid in ('', None):
        with pytest.raises(ValueError):
            client.delete_device_profile('locationTeam', uid)


def test_close_job_removes_profiles_and_keeps_individual_account(data, ots, app, monkeypatch):
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    event = make_event(data, end=-0.5)
    team = make_team(data)
    past = datetime.now() - timedelta(minutes=1)
    make_session(data, event, team, 'shared_acc', mode='shared', expires=past)
    make_session(data, event, team, 'zz_player', mode='individual', expires=past)
    data.session.commit()
    ots.euds = [eud('s1', 'shared_acc'), eud('p1', 'zz_player')]
    ots.profiles = {(k, u): v for u in ('s1', 'p1')
                    for k, v in (('locationTeam', 'Red'), ('atakRoleType', 'Team Member'))}
    jobs.close_expired_event_sessions()
    assert ots.profiles == {}
    assert getattr(ots, 'deactivated', []) == ['shared_acc']
    assert getattr(ots, 'removed_users', []) == []
    assert all(s.closedAt for s in EventSessionModel.query.filter_by(eventId=event.id))


def test_close_job_reads_profiles_once(data, ots, app, monkeypatch):
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    event = make_event(data, end=-0.5)
    team = make_team(data)
    past = datetime.now() - timedelta(minutes=1)
    for i in range(5):
        make_session(data, event, team, f'zz_p{i}', mode='individual', expires=past)
    data.session.commit()
    ots.euds = [eud(f'p{i}', f'zz_p{i}') for i in range(5)]
    ots.profiles = {(k, f'p{i}'): v for i in range(5)
                    for k, v in (('locationTeam', 'Red'), ('atakRoleType', 'Team Member'))}
    jobs.close_expired_event_sessions()
    assert ots.profiles == {}
    assert ots.profile_reads == 2  # one per managed key, not per session


def test_record_event_registration(data):
    from app.api_v1.events import record_event_registration
    event = make_event(data, mode='individual')
    team = make_team(data)
    code = OnboardingCodeModel(name='zz_code', onboardingCode='zzcode')
    data.session.add(code)
    data.session.flush()
    data.session.add(EventLinkModel(eventId=event.id, eventTeamId=team.id, role='player',
                                    tokenHash='h2', onboardingCodeId=code.id))
    data.session.flush()
    record_event_registration('zz_new', code)
    s = EventSessionModel.query.filter_by(otsUsername='zz_new').one()
    assert (s.otsUsername, s.mode, s.eventTeamId, s.expiresAt) == ('zz_new', 'individual', team.id, event.validUntil)

    other = OnboardingCodeModel(name='zz_other', onboardingCode='zzother')
    data.session.add(other)
    data.session.flush()
    record_event_registration('zz_x', other)
    assert EventSessionModel.query.filter_by(otsUsername='zz_x').count() == 0


def _registration_setup(data, **event_kwargs):
    event = make_event(data, mode=event_kwargs.pop('mode', 'individual'), **event_kwargs)
    team = make_team(data)
    code = OnboardingCodeModel(name='zz_code', onboardingCode='zzcode')
    data.session.add(code)
    data.session.flush()
    data.session.add(EventLinkModel(eventId=event.id, eventTeamId=team.id, role='player',
                                    tokenHash='h3', onboardingCodeId=code.id))
    data.session.flush()
    return event, code


def _recorded(username):
    return EventSessionModel.query.filter_by(otsUsername=username).count()


def test_record_skips_inactive_ended_and_shared_events(data):
    from app.api_v1.events import record_event_registration
    cases = [dict(active=False), dict(start=-30, end=-2), dict(mode='shared')]
    for i, kwargs in enumerate(cases):
        _, code = _registration_setup(data, **kwargs)
        record_event_registration(f'zz_case{i}', code)
        assert _recorded(f'zz_case{i}') == 0
        data.session.rollback()


def test_record_allows_registration_before_event_start(data):
    from app.api_v1.events import record_event_registration
    _, code = _registration_setup(data, start=48, end=72)
    record_event_registration('zz_early', code)
    assert _recorded('zz_early') == 1


def test_record_never_raises(data, monkeypatch):
    from app.api_v1 import events
    _, code = _registration_setup(data)
    monkeypatch.setattr(events, 'EventSessionModel', None)
    events.record_event_registration('zz_boom', code)
