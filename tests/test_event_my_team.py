"""
Leader own-team view: scope comes only from the leader JWT claims, access is
re-validated on every call, and only the team's player link / shared ATAK
enrollment are exposed.
"""
import datetime

import pytest
from flask_jwt_extended import create_access_token

from app.api_v1 import events as events_mod
from app.models import (EventLinkModel, EventModel, EventSessionModel, EventTeamModel, OTSGroupModel,
                        SystemSettingsModel, UserModel, UserRoleModel)

STRONG = 'a-strong-secret-key-for-tests-0123456789'


class FakeOTS:
    def __init__(self):
        self.atak_calls = []
        self.fail_atak = False

    def create_user(self, *a, **k):
        return {}

    def activate_user(self, *a, **k):
        return {}

    def add_user_to_group(self, *a, **k):
        return {}

    def delete_user(self, *a, **k):
        return {}

    def create_atak_qr_string(self, username=None, exp=None, nbf=None, max_uses=None):
        if self.fail_atak:
            raise RuntimeError('https://ots.internal/secret boom')
        self.atak_calls.append({'username': username, 'exp': exp, 'max_uses': max_uses})
        return {'response': {'qr_string': 'tak://enroll?x=1'}}


@pytest.fixture
def ots(monkeypatch):
    fake = FakeOTS()
    monkeypatch.setattr(events_mod, 'otsClient', fake)
    return fake


@pytest.fixture
def strong_key(app):
    old = app.config.get('SECRET_KEY')
    app.config['SECRET_KEY'] = STRONG
    yield
    app.config['SECRET_KEY'] = old


@pytest.fixture(autouse=True)
def _clean(db):
    yield
    db.session.rollback()
    for model in (EventSessionModel, EventLinkModel, EventTeamModel, EventModel):
        model.query.delete()
    db.session.commit()


def _user(db, name):
    return UserModel.query.filter_by(username=name).first() or \
        UserModel.create_user(username=name, email=f'{name}@example.com')


def _team(db, name, leader, event, mode_link=True, retrievable=True):
    # Device accounts need a usable team group
    group = OTSGroupModel.query.filter_by(name='mt_' + name).first() or OTSGroupModel(name='mt_' + name)
    db.session.add(group)
    db.session.flush()
    team = EventTeamModel(name=name, leaderUserId=leader.id, otsGroupId=group.id)
    db.session.add(team)
    db.session.commit()
    link = None
    if mode_link:
        link = EventLinkModel(eventId=event.id, eventTeamId=team.id, role='player',
                              tokenHash='x', tokenVersion=1,
                              tokenNonce='nonce-' + name if retrievable else None)
        db.session.add(link)
        db.session.commit()
        if retrievable:
            link.tokenHash = events_mod._hash_token(events_mod._derive_link_token(link))
        else:
            link.tokenHash = 'legacy-' + name
        db.session.commit()
    return team, link


def _leader_link(team, event):
    from app.models import db
    link = EventLinkModel.query.filter_by(eventTeamId=team.id, role='leader').first()
    if not link:
        link = EventLinkModel(eventId=event.id, eventTeamId=team.id, role='leader',
                              tokenHash=events_mod._hash_token('leader-tok-%d' % team.id),
                              tokenVersion=1, tokenNonce='n')
        db.session.add(link)
        db.session.commit()
    return link


def _leader_headers(user, team, event, **extra):
    link = _leader_link(team, event)
    claims = {'event_leader_team': team.id, 'event_leader_event': event.id,
              'event_leader_until': 9999999999,
              'event_leader_link': link.id, 'event_leader_link_version': link.tokenVersion,
              'event_leader_link_nonce': link.tokenNonce}
    claims.update(extra)
    return {'Authorization': 'Bearer ' + create_access_token(
        identity=str(user.id), additional_claims=claims)}


@pytest.fixture
def world(db, strong_key):
    SystemSettingsModel.set_setting('events_enabled', True)
    now = datetime.datetime.now()
    event = EventModel(name='ev', playerAccessMode='shared',
                       validFrom=now - datetime.timedelta(hours=1),
                       validUntil=now + datetime.timedelta(hours=5))
    db.session.add(event)
    db.session.commit()
    la, lb = _user(db, 'leadera'), _user(db, 'leaderb')
    ta, link_a = _team(db, 'A', la, event)
    tb, link_b = _team(db, 'B', lb, event)
    return dict(event=event, la=la, lb=lb, ta=ta, tb=tb, link_a=link_a, link_b=link_b)


