"""
Tests for queued announcement email delivery (retry, partial progress, admin visibility)
"""
import json
import smtplib
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app import jobs
from app import email as email_module
from app import notifications
from app.extensions import scheduler
from app.models import (
    db as _db, AnnouncementModel, SystemSettingsModel, UserModel, UserRoleModel,
)


class FakeMail:
    """Stands in for flask-mail's `mail.send`; `behaviour` decides per recipient"""

    def __init__(self):
        self.sent = []
        self.behaviour = lambda recipient: None

    def send(self, msg):
        recipient = msg.recipients[0]
        self.behaviour(recipient)
        self.sent.append(recipient)


@pytest.fixture
def mail(monkeypatch, app):
    fake = FakeMail()
    monkeypatch.setattr(email_module.mail, 'send', fake.send)
    monkeypatch.setattr(email_module, 'MAIL_ENABLED', True)
    monkeypatch.setattr(jobs, 'MAIL_ENABLED', True)
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    return fake


@pytest.fixture
def data(app):
    """Removes only the rows created during the test."""
    models = (AnnouncementModel, UserModel, UserRoleModel, SystemSettingsModel)
    with app.app_context():
        before = {m: {o.id for o in m.query.all()} for m in models}
        yield _db
        _db.session.rollback()
        for m in models:
            for obj in m.query.all():
                if obj.id not in before[m]:
                    _db.session.delete(obj)
        _db.session.commit()


def add_user(name, email='default', roles=()):
    if email == 'default':
        email = f'{name}@example.com'
    user = UserModel(username=name, email=email)
    for r in roles:
        user.roles.append(r)
    _db.session.add(user)
    _db.session.commit()
    return user


def add_announcement(creator, status='sent', send_email=True, scheduled_at=None, target='all', users=()):
    a = AnnouncementModel(title='Hello', content='<p>Body</p>', target_type=target, status=status,
                          send_email=send_email, scheduled_at=scheduled_at, created_by=creator.id)
    for u in users:
        a.target_users.append(u)
    _db.session.add(a)
    _db.session.commit()
    return a


def pending(a):
    return json.loads(a.email_pending_user_ids)


def make_due(a_id):
    a = _db.session.get(AnnouncementModel, a_id)
    a.email_next_attempt_at = datetime.now() - timedelta(seconds=1)
    _db.session.commit()


def refresh(a_id):
    _db.session.expire_all()
    return _db.session.get(AnnouncementModel, a_id)


def queued_announcement(admin, users, **kw):
    """Scheduled announcement targeted at `users`, published + queued by phase A"""
    a = add_announcement(admin, status='scheduled', target='users', users=users,
                         scheduled_at=datetime.now() - timedelta(minutes=1), **kw)
    return a.id


