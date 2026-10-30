"""
Shared player mode: one OTS account per device. Fake OTS, no network.
"""
import datetime
import re
from datetime import timedelta

import pytest

from app import jobs
from app.exceptions import BadRequestError
from app.api_v1 import events as events_mod
from app.extensions import scheduler
from app.models import (EventLinkModel, EventModel, EventSessionModel, EventTeamModel,
                        OTSGroupModel, SystemSettingsModel, UserModel)
from app.services import event_team_colors as etc

from tests.test_event_my_team import _leader_headers, _user, strong_key  # noqa: F401
from tests.test_event_team_colors import FakeOTS as ColorFakeOTS, eud

TOKEN = 'player-token-1'
USERNAME_RE = re.compile(r'^evt\d+_[a-z0-9]{8}$')


class FakeOTS(ColorFakeOTS):
    def __init__(self):
        super().__init__()
        self.users = []
        self.passwords = []
        self.group_adds = []
        self.group_removes = []
        self.qr_calls = []
        self.qr_deletes = []
        self.events = []
        self.fail_remove_group = False
        self.fail_qr = False
        self.fail_create_times = 0
        self.fail_create_other = False
        self.fail_activate = False
        self.fail_qr_delete = False
        self.fail_create_400 = False  # 400 for a name that does not exist (real failure)
        self.taken = set()  # users that exist in OTS but are not ours
        self.delete_attempts = []

    def get_users(self, page=None, per_page=None, username=None, email=None):
        names = [u for u, _ in self.users] + sorted(self.taken)
        return {'response': {'results': [{'username': n} for n in names
                                         if username is None or n == username]}}

    def delete_user(self, username):
        self.delete_attempts.append(username)
        if username not in [u for u, _ in self.users] and username not in self.taken:
            raise BadRequestError({'response': {'errors': {'username': ['Failed to delete user']}},
                                   'status_code': 400})
        super().delete_user(username)

    def create_user(self, username, password, roles=None):
        if self.fail_create_400:
            raise BadRequestError({'response': {'errors': {'password': ['too weak']}},
                                   'status_code': 400})
        if self.fail_create_times:
            self.fail_create_times -= 1
            self.taken.add(username)
            raise BadRequestError({'response': {'errors': {'username': ['Username already exists']}},
                                   'status_code': 400})
        if self.fail_create_other:
            raise RuntimeError('ots down')
        self.users.append((username, roles))
        self.passwords.append(password)
        return {}

    def activate_user(self, username):
        if self.fail_activate:
            raise RuntimeError('activate failed')
        self.events.append(('activate', username))

    def add_user_to_group(self, username, group, direction='IN'):
        self.events.append(('add', username))
        self.group_adds.append((username, group, direction))

    def remove_user_from_group(self, username, group, direction='IN'):
        if self.fail_remove_group:
            raise RuntimeError('group removal failed')
        self.events.append(('remove', username))
        self.group_removes.append((username, group, direction))

    def deactivate_user(self, username):
        self.events.append(('deactivate', username))
        super().deactivate_user(username)

    def create_atak_qr_string(self, username=None, exp=None, nbf=None, max_uses=None):
        if self.fail_qr:
            raise RuntimeError('boom')
        self.qr_calls.append({'username': username, 'exp': exp, 'max_uses': max_uses})
        return {'response': {'qr_string': 'tak://enroll?u=%s&n=%d' % (username, len(self.qr_calls))}}

    def delete_atak_qr_string(self, username):
        if self.fail_qr_delete:
            raise RuntimeError('qr delete failed')
        self.qr_deletes.append(username)

    def get_itak_qr_string(self, username):
        raise RuntimeError('not supported')