def test_leader_sees_own_team_link(client, strong_key, world):
    w = world
    r = client.get('/api/v1/events/my-team', headers=_leader_headers(w['la'], w['ta'], w['event']))
    assert r.status_code == 200
    d = r.get_json()
    assert d['team']['id'] == w['ta'].id
    assert d['mode'] == 'shared' and d['atakAvailable'] is True
    assert list(d['playerLink'].keys()) == ['url']
    assert d['playerLink']['url'].endswith('/events/join/' + events_mod._derive_link_token(w['link_a']))
    assert 'reason' not in d


def test_ids_in_query_and_body_ignored(client, strong_key, world):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'])
    r = client.get(f"/api/v1/events/my-team?teamId={w['tb'].id}&eventId=999", headers=h,
                   json={'teamId': w['tb'].id})
    assert r.get_json()['team']['id'] == w['ta'].id
    assert w['ta'].id != w['tb'].id


def test_team_b_leader_never_sees_team_a(client, strong_key, world):
    w = world
    r = client.get('/api/v1/events/my-team', headers=_leader_headers(w['lb'], w['tb'], w['event']))
    d = r.get_json()
    assert d['team']['id'] == w['tb'].id
    assert d['playerLink']['url'].endswith(events_mod._derive_link_token(w['link_b']))
    assert events_mod._derive_link_token(w['link_a']) not in d['playerLink']['url']
    # a token claiming team A but belonging to leader B is rejected
    r = client.get('/api/v1/events/my-team', headers=_leader_headers(w['lb'], w['ta'], w['event']))
    assert r.status_code == 403


def test_normal_user_and_admin_without_claims_forbidden(client, db, world):
    u = _user(db, 'plainuser')
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='admin')
    if role not in u.roles:
        u.roles.append(role)
        db.session.commit()
    h = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(u.id), additional_claims={'roles': ['administrator']})}
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403
    assert client.post('/api/v1/events/my-team/enrollment', headers=h).status_code == 403
    h2 = {'Authorization': 'Bearer ' + create_access_token(identity=str(world['la'].id))}
    assert client.get('/api/v1/events/my-team', headers=h2).status_code == 403


def test_access_ends_on_event_end_deactivation_reassignment(client, db, strong_key, world):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'])
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 200

    w['event'].active = False
    db.session.commit()
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403
    w['event'].active = True
    w['event'].validUntil = datetime.datetime.now() - datetime.timedelta(minutes=1)
    db.session.commit()
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403
    w['event'].validUntil = datetime.datetime.now() + datetime.timedelta(hours=1)
    db.session.commit()
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 200

    w['ta'].leaderUserId = w['lb'].id
    db.session.commit()
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403
    w['ta'].leaderUserId = None
    db.session.commit()
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403


def test_weak_key_returns_null_link_with_reason(client, app, world):
    w = world
    app.config['SECRET_KEY'] = 'dev'
    d = client.get('/api/v1/events/my-team',
                   headers=_leader_headers(w['la'], w['ta'], w['event'])).get_json()
    assert d['playerLink'] is None and d['reason'] == 'notRetrievable'


def test_legacy_link_not_retrievable(client, db, strong_key, world):
    w = world
    team, _ = _team(db, 'L', _user(db, 'leaderl'), w['event'], retrievable=False)
    d = client.get('/api/v1/events/my-team',
                   headers=_leader_headers(_user(db, 'leaderl'), team, w['event'])).get_json()
    assert d['playerLink'] is None and d['reason'] == 'notRetrievable'


def test_missing_link(client, db, strong_key, world):
    w = world
    team, _ = _team(db, 'M', _user(db, 'leaderm'), w['event'], mode_link=False)
    d = client.get('/api/v1/events/my-team',
                   headers=_leader_headers(_user(db, 'leaderm'), team, w['event'])).get_json()
    assert d['playerLink'] is None and d['reason'] == 'missing'


def test_individual_mode(client, db, strong_key, world, ots):
    w = world
    w['event'].playerAccessMode = 'individual'
    db.session.commit()
    h = _leader_headers(w['la'], w['ta'], w['event'])
    d = client.get('/api/v1/events/my-team', headers=h).get_json()
    assert d['mode'] == 'individual' and d['atakAvailable'] is False
    assert client.post('/api/v1/events/my-team/enrollment', headers=h).status_code == 409
    assert ots.atak_calls == []


