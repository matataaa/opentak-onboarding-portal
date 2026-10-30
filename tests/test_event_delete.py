"""
Deleting events/teams must handle child rows explicitly (PostgreSQL enforces the
non-cascading FKs, SQLite does not, so row counts are asserted).
"""
from datetime import datetime

import pytest
from flask_jwt_extended import create_access_token

from app.models import (EventLinkModel, EventModel, EventSessionModel, EventTeamModel,
                        SystemSettingsModel, UserModel, UserRoleModel)


@pytest.fixture
def admin_headers(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    admin = UserModel.query.filter_by(username='evtdeladmin').first() or \
        UserModel.create_user(username='evtdeladmin', email='evtdeladmin@example.com')
    if role not in admin.roles:
        admin.roles.append(role)
        db.session.commit()
    token = create_access_token(identity=str(admin.id), additional_claims={'roles': ['administrator']})
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture(autouse=True)
def _clean_events(db):
    """The endpoints commit, so remove leftovers to keep other tests isolated."""
    old_enabled = SystemSettingsModel.get_setting('events_enabled', False)
    SystemSettingsModel.set_setting('events_enabled', True)
    yield
    db.session.rollback()
    for model in (EventSessionModel, EventLinkModel, EventModel, EventTeamModel):
        model.query.delete()
    db.session.commit()
    SystemSettingsModel.set_setting('events_enabled', old_enabled)


def _event(db, name):
    ev = EventModel(name=name)
    db.session.add(ev)
    db.session.flush()
    return ev


def _team(db, name):
    t = EventTeamModel(name=name)
    db.session.add(t)
    db.session.flush()
    return t


def _link(db, ev, team, role='player'):
    db.session.add(EventLinkModel(eventId=ev.id, eventTeamId=team.id, role=role,
                                  tokenHash=f'h-{ev.id}-{team.id}-{role}'))


def _session(db, ev, team, closed):
    db.session.add(EventSessionModel(eventId=ev.id, eventTeamId=team.id, otsUsername='x',
                                     closedAt=datetime.utcnow() if closed else None))


def _counts(db, ev_ids, team_ids):
    return (
        EventModel.query.filter(EventModel.id.in_(ev_ids)).count(),
        EventTeamModel.query.filter(EventTeamModel.id.in_(team_ids)).count(),
        EventLinkModel.query.filter(EventLinkModel.eventId.in_(ev_ids)).count(),
        EventSessionModel.query.filter(EventSessionModel.eventId.in_(ev_ids)).count(),
    )


def test_delete_event_open_session_409(client, db, admin_headers):
    ev, team = _event(db, 'e1'), _team(db, 't1')
    _link(db, ev, team)
    _session(db, ev, team, closed=True)
    _session(db, ev, team, closed=False)
    db.session.commit()
    r = client.delete(f'/api/v1/events/{ev.id}', headers=admin_headers)
    assert r.status_code == 409
    assert 'open sessions' in r.get_json()['error']
    assert _counts(db, [ev.id], [team.id]) == (1, 1, 1, 2)


def test_delete_event_closed_sessions_and_links(client, db, admin_headers):
    ev, other, team = _event(db, 'e2'), _event(db, 'e2other'), _team(db, 't2')
    _link(db, ev, team, 'leader')
    _link(db, ev, team, 'player')
    _link(db, other, team)
    _session(db, ev, team, closed=True)
    _session(db, other, team, closed=False)
    db.session.commit()
    r = client.delete(f'/api/v1/events/{ev.id}', headers=admin_headers)
    assert r.status_code == 200
    assert _counts(db, [ev.id], [team.id]) == (0, 1, 0, 0)
    assert _counts(db, [other.id], [team.id]) == (1, 1, 1, 1)


def test_delete_event_without_children(client, db, admin_headers):
    ev = _event(db, 'e3')
    db.session.commit()
    assert client.delete(f'/api/v1/events/{ev.id}', headers=admin_headers).status_code == 200
    assert EventModel.query.filter_by(id=ev.id).count() == 0


def test_delete_team_open_session_409(client, db, admin_headers):
    ev, team = _event(db, 'e4'), _team(db, 't4')
    _link(db, ev, team)
    _session(db, ev, team, closed=False)
    db.session.commit()
    r = client.delete(f'/api/v1/events/teams/{team.id}', headers=admin_headers)
    assert r.status_code == 409
    assert 'open sessions' in r.get_json()['error']
    assert _counts(db, [ev.id], [team.id]) == (1, 1, 1, 1)


def test_delete_team_across_several_events(client, db, admin_headers):
    e1, e2, team, keep = _event(db, 'e5a'), _event(db, 'e5b'), _team(db, 't5'), _team(db, 't5keep')
    for e in (e1, e2):
        _link(db, e, team)
        _session(db, e, team, closed=True)
    _link(db, e1, keep)
    _session(db, e1, keep, closed=False)
    db.session.commit()
    r = client.delete(f'/api/v1/events/teams/{team.id}', headers=admin_headers)
    assert r.status_code == 200
    assert EventTeamModel.query.filter_by(id=team.id).count() == 0
    assert EventLinkModel.query.filter_by(eventTeamId=team.id).count() == 0
    assert EventSessionModel.query.filter_by(eventTeamId=team.id).count() == 0
    assert EventLinkModel.query.filter_by(eventTeamId=keep.id).count() == 1
    assert EventSessionModel.query.filter_by(eventTeamId=keep.id).count() == 1
    assert EventModel.query.filter(EventModel.id.in_([e1.id, e2.id])).count() == 2


def test_delete_team_without_children(client, db, admin_headers):
    team = _team(db, 't6')
    db.session.commit()
    assert client.delete(f'/api/v1/events/teams/{team.id}', headers=admin_headers).status_code == 200
    assert EventTeamModel.query.filter_by(id=team.id).count() == 0


def test_shortening_end_moves_open_sessions_then_delete_works(app, client, db, admin_headers, monkeypatch):
    from datetime import timedelta
    from app import jobs
    from app.extensions import scheduler
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    ev, team = _event(db, 'e7'), _team(db, 't7')
    ev.validUntil = datetime.now() + timedelta(days=1)
    db.session.add(EventSessionModel(eventId=ev.id, eventTeamId=team.id, otsUsername=None,
                                     mode='individual', expiresAt=ev.validUntil))
    db.session.commit()
    assert client.delete(f'/api/v1/events/{ev.id}', headers=admin_headers).status_code == 409
    past = datetime.now() - timedelta(hours=1)
    r = client.put(f'/api/v1/events/{ev.id}', headers=admin_headers,
                   json={'validUntil': past.isoformat()})
    assert r.status_code == 200
    sess = EventSessionModel.query.filter_by(eventId=ev.id).one()
    assert sess.expiresAt is not None and sess.expiresAt < datetime.now()
    jobs.close_expired_event_sessions()
    db.session.expire_all()
    assert EventSessionModel.query.filter_by(eventId=ev.id).one().closedAt is not None
    assert client.delete(f'/api/v1/events/{ev.id}', headers=admin_headers).status_code == 200
    assert _counts(db, [ev.id], [team.id]) == (0, 1, 0, 0)


def test_clearing_end_keeps_session_expiry(client, db, admin_headers):
    from datetime import timedelta
    ev, team = _event(db, 'e8'), _team(db, 't8')
    exp = datetime.now() + timedelta(days=1)
    db.session.add(EventSessionModel(eventId=ev.id, eventTeamId=team.id, expiresAt=exp))
    db.session.commit()
    r = client.put(f'/api/v1/events/{ev.id}', headers=admin_headers, json={'validUntil': None})
    assert r.status_code == 200
    assert EventSessionModel.query.filter_by(eventId=ev.id).one().expiresAt == exp


def test_delete_logs_failed_ots_cleanup(client, db, admin_headers, caplog):
    ev, team = _event(db, 'e9'), _team(db, 't9')
    db.session.add(EventSessionModel(eventId=ev.id, eventTeamId=team.id, otsUsername='evt9_t9',
                                     closedAt=datetime.utcnow(), closeReason='expired (OTS cleanup failed)'))
    db.session.commit()
    with caplog.at_level('WARNING'):
        assert client.delete(f'/api/v1/events/{ev.id}', headers=admin_headers).status_code == 200
    assert 'evt9_t9' in caplog.text