@pytest.fixture
def ots(monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(events_mod, 'otsClient', fake)
    monkeypatch.setattr(etc, 'otsClient', fake)
    monkeypatch.setattr(jobs, 'otsClient', fake)
    return fake


@pytest.fixture(autouse=True)
def _clean(db):
    yield
    db.session.rollback()
    for model in (EventSessionModel, EventLinkModel, EventTeamModel, EventModel, OTSGroupModel):
        model.query.delete()
    db.session.commit()


def _event(db, mode='shared', until_hours=5):
    now = datetime.datetime.now()
    event = EventModel(name='ev', playerAccessMode=mode,
                       validFrom=now - timedelta(hours=1),
                       validUntil=now + timedelta(hours=until_hours) if until_hours is not None else None)
    db.session.add(event)
    db.session.commit()
    return event


def _team_link(db, event, name='A', color='Red', leader=None, group=True):
    grp = None
    if group:
        grp = OTSGroupModel(name='grp_' + name)
        db.session.add(grp)
        db.session.flush()
    team = EventTeamModel(name=name, color=color, otsGroupId=grp.id if grp else None,
                          leaderUserId=leader.id if leader else None)
    db.session.add(team)
    db.session.commit()
    link = EventLinkModel(eventId=event.id, eventTeamId=team.id, role='player',
                          tokenHash=events_mod._hash_token(TOKEN + name), tokenVersion=1,
                          tokenNonce='nonce-' + name)
    db.session.add(link)
    db.session.commit()
    return team, link


@pytest.fixture
def world(db):
    SystemSettingsModel.set_setting('events_enabled', True)
    event = _event(db)
    team, link = _team_link(db, event)
    return dict(event=event, team=team, link=link, url='/api/v1/events/join/' + TOKEN + 'A')


def _forget(client, w):
    client.delete_cookie(events_mod.DEVICE_COOKIE_NAME, domain='localhost', path=w['url'])


def _forget_cookie_only(client, w):
    client.delete_cookie(events_mod.DEVICE_COOKIE_NAME, domain='localhost', path=w['url'])


def _join(client, w):
    return client.post(w['url'])


def test_new_account_per_visit_without_cookie(client, ots, world):
    r1 = _join(client, world)
    _forget(client, world)
    r2 = _join(client, world)
    assert r1.status_code == r2.status_code == 200
    names = [u for u, _ in ots.users]
    assert len(names) == 2 and names[0] != names[1]
    assert all(USERNAME_RE.match(n) for n in names)
    assert all(roles == ['user'] for _, roles in ots.users)
    sessions = EventSessionModel.query.filter_by(mode='device').all()
    assert {s.otsUsername for s in sessions} == set(names)
    s = sessions[0]
    assert s.eventId == world['event'].id and s.eventTeamId == world['team'].id
    assert s.eventLinkId == world['link'].id and s.expiresAt == world['event'].validUntil
    assert s.clientKeyHash and s.parkedAt is None


def test_reload_with_cookie_same_account_fresh_enrollment(client, ots, world):
    r1 = _join(client, world)
    r2 = _join(client, world)
    assert r2.status_code == 200
    assert len(ots.users) == 1
    assert EventSessionModel.query.filter_by(mode='device').count() == 1
    username = ots.users[0][0]
    # the old token is dropped first (OTS never resets total_uses), then a new one is made
    assert ots.qr_deletes == [username]
    assert len(ots.qr_calls) == 2
    assert r1.get_json()['atak']['qrString'] != r2.get_json()['atak']['qrString']
    assert 'Set-Cookie' not in r2.headers.to_wsgi_list().__str__()


def test_cookie_of_another_link_or_closed_session_is_ignored(client, ots, world, db):
    _join(client, world)
    s = EventSessionModel.query.filter_by(mode='device').one()
    s.closedAt = datetime.datetime.now()
    db.session.commit()
    _join(client, world)
    assert len(ots.users) == 2


def test_cookie_of_another_link_does_not_match(client, ots, world, db):
    team_b, link_b = _team_link(db, world['event'], 'B', color='Blue')
    url_b = '/api/v1/events/join/' + TOKEN + 'B'
    r = _join(client, world)
    assert r.status_code == 200
    # the cookie is scoped to link A's path: present it explicitly to link B
    header = [c for c in r.headers.getlist('Set-Cookie') if c.startswith(events_mod.DEVICE_COOKIE_NAME)][0]
    key = header.split(';')[0].split('=', 1)[1]
    _forget(client, world)
    client.set_cookie(events_mod.DEVICE_COOKIE_NAME, key, path=url_b)
    assert client.post(url_b).status_code == 200
    assert len(ots.users) == 2 and ots.qr_deletes == []  # no re-issue: new account for B
    by_link = {s.eventLinkId for s in EventSessionModel.query.filter_by(mode='device')}
    assert by_link == {world['link'].id, link_b.id}


def test_cookie_flags(client, ots, world):
    r = client.post(world['url'], headers={'X-Forwarded-Proto': 'https'})
    cookie = [c for c in r.headers.getlist('Set-Cookie') if c.startswith(events_mod.DEVICE_COOKIE_NAME)][0]
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Secure' in cookie
    assert 'Path=%s' % world['url'] in cookie
    # http without forwarded proto: not Secure
    _forget(client, world)
    r = client.post(world['url'])
    cookie = [c for c in r.headers.getlist('Set-Cookie') if c.startswith(events_mod.DEVICE_COOKIE_NAME)][0]
    assert 'Secure' not in cookie


def test_enrollment_parameters_and_no_password_leak(client, ots, world):
    r = _join(client, world)
    d = r.get_json()
    call = ots.qr_calls[0]
    assert call['max_uses'] == events_mod.DEVICE_ENROLLMENT_MAX_USES == 3
    assert call['username'] == ots.users[0][0]
    import time
    assert call['exp'] <= int(time.time()) + events_mod.DEVICE_ENROLLMENT_TTL_SECONDS
    assert d['atak']['expiresAt'] == call['exp']
    body = r.get_data(as_text=True)
    assert ots.passwords[0] not in body
    assert 'password' not in body.lower()


def test_enrollment_expiry_capped_by_event_end(client, ots, db):
    SystemSettingsModel.set_setting('events_enabled', True)
    event = _event(db, until_hours=None)
    event.validUntil = datetime.datetime.now() + timedelta(minutes=5)
    db.session.commit()
    team, link = _team_link(db, event)
    import time
    client.post('/api/v1/events/join/' + TOKEN + 'A')
    assert ots.qr_calls[0]['exp'] <= int(time.time()) + 5 * 60


def test_group_added_in_and_out(client, ots, world):
    _join(client, world)
    u = ots.users[0][0]
    assert ots.group_adds == [(u, 'grp_A', 'IN'), (u, 'grp_A', 'OUT')]
    assert ('activate', u) in ots.events
    kinds = [k for k, _ in ots.events]
    assert kinds.index('activate') < kinds.index('add')


def test_missing_group_skipped(client, ots, world, db):
    world['team'].otsGroup.ots_missing_since = datetime.datetime(2026, 1, 1)
    db.session.commit()
    assert _join(client, world).status_code == 200
    assert ots.group_adds == []


def test_username_collision_retries_then_fails(client, ots, world):
    ots.fail_create_times = 2
    assert _join(client, world).status_code == 200
    assert len(ots.users) == 1
    # colliding candidates leave no ledger rows behind
    assert EventSessionModel.query.filter_by(mode='device').count() == 1
    # someone else's accounts are never deleted
    assert ots.delete_attempts == []
    _forget(client, world)
    ots.fail_create_times = 3
    r = _join(client, world)
    assert r.status_code == 500
    assert EventSessionModel.query.filter_by(mode='device').count() == 1
    assert ots.delete_attempts == []


def test_400_for_unknown_name_is_real_failure_and_never_deletes(client, ots, world):
    ots.fail_create_400 = True
    calls = []
    orig = ots.create_user

    def counting(*a, **k):
        calls.append(a)
        return orig(*a, **k)
    ots.create_user = counting
    assert _join(client, world).status_code == 500
    assert len(calls) == 1
    assert ots.delete_attempts == []
    assert EventSessionModel.query.filter_by(mode='device').count() == 0


def test_other_create_error_does_not_retry(client, ots, world):
    ots.fail_create_other = True
    calls = []
    orig = ots.create_user

    def counting(*a, **k):
        calls.append(a)
        return orig(*a, **k)
    ots.create_user = counting
    r = _join(client, world)
    assert r.status_code == 500 and len(calls) == 1
    # outcome unknown: the account is discarded in OTS first. The fake does not
    # know the user (like real OTS: "Failed to delete user"), so the row stays
    # for the close job.
    assert ots.delete_attempts and EventSessionModel.query.filter_by(mode='device').count() == 1


def test_session_row_written_before_ots_create(client, ots, world):
    seen = []
    orig = ots.create_user

    def check(username, *a, **k):
        seen.append(EventSessionModel.query.filter_by(otsUsername=username).count())
        return orig(username, *a, **k)
    ots.create_user = check
    assert _join(client, world).status_code == 200
    assert seen == [1]


def test_activate_failure_deletes_ots_user_and_row(client, ots, world):
    ots.fail_activate = True
    assert _join(client, world).status_code == 500
    assert getattr(ots, 'removed_users', []) == [ots.users[0][0]]
    assert EventSessionModel.query.filter_by(mode='device').count() == 0


def test_discard_keeps_row_when_ots_delete_fails(client, ots, world):
    ots.fail_activate = True
    ots.fail_delete = True
    assert _join(client, world).status_code == 500
    rows = EventSessionModel.query.filter_by(mode='device').all()
    assert [r.otsUsername for r in rows] == [ots.users[0][0]]
    assert rows[0].expiresAt == world['event'].validUntil and rows[0].eventLinkId == world['link'].id


def test_other_create_error_keeps_row_if_delete_fails(client, ots, world):
    ots.fail_create_other = True
    ots.fail_delete = True
    assert _join(client, world).status_code == 500
    assert EventSessionModel.query.filter_by(mode='device').count() == 1


def test_other_create_error_drops_row_when_delete_works(client, ots, world):
    orig = ots.create_user

    def create_then_fail(username, *a, **k):
        orig(username, *a, **k)  # timeout after OTS created it
        raise RuntimeError('timeout')
    ots.create_user = create_then_fail
    assert _join(client, world).status_code == 500
    assert ots.removed_users == [ots.users[0][0]]
    assert EventSessionModel.query.filter_by(mode='device').count() == 0


def test_enrollment_failure_discards_new_account(client, ots, world):
    ots.fail_qr = True
    r = _join(client, world)
    assert r.status_code == 500
    assert EventSessionModel.query.filter_by(mode='device').count() == 0
    assert getattr(ots, 'removed_users', []) == [ots.users[0][0]]


def _open_devices(db, w, n, team=None, prefix='x'):
    team = team or w['team']
    old = datetime.datetime.utcnow() - timedelta(hours=1)
    for i in range(n):
        db.session.add(EventSessionModel(eventId=w['event'].id, eventTeamId=team.id,
                                         otsUsername='%s%d' % (prefix, i), mode='device', createdAt=old))
    db.session.commit()


def test_per_team_cap_returns_429(client, ots, world, db, monkeypatch):
    monkeypatch.setattr(events_mod, 'DEVICE_MAX_PER_TEAM', 2)
    _open_devices(db, world, 2)
    r = _join(client, world)
    assert r.status_code == 429
    assert ots.users == []
    # another team of the same event is not affected
    team_b, _ = _team_link(db, world['event'], 'B', color='Blue')
    assert client.post('/api/v1/events/join/' + TOKEN + 'B').status_code == 200
    # a closed session does not count
    EventSessionModel.query.filter_by(otsUsername='x0').one().closedAt = datetime.datetime.now()
    db.session.commit()
    _forget(client, world)
    assert _join(client, world).status_code == 200


def test_event_wide_cap_returns_429(client, ots, world, db, monkeypatch):
    monkeypatch.setattr(events_mod, 'EVENT_MAX_DEVICE_ACCOUNTS', 3)
    team_b, _ = _team_link(db, world['event'], 'B', color='Blue')
    _open_devices(db, world, 2, team=team_b, prefix='b')
    _open_devices(db, world, 1, prefix='a')
    assert _join(client, world).status_code == 429
    assert ots.users == []


def test_throttle_counts_attempts_including_failures(client, ots, world, monkeypatch):
    ots.fail_create_other = True
    for _ in range(events_mod.DEVICE_THROTTLE_MAX):
        assert _join(client, world).status_code == 500
    ots.fail_create_other = False
    assert _join(client, world).status_code == 429
    assert ots.users == []


def test_throttle_window_slides(client, ots, world, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(events_mod.time, 'monotonic', lambda: clock[0])
    for _ in range(events_mod.DEVICE_THROTTLE_MAX):
        _forget(client, world)
        assert _join(client, world).status_code == 200
    _forget(client, world)
    assert _join(client, world).status_code == 429
    clock[0] += events_mod.DEVICE_THROTTLE_WINDOW_SECONDS + 1
    assert _join(client, world).status_code == 200


def test_throttle_is_per_link_and_leader_is_separate(client, ots, world, db, strong_key, monkeypatch):
    monkeypatch.setattr(events_mod, 'DEVICE_THROTTLE_MAX', 5)
    leader = _user(db, 'leadera')
    world['team'].leaderUserId = leader.id
    team_b, link_b = _team_link(db, world['event'], 'B', color='Blue')
    db.session.commit()
    link_key = ('link', world['link'].id)
    for _ in range(5):
        _forget(client, world)
        assert _join(client, world).status_code == 200
    _forget(client, world)
    assert _join(client, world).status_code == 429
    assert len(events_mod._device_attempts[link_key]) == 5
    # other link: own budget
    assert client.post('/api/v1/events/join/' + TOKEN + 'B').status_code == 200
    assert len(events_mod._device_attempts[('link', link_b.id)]) == 1
    assert len(events_mod._device_attempts[link_key]) == 5
    # leader presses: own key, not blocked by the full player link, not counted on it
    h = _leader_headers(leader, world['team'], world['event'])
    leader_key = ('leader', world['event'].id, world['team'].id)
    for i in range(1, 4):
        assert client.post('/api/v1/events/my-team/enrollment', headers=h).status_code == 200
        assert len(events_mod._device_attempts[leader_key]) == i
        assert len(events_mod._device_attempts[link_key]) == 5


def test_individual_mode_untouched(client, ots, db):
    SystemSettingsModel.set_setting('events_enabled', True)
    event = _event(db, mode='individual')
    team, link = _team_link(db, event)
    r = client.post('/api/v1/events/join/' + TOKEN + 'A')
    assert r.status_code == 409  # no onboarding code configured, as before
    assert ots.users == [] and EventSessionModel.query.count() == 0


def test_shared_without_valid_until_rejected(client, db, world):
    from flask_jwt_extended import create_access_token
    from app.models import UserRoleModel
    u = _user(db, 'adm')
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='admin')
    if role not in u.roles:
        u.roles.append(role)
        db.session.commit()
    h = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(u.id), additional_claims={'roles': ['administrator']})}
    r = client.post('/api/v1/events', json={'name': 'n', 'playerAccessMode': 'shared'}, headers=h)
    assert r.status_code == 400
    r = client.post('/api/v1/events', json={'name': 'n', 'playerAccessMode': 'individual'}, headers=h)
    assert r.status_code == 201
    ind = r.get_json()['id']
    assert client.put('/api/v1/events/%d' % ind, json={'playerAccessMode': 'shared'}, headers=h).status_code == 400
    ok = client.put('/api/v1/events/%d' % ind, headers=h, json={
        'playerAccessMode': 'shared', 'validUntil': '2099-01-01T10:00:00'})
    assert ok.status_code == 200
    assert client.put('/api/v1/events/%d' % ind, json={'validUntil': None}, headers=h).status_code == 400
    assert client.put('/api/v1/events/%d' % ind, json={'name': 'renamed'}, headers=h).status_code == 200