def test_shared_enrollment(client, strong_key, world, ots):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'])
    r = client.post('/api/v1/events/my-team/enrollment', headers=h)
    assert r.status_code == 200
    d = r.get_json()
    assert list(d.keys()) == ['atak']
    assert d['atak']['qrString'] == 'tak://enroll?x=1'
    assert 'password' not in r.get_data(as_text=True).lower()
    call = ots.atak_calls[0]
    # one new account per press (details in test_event_device_accounts.py)
    assert call['username'].startswith('evt%d_' % w['event'].id)
    assert call['max_uses'] == events_mod.DEVICE_ENROLLMENT_MAX_USES
    assert call['exp'] == d['atak']['expiresAt']
    import time
    assert abs((call['exp'] - time.time()) - events_mod.DEVICE_ENROLLMENT_TTL_SECONDS) < 5


def test_enrollment_ots_failure(client, db, strong_key, world, ots):
    w = world
    ots.fail_atak = True
    r = client.post('/api/v1/events/my-team/enrollment',
                    headers=_leader_headers(w['la'], w['ta'], w['event']))
    assert r.status_code == 500
    assert 'ots.internal' not in r.get_data(as_text=True)
    assert r.get_json() == {'error': 'Could not generate TAK enrollment'}


# --- leader link binding, events switch, refresh ---------------------------
def _join_leader(client, w):
    link = _leader_link(w['ta'], w['event'])
    r = client.post('/api/v1/events/join/leader-tok-%d' % w['ta'].id)
    assert r.status_code == 200, r.get_data(as_text=True)
    return link, r.get_json()


def _bearer(tok):
    return {'Authorization': 'Bearer ' + tok}


def test_regenerated_leader_link_ends_session(client, db, strong_key, world):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'])
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 200
    link = _leader_link(w['ta'], w['event'])
    link.tokenVersion += 1
    db.session.commit()
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403
    assert client.post('/api/v1/events/my-team/enrollment', headers=h).status_code == 403


def test_deleted_leader_link_ends_session(client, db, strong_key, world):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'])
    db.session.delete(_leader_link(w['ta'], w['event']))
    db.session.commit()
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403


def test_legacy_token_without_link_claims_forbidden(client, strong_key, world):
    w = world
    h = {'Authorization': 'Bearer ' + create_access_token(
        identity=str(w['la'].id),
        additional_claims={'event_leader_team': w['ta'].id, 'event_leader_event': w['event'].id,
                           'event_leader_until': 9999999999})}
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403


def test_link_claim_must_match_leader_link_of_team(client, strong_key, world):
    w = world
    # claims point at team B's leader link while naming team A
    other = _leader_link(w['tb'], w['event'])
    h = _leader_headers(w['la'], w['ta'], w['event'], event_leader_link=other.id)
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403
    # a player link id is never accepted
    h = _leader_headers(w['la'], w['ta'], w['event'], event_leader_link=w['link_a'].id)
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403


def test_events_disabled_forbidden(client, strong_key, world):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'])
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 200
    SystemSettingsModel.set_setting('events_enabled', False)
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403
    assert client.post('/api/v1/events/my-team/enrollment', headers=h).status_code == 403


def test_join_refresh_round_trip_keeps_scope(client, db, strong_key, world):
    w = world
    link, data = _join_leader(client, w)
    assert client.get('/api/v1/events/my-team',
                      headers=_bearer(data['access_token'])).status_code == 200
    r = client.post('/api/v1/auth/refresh', headers=_bearer(data['refresh_token']))
    assert r.status_code == 200
    new_access = r.get_json()['access_token']
    r = client.get('/api/v1/events/my-team', headers=_bearer(new_access))
    assert r.status_code == 200 and r.get_json()['team']['id'] == w['ta'].id
    # regenerating the leader link also kills refreshed tokens
    link.tokenVersion += 1
    db.session.commit()
    assert client.get('/api/v1/events/my-team', headers=_bearer(new_access)).status_code == 403


def test_refresh_token_rejected_on_my_team(client, strong_key, world):
    w = world
    _link, data = _join_leader(client, w)
    assert client.get('/api/v1/events/my-team',
                      headers=_bearer(data['refresh_token'])).status_code in (401, 422)
    assert client.post('/api/v1/events/my-team/enrollment',
                       headers=_bearer(data['refresh_token'])).status_code in (401, 422)


# --- _join_as_player_shared error contract (unchanged by the refactor) ------
def _join_shared(client, w):
    from app.models import db
    token = 'player-tok'
    w['link_a'].tokenHash = events_mod._hash_token(token)
    db.session.commit()
    return client.post('/api/v1/events/join/' + token)