class TestDelivery:
    def test_scheduled_published_queued_then_sent(self, data, mail):
        admin = add_user('zadmin')
        u1, u2 = add_user('zu1'), add_user('zu2')
        a_id = queued_announcement(admin, [u1, u2])
        # Phase A queues; phase B in the same run can already send
        jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert a.status == 'sent' and a.sent_at is not None
        assert sorted(mail.sent) == ['zu1@example.com', 'zu2@example.com']
        assert a.email_status == 'sent'
        assert pending(a) == []
        assert a.email_last_error is None

    def test_publish_phase_queues_snapshot_with_addresses_only(self, data, mail, monkeypatch):
        admin = add_user('zadmin')
        u1, u2 = add_user('zu1'), add_user('zu2', email=None)
        a_id = queued_announcement(admin, [u1, u2])
        monkeypatch.setattr(AnnouncementModel, 'get_email_due', staticmethod(lambda: []))
        jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert a.status == 'sent'
        assert a.email_status == 'pending'
        assert pending(a) == [u1.id]
        assert mail.sent == []

    def test_connection_error_aborts_run_and_schedules_retry(self, data, mail):
        admin = add_user('zadmin')
        users = [add_user('zu1'), add_user('zu2'), add_user('zu3')]
        ids = [u.id for u in users]
        a_id = queued_announcement(admin, users)

        def boom(recipient):
            raise smtplib.SMTPServerDisconnected('connection lost')
        calls = []
        mail.behaviour = lambda r: (calls.append(r), boom(r))
        jobs.send_scheduled_announcements()

        a = refresh(a_id)
        assert len(calls) == 1
        assert a.email_attempts == 1
        assert a.email_status == 'pending'
        assert 'connection lost' in a.email_last_error
        assert sorted(pending(a)) == sorted(ids)
        delta = a.email_next_attempt_at - datetime.now()
        assert timedelta(seconds=30) < delta <= timedelta(minutes=1)
        # still published in-app
        assert a.status == 'sent'
        assert a in AnnouncementModel.get_user_announcements(users[0].id)

    def test_retry_waits_then_mails_only_pending(self, data, mail):
        admin = add_user('zadmin')
        u1, u2, u3 = add_user('zu1'), add_user('zu2'), add_user('zu3')
        a_id = queued_announcement(admin, [u1, u2, u3])

        def second_fails(recipient):
            if recipient == 'zu2@example.com':
                raise smtplib.SMTPServerDisconnected('down')
        mail.behaviour = second_fails
        jobs.send_scheduled_announcements()
        first_run = list(mail.sent)
        assert len(first_run) == 1
        a = refresh(a_id)
        assert sorted(pending(a)) == sorted([u2.id, u3.id])

        # not due yet: nothing is sent
        mail.sent.clear()
        mail.behaviour = lambda r: None
        jobs.send_scheduled_announcements()
        assert mail.sent == []

        make_due(a_id)
        jobs.send_scheduled_announcements()
        assert sorted(mail.sent) == ['zu2@example.com', 'zu3@example.com']
        assert first_run[0] not in mail.sent
        a = refresh(a_id)
        assert a.email_status == 'sent' and pending(a) == []

    def test_refused_recipient_dropped_and_recorded(self, data, mail):
        admin = add_user('zadmin')
        u1, u2, u3 = add_user('zu1'), add_user('zu2'), add_user('zu3')
        a_id = queued_announcement(admin, [u1, u2, u3])

        def refuse(recipient):
            if recipient == 'zu2@example.com':
                raise smtplib.SMTPRecipientsRefused({recipient: (550, b'no such user')})
        mail.behaviour = refuse
        jobs.send_scheduled_announcements()

        a = refresh(a_id)
        assert sorted(mail.sent) == ['zu1@example.com', 'zu3@example.com']
        assert a.email_status == 'sent'
        assert pending(a) == []
        assert a.email_attempts == 0
        failures = json.loads(a.email_failures)
        assert len(failures) == 1 and failures[0]['userId'] == u2.id
        assert '550' in failures[0]['error']

    def test_six_failures_end_in_failed(self, data, mail):
        admin = add_user('zadmin')
        u1 = add_user('zu1')
        a_id = queued_announcement(admin, [u1])

        def boom(recipient):
            raise smtplib.SMTPServerDisconnected('down')
        mail.behaviour = boom
        expected_minutes = [1, 5, 15, 60, 240]
        jobs.send_scheduled_announcements()
        for attempt, minutes in enumerate(expected_minutes, start=1):
            a = refresh(a_id)
            assert a.email_attempts == attempt and a.email_status == 'pending'
            delta = a.email_next_attempt_at - datetime.now()
            assert timedelta(minutes=minutes) - timedelta(seconds=30) < delta <= timedelta(minutes=minutes)
            make_due(a_id)
            jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert a.email_attempts == 6
        assert a.email_status == 'failed'
        assert pending(a) == [u1.id]

        mail.sent.clear()
        calls = []
        mail.behaviour = lambda r: calls.append(r)
        jobs.send_scheduled_announcements()
        assert calls == [] and mail.sent == []

    def test_whole_announcement_error_counts_as_attempt(self, data, mail, monkeypatch):
        admin = add_user('zadmin')
        a_id = queued_announcement(admin, [add_user('zu1')])

        def render_error(*args, **kwargs):
            raise RuntimeError('template exploded')
        monkeypatch.setattr(email_module, 'render_template', render_error)
        jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert a.email_attempts == 1 and a.email_status == 'pending'
        assert 'template exploded' in a.email_last_error
        assert a.email_next_attempt_at is not None

    def test_deleted_user_dropped_silently(self, data, mail, monkeypatch):
        admin = add_user('zadmin')
        u1, u2 = add_user('zu1'), add_user('zu2')
        a_id = queued_announcement(admin, [u1, u2])
        with monkeypatch.context() as m:
            m.setattr(AnnouncementModel, 'get_email_due', staticmethod(lambda: []))
            jobs.send_scheduled_announcements()
        a = refresh(a_id)
        a.target_users.remove(u2)
        _db.session.delete(u2)
        _db.session.commit()
        jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert mail.sent == ['zu1@example.com']
        assert a.email_status == 'sent' and pending(a) == []
        assert a.email_failures is None

    def test_mail_disabled_is_skipped(self, data, mail, monkeypatch):
        monkeypatch.setattr(jobs, 'MAIL_ENABLED', False)
        monkeypatch.setattr(email_module, 'MAIL_ENABLED', False)
        admin = add_user('zadmin')
        a_id = queued_announcement(admin, [add_user('zu1')])
        jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert mail.sent == []
        assert a.email_status == 'skipped'
        assert pending(a) == []
        assert a.email_attempts == 0

    def test_send_email_false_never_queued(self, data, mail):
        admin = add_user('zadmin')
        a = add_announcement(admin, status='scheduled', send_email=False, target='users',
                             users=[add_user('zu1')], scheduled_at=datetime.now() - timedelta(minutes=1))
        a_id = a.id
        jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert a.status == 'sent'
        assert a.email_status is None
        assert mail.sent == []
        assert a not in AnnouncementModel.get_email_due()