def test_leader_enrollment_creates_device_in_own_team_only(client, ots, db, strong_key):
    SystemSettingsModel.set_setting('events_enabled', True)
    event = _event(db)
    la, lb = _user(db, 'leadera'), _user(db, 'leaderb')
    ta, _ = _team_link(db, event, 'A', leader=la)
    tb, _ = _team_link(db, event, 'B', leader=lb)
    h = _leader_headers(la, ta, event)
    r1 = client.post('/api/v1/events/my-team/enrollment', headers=h, json={'teamId': tb.id})
    r2 = client.post('/api/v1/events/my-team/enrollment', headers=h)
    assert r1.status_code == r2.status_code == 200
    assert r1.get_json()['atak']['qrString'] != r2.get_json()['atak']['qrString']
    sessions = EventSessionModel.query.filter_by(mode='device').all()
    assert len(sessions) == 2 and {s.eventTeamId for s in sessions} == {ta.id}
    assert {g for _, g, _ in ots.group_adds} == {'grp_A'}
    assert 'password' not in r1.get_data(as_text=True).lower()
    event.playerAccessMode = 'individual'
    db.session.commit()
    assert client.post('/api/v1/events/my-team/enrollment', headers=h).status_code == 409


def test_leader_enrollment_failure_discards_account(client, ots, db, strong_key):
    SystemSettingsModel.set_setting('events_enabled', True)
    event = _event(db)
    la = _user(db, 'leadera')
    ta, _ = _team_link(db, event, 'A', leader=la)
    h = _leader_headers(la, ta, event)
    ots.fail_qr = True
    r = client.post('/api/v1/events/my-team/enrollment', headers=h)
    assert r.status_code == 500
    assert getattr(ots, 'removed_users', []) == [ots.users[0][0]]
    assert EventSessionModel.query.filter_by(mode='device').count() == 0
    # OTS delete failing as well: the row stays as cleanup ledger
    ots.fail_delete = True
    assert client.post('/api/v1/events/my-team/enrollment', headers=h).status_code == 500
    assert EventSessionModel.query.filter_by(mode='device').count() == 1