def test_shared_join_error_contract(client, db, strong_key, world, ots, monkeypatch):
    w = world
    # OTS enrollment failure -> 500 'Could not generate TAK enrollment'
    ots.fail_atak = True
    r = _join_shared(client, w)
    assert r.status_code == 500
    assert r.get_json() == {'error': 'Could not generate TAK enrollment'}
    ots.fail_atak = False
    EventSessionModel.query.delete()  # force account provisioning on the next join
    db.session.commit()

    # provisioning failure -> 500 'Could not provision team access'
    def boom(*a, **k):
        raise RuntimeError('ots down')
    monkeypatch.setattr(ots, 'create_user', boom)
    r = _join_shared(client, w)
    assert r.status_code == 500
    assert r.get_json() == {'error': 'Could not provision team access'}
    monkeypatch.undo()

    # event ended between window check and enrollment -> 403 'This event has ended'
    monkeypatch.setattr(events_mod, '_event_window_error', lambda ev: None)
    persisted = EventSessionModel(eventId=w['event'].id, eventTeamId=w['ta'].id,
                                  otsUsername='evt_x', mode='device')
    db.session.add(persisted)
    db.session.commit()
    monkeypatch.setattr(events_mod, '_create_device_account',
                        lambda ev, tm, link=None, client_key=None: (persisted, None))
    w['event'].validUntil = datetime.datetime.now() - datetime.timedelta(minutes=1)
    db.session.commit()
    r = _join_shared(client, w)
    assert r.status_code == 403
    assert r.get_json() == {'error': 'This event has ended'}


# --- leader session bound to the link nonce ---------------------------------
def _both_403(client, h):
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 403
    assert client.post('/api/v1/events/my-team/enrollment', headers=h).status_code == 403


def test_recreated_link_with_same_id_and_version_ends_session(client, db, strong_key, world):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'])
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 200
    old = _leader_link(w['ta'], w['event'])
    old_id = old.id
    db.session.delete(old)
    db.session.commit()
    # simulate SQLite id reuse: same id, tokenVersion=1, fresh nonce
    db.session.add(EventLinkModel(id=old_id, eventId=w['event'].id, eventTeamId=w['ta'].id,
                                  role='leader', tokenHash=events_mod._hash_token('new-leader'),
                                  tokenVersion=1, tokenNonce='fresh-nonce'))
    db.session.commit()
    _both_403(client, h)


def _admin_headers(db):
    u = _user(db, 'myteamadmin')
    role = UserRoleModel.query.filter_by(name='events_admin').first() or \
        UserRoleModel.create_role(name='events_admin', description='events admin')
    if role not in u.roles:
        u.roles.append(role)
        db.session.commit()
    return {'Authorization': 'Bearer ' + create_access_token(
        identity=str(u.id), additional_claims={'roles': ['events_admin']})}


def test_real_revoke_and_delete_endpoints_end_session(client, db, strong_key, world):
    w = world
    admin = _admin_headers(db)
    h = _leader_headers(w['la'], w['ta'], w['event'])
    link = _leader_link(w['ta'], w['event'])
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 200
    assert client.post(f'/api/v1/events/links/{link.id}/revoke', headers=admin).status_code == 200
    _both_403(client, h)

    # a session joined on the regenerated link works, then dies on delete
    db.session.expire_all()
    link = EventLinkModel.query.get(link.id)
    r = client.post('/api/v1/events/join/' + events_mod._derive_link_token(link))
    assert r.status_code == 200
    h2 = _bearer(r.get_json()['access_token'])
    assert client.get('/api/v1/events/my-team', headers=h2).status_code == 200
    assert client.delete(f'/api/v1/events/links/{link.id}', headers=admin).status_code == 200
    _both_403(client, h2)


def test_wrong_event_claim_forbidden(client, db, strong_key, world):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'], event_leader_event=w['event'].id + 999)
    _both_403(client, h)


def test_legacy_null_nonce_link_joins_and_is_bound(client, db, strong_key, world):
    w = world
    link = _leader_link(w['ta'], w['event'])
    link.tokenNonce = None
    db.session.commit()
    old_hash = link.tokenHash
    r = client.post('/api/v1/events/join/leader-tok-%d' % w['ta'].id)
    assert r.status_code == 200
    db.session.expire_all()
    link = EventLinkModel.query.get(link.id)
    assert link.tokenNonce and link.tokenHash == old_hash
    assert not events_mod._link_retrievable(link)
    h = _bearer(r.get_json()['access_token'])
    assert client.get('/api/v1/events/my-team', headers=h).status_code == 200
    link.tokenVersion += 1
    link.tokenNonce = 'regenerated'
    db.session.commit()
    _both_403(client, h)


def test_token_without_nonce_claim_forbidden(client, strong_key, world):
    w = world
    h = _leader_headers(w['la'], w['ta'], w['event'], event_leader_link_nonce=None)
    _both_403(client, h)