def headers_for(user):
    return {'Authorization': f'Bearer {create_access_token(identity=str(user.id))}'}


class TestDropAndLease:
    def test_smtp_5xx_drops_recipient_and_continues(self, data, mail):
        admin = add_user('zadmin')
        u1, u2, u3 = add_user('zu1'), add_user('zu2'), add_user('zu3')
        a_id = queued_announcement(admin, [u1, u2, u3])

        def reject(recipient):
            if recipient == 'zu1@example.com':
                raise smtplib.SMTPDataError(554, b'rejected')
        mail.behaviour = reject
        jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert sorted(mail.sent) == ['zu2@example.com', 'zu3@example.com']
        assert a.email_status == 'sent' and a.email_attempts == 0
        failures = json.loads(a.email_failures)
        assert [f['userId'] for f in failures] == [u1.id]

    def test_sender_refused_aborts_run(self, data, mail):
        admin = add_user('zadmin')
        users = [add_user('zu1'), add_user('zu2'), add_user('zu3')]
        a_id = queued_announcement(admin, users)
        calls = []

        def refuse_sender(recipient):
            calls.append(recipient)
            raise smtplib.SMTPSenderRefused(550, b'bad sender', 'from@example.com')
        mail.behaviour = refuse_sender
        jobs.send_scheduled_announcements()
        a = refresh(a_id)
        assert len(calls) == 1
        assert a.email_attempts == 1 and a.email_status == 'pending'
        assert len(pending(a)) == 3 and a.email_failures is None

    def test_failing_progress_commit_does_not_remail(self, data, mail, monkeypatch):
        admin = add_user('zadmin')
        u1, u2 = add_user('zu1'), add_user('zu2')
        a_id = queued_announcement(admin, [u1, u2])
        real_commit = _db.session.commit
        state = {'armed': False}

        def flaky_commit():
            if state['armed'] and mail.sent:
                raise RuntimeError('db gone')
            real_commit()
        original_send = mail.send

        def send_and_arm(msg):
            original_send(msg)
            state['armed'] = True
        with monkeypatch.context() as m:
            m.setattr(email_module.mail, 'send', send_and_arm)
            m.setattr(_db.session, 'commit', flaky_commit)
            jobs.send_scheduled_announcements()
        assert len(mail.sent) == 1
        first = list(mail.sent)
        _db.session.rollback()
        # leased: not due immediately, so nobody is mailed twice
        jobs.send_scheduled_announcements()
        assert mail.sent == first


class TestSnapshots:
    def test_all_and_roles_targets(self, data, mail, monkeypatch):
        admin = add_user('zadmin')
        role = UserRoleModel(name='zrole')
        _db.session.add(role)
        _db.session.commit()
        in_role = add_user('zinrole', roles=[role])
        no_mail = add_user('znomail', email=None, roles=[role])
        monkeypatch.setattr(AnnouncementModel, 'get_email_due', staticmethod(lambda: []))
        r = AnnouncementModel(title='R', content='c', target_type='roles', status='scheduled', send_email=True,
                              scheduled_at=datetime.now() - timedelta(minutes=1), created_by=admin.id)
        r.target_roles.append(role)
        al = AnnouncementModel(title='A', content='c', target_type='all', status='scheduled', send_email=True,
                               scheduled_at=datetime.now() - timedelta(minutes=1), created_by=admin.id)
        _db.session.add_all([r, al])
        _db.session.commit()
        r_id, al_id = r.id, al.id
        jobs.send_scheduled_announcements()
        assert pending(refresh(r_id)) == [in_role.id]
        everyone = {u.id for u in UserModel.query.all() if u.email}
        assert set(pending(refresh(al_id))) == everyone
        assert no_mail.id not in everyone