def test_reissue_delete_ok_create_fails_then_reload_succeeds(client, ots, world):
    assert _join(client, world).status_code == 200
    ots.fail_qr = True
    assert _join(client, world).status_code == 500
    # reissue failure never discards the account
    assert EventSessionModel.query.filter_by(mode='device').count() == 1
    ots.fail_qr = False
    r = _join(client, world)
    assert r.status_code == 200 and r.get_json()['atak']['qrString']
    assert len(ots.users) == 1


def test_reissue_delete_raises_returns_500_and_reload_retries(client, ots, world):
    assert _join(client, world).status_code == 200
    ots.fail_qr_delete = True
    r = _join(client, world)
    # the old token may still exist: no create, account and session kept
    assert r.status_code == 500
    assert len(ots.qr_calls) == 1
    assert EventSessionModel.query.filter_by(mode='device', closedAt=None).count() == 1
    assert ots.delete_attempts == []
    ots.fail_qr_delete = False
    r = _join(client, world)
    assert r.status_code == 200 and r.get_json()['atak']['qrString']
    assert len(ots.users) == 1


def test_delete_link_keeps_sessions(client, ots, world, db):
    from flask_jwt_extended import create_access_token
    from app.models import UserRoleModel
    assert _join(client, world).status_code == 200
    u = _user(db, 'adm2')
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='admin')
    if role not in u.roles:
        u.roles.append(role)
        db.session.commit()
    h = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(u.id), additional_claims={'roles': ['administrator']})}
    sid = EventSessionModel.query.filter_by(mode='device').one().id
    r = client.delete('/api/v1/events/links/%d' % world['link'].id, headers=h)
    assert r.status_code == 200
    db.session.expire_all()
    assert EventSessionModel.query.get(sid).eventLinkId is None
    assert EventLinkModel.query.count() == 0


# ---- jobs and colours -------------------------------------------------------
def _device(db, event, team, username, mode='device', expires=None, parked=False):
    s = EventSessionModel(eventId=event.id, eventTeamId=team.id, otsUsername=username, mode=mode,
                          expiresAt=expires, parkedAt=datetime.datetime.now() if parked else None)
    db.session.add(s)
    db.session.commit()
    return s


def _expired(db, monkeypatch, app, ots, mode='device'):
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    event = _event(db, until_hours=-1)
    team, _ = _team_link(db, event)
    s = _device(db, event, team, 'evt1_abcd1234', mode=mode,
                expires=datetime.datetime.now() - timedelta(minutes=1))
    ots.users.append(('evt1_abcd1234', ['user']))
    return s


def test_close_job_deactivates_then_removes_group(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    s = EventSessionModel.query.get(s.id)
    assert s.closedAt is not None and s.closeReason == 'expired'
    assert ots.group_removes == [('evt1_abcd1234', 'grp_A', 'IN'), ('evt1_abcd1234', 'grp_A', 'OUT')]
    assert ots.deactivated == ['evt1_abcd1234']
    kinds = [k for k, _ in ots.events]
    assert kinds.index('deactivate') < kinds.index('remove')  # account first, groups best-effort


def test_close_job_delete_policy(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    s.event.accountCleanup = 'delete'
    db.session.commit()
    jobs.close_expired_event_sessions()
    assert ots.removed_users == ['evt1_abcd1234']


def test_close_job_group_failure_still_deactivates_and_is_retried(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    ots.fail_remove_group = True
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    # security-critical step done, session stays open only for the group retry
    assert ots.deactivated == ['evt1_abcd1234']
    assert EventSessionModel.query.get(s.id).closedAt is None
    ots.fail_remove_group = False
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    assert EventSessionModel.query.get(s.id).closedAt is not None
    assert ots.group_removes == [('evt1_abcd1234', 'grp_A', 'IN'), ('evt1_abcd1234', 'grp_A', 'OUT')]


def test_close_job_group_failure_gives_up_after_seven_days(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    s.expiresAt = datetime.datetime.now() - timedelta(days=8)
    db.session.commit()
    ots.fail_remove_group = True
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    s = EventSessionModel.query.get(s.id)
    assert s.closedAt is not None and 'failed' in s.closeReason
    assert ots.deactivated == ['evt1_abcd1234']


def test_close_job_delete_mode_skips_group_removal(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    s.event.accountCleanup = 'delete'
    db.session.commit()
    ots.fail_remove_group = True  # would block the session if it were attempted
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    assert ots.removed_users == ['evt1_abcd1234']
    assert ots.group_removes == []
    assert EventSessionModel.query.get(s.id).closedAt is not None


def test_close_job_legacy_shared_session_still_handled(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots, mode='shared')
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    assert EventSessionModel.query.get(s.id).closedAt is not None
    assert ots.deactivated == ['evt1_abcd1234']
    assert ots.group_removes == []  # legacy behaviour unchanged


def test_desired_colours_ignore_parked_and_uncoloured(db):
    event = _event(db)
    red, _ = _team_link(db, event, 'A', color='Red')
    plain, _ = _team_link(db, event, 'B', color=None)
    _device(db, event, red, 'dev_ok')
    _device(db, event, red, 'dev_parked', parked=True)
    _device(db, event, plain, 'dev_plain')
    _device(db, event, red, 'legacy', mode='shared')
    desired = etc._build_desired_state([event])
    assert desired == {'dev_ok': ('Red', 'Team Member'), 'legacy': ('Red', 'Team Member')}
    assert etc._unwanted_device_usernames([event], desired) == {'dev_parked', 'dev_plain'}


def test_sync_removes_colours_of_parked_device_value_checked(db, ots, monkeypatch):
    monkeypatch.setattr(SystemSettingsModel, 'get_setting', staticmethod(lambda k, d=None: True))
    event = _event(db)
    red, _ = _team_link(db, event, 'A', color='Red')
    _device(db, event, red, 'dev_ok')
    _device(db, event, red, 'dev_parked', parked=True)
    ots.euds = [eud('u1', 'dev_ok'), eud('u2', 'dev_parked')]
    ots.profiles = {('locationTeam', 'u2'): 'Red', ('atakRoleType', 'u2'): 'Team Member'}
    etc.sync_event_team_colors()
    assert ('locationTeam', 'u1', ) not in ots.deleted
    assert sorted(ots.deleted) == [('atakRoleType', 'u2'), ('locationTeam', 'u2')]
    assert ('locationTeam', 'Red', 'u1') in ots.created
    # a hand-set custom value is kept
    ots.deleted.clear()
    ots.profiles = {('locationTeam', 'u2'): 'Custom', ('atakRoleType', 'u2'): 'Medic'}
    etc.sync_event_team_colors()
    assert ots.deleted == []


# ---- close job: the account is never left usable because of colour failures --
def _open(s_id):
    db_s = EventSessionModel.query.get(s_id)
    return db_s.closedAt is None


def test_close_job_profiles_unreadable_still_deactivates(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    monkeypatch.setattr(jobs, 'read_profiles', lambda: (_ for _ in ()).throw(RuntimeError('down')))
    jobs.close_expired_event_sessions()  # existing is None
    db.session.expire_all()
    assert ots.deactivated == ['evt1_abcd1234']
    assert _open(s.id)  # kept open for the colour retry


def test_close_job_euds_raise_still_deactivates(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    ots.fail_euds = True
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    assert ots.deactivated == ['evt1_abcd1234']
    assert _open(s.id)


def test_close_job_profile_delete_failure_still_deactivates(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    ots.euds = [eud('u1', 'evt1_abcd1234')]
    ots.profiles[('locationTeam', 'u1')] = 'Red'
    ots.fail_profile_delete = True
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    assert ots.deactivated == ['evt1_abcd1234']
    assert _open(s.id)


def test_close_job_delete_mode_colour_failure_deactivates_instead(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    s.event.accountCleanup = 'delete'
    db.session.commit()
    ots.fail_euds = True
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    assert not getattr(ots, 'removed_users', [])
    assert ots.deactivated == ['evt1_abcd1234']
    assert _open(s.id)


def test_close_job_delete_mode_delete_failure_deactivates(db, ots, app, monkeypatch):
    s = _expired(db, monkeypatch, app, ots)
    s.event.accountCleanup = 'delete'
    db.session.commit()
    ots.fail_delete = True
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    assert ots.deactivated == ['evt1_abcd1234']
    assert _open(s.id)