class TestApi:
    def test_create_send_immediately_queues_without_sending(self, data, mail, client):
        role = UserRoleModel.query.filter_by(name='administrator').first() or UserRoleModel(name='administrator')
        _db.session.add(role)
        admin = add_user('zadmin', roles=[role])
        u1 = add_user('zu1')
        r = client.post('/api/v1/admin/announcements', headers=headers_for(admin), json={
            'title': 'T', 'content': 'C', 'targetType': 'users', 'userIds': [u1.id],
            'sendEmail': True, 'sendImmediately': True})
        assert r.status_code == 201
        assert mail.sent == []
        a = refresh(r.get_json()['announcement']['id'])
        assert a.status == 'sent' and a.sent_at is not None
        assert a.email_status == 'pending' and a.email_attempts == 0
        assert pending(a) == [u1.id]

    def test_send_now_queues_without_sending(self, data, mail, client):
        role = UserRoleModel.query.filter_by(name='administrator').first() or UserRoleModel(name='administrator')
        _db.session.add(role)
        admin = add_user('zadmin', roles=[role])
        u1 = add_user('zu1')
        a = add_announcement(admin, status='draft', target='users', users=[u1])
        a_id = a.id
        r = client.post(f'/api/v1/admin/announcements/{a_id}/send', headers=headers_for(admin))
        assert r.status_code == 200
        assert mail.sent == []
        a = refresh(a_id)
        assert a.status == 'sent' and a.email_status == 'pending'
        assert pending(a) == [u1.id]

    @pytest.mark.parametrize('role_name', ['administrator', 'announcement_readonly'])
    def test_admin_gets_expose_email_fields(self, data, mail, client, role_name):
        role = UserRoleModel.query.filter_by(name=role_name).first() or UserRoleModel(name=role_name)
        _db.session.add(role)
        viewer = add_user('zviewer', roles=[role])
        u1, u2 = add_user('zu1'), add_user('zu2')
        a = add_announcement(viewer, target='users', users=[u1, u2])
        a.email_status = 'pending'
        a.email_attempts = 2
        a.email_last_error = 'down'
        a.email_next_attempt_at = datetime.now() + timedelta(minutes=5)
        a.email_pending_user_ids = json.dumps([u1.id, 99999])
        a.email_failures = json.dumps([{'userId': u2.id, 'error': 'refused'}])
        _db.session.commit()
        a_id = a.id

        listing = client.get('/api/v1/admin/announcements', headers=headers_for(viewer)).get_json()
        item = next(x for x in listing['announcements'] if x['id'] == a_id)
        assert item['emailStatus'] == 'pending'
        assert item['emailAttempts'] == 2
        assert item['emailLastError'] == ('down' if role_name == 'administrator' else None)
        assert item['emailFailureCount'] == 1
        assert item['emailNextAttemptAt'] is not None
        assert item['emailPendingCount'] == 2

        detail = client.get(f'/api/v1/admin/announcements/{a_id}', headers=headers_for(viewer)).get_json()
        assert detail['emailStatus'] == 'pending' and detail['emailPendingCount'] == 2
        full = role_name == 'administrator'
        assert detail['emailPending'][0] == {
            'userId': u1.id, 'username': 'zu1', 'email': 'zu1@example.com' if full else None}
        assert detail['emailPending'][1] == {'userId': 99999, 'username': None, 'email': None}
        assert detail['emailFailures'] == [{
            'userId': u2.id, 'username': 'zu2',
            'email': 'zu2@example.com' if full else None, 'error': 'refused' if full else None}]
        assert detail['emailFailureCount'] == 1
        assert detail['emailLastError'] == ('down' if full else None)


class TestOtherEmailCallers:
    def test_send_html_email_swallows_errors_by_default(self, data, mail, app):
        def boom(recipient):
            raise smtplib.SMTPServerDisconnected('down')
        mail.behaviour = boom
        with app.test_request_context():
            email_module.send_html_email('s', ['a@example.com'], 'm')  # no raise
            with pytest.raises(smtplib.SMTPServerDisconnected):
                email_module.send_html_email('s', ['a@example.com'], 'm', raise_errors=True)


class TestFrontendUrlSafe:
    def test_configured_url_outside_request(self, app):
        app.config['FRONTEND_URL'] = 'https://portal.example.org/'
        try:
            assert notifications.get_frontend_url_safe() == 'https://portal.example.org'
        finally:
            app.config.pop('FRONTEND_URL', None)

    def test_localhost_config_uses_forwarded_host(self, app):
        app.config['FRONTEND_URL'] = 'http://localhost:5000'
        try:
            with app.test_request_context('/', headers={
                    'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'tak.example.org'}):
                assert notifications.get_frontend_url_safe() == 'https://tak.example.org'
        finally:
            app.config.pop('FRONTEND_URL', None)
