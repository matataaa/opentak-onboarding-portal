"""
Emailed links always come from FRONTEND_URL, never from the request's headers. Only the
URLs that go back to the requesting browser (kiosk QR, OIDC) follow the request at the default.
"""
import logging
import threading

import pytest

from app import warn_if_frontend_url_is_local
from app.api_v1 import auth as auth_mod, magic_link as magic_link_mod
from app.api_v1.oidc import get_request_base_url
from app.models import KioskSessionModel, OneTimeTokenModel, SystemSettingsModel, UserModel, db as _db

DEFAULT = 'http://localhost:5000'
PUBLIC = 'https://portal.example.org'
OTHER_HOST = {'Host': 'other.example', 'X-Forwarded-Host': 'other.example', 'X-Forwarded-Proto': 'other.example'}
REQUEST_HOST = {'Host': 'portal.lan:8080'}


@pytest.fixture
def setting(app):
    """Set system settings for one test, restoring the old values afterwards."""
    with app.app_context():
        changed = {}

        def set_it(key, value):
            changed.setdefault(key, SystemSettingsModel.get_setting(key, False))
            SystemSettingsModel.set_setting(key, value)

        yield set_it
        _db.session.rollback()
        for key, old in changed.items():
            SystemSettingsModel.set_setting(key, old)


@pytest.fixture
def mail_user(app):
    with app.app_context():
        user = UserModel(username='zz_url', email='zz_url@example.com')
        _db.session.add(user)
        _db.session.commit()
        user_id = user.id
        yield user
        _db.session.rollback()
        OneTimeTokenModel.query.filter_by(user_id=user_id).delete()
        _db.session.delete(_db.session.get(UserModel, user_id))
        _db.session.commit()


@pytest.mark.parametrize('configured', [DEFAULT, PUBLIC + '/'])
@pytest.mark.parametrize('path, module', [
    ('/api/v1/auth/forgot-password', auth_mod),
    ('/api/v1/auth/magic-link', magic_link_mod),
])
def test_emailed_link_ignores_request_headers(client, app, monkeypatch, mail_user, setting, path, module, configured):
    monkeypatch.setitem(app.config, 'FRONTEND_URL', configured)
    setting('magic_link_login_enabled', True)
    sent = []
    done = threading.Event()  # the magic link is mailed from a background thread

    def capture(**kwargs):
        sent.append(kwargs)
        done.set()

    monkeypatch.setattr(module, 'send_html_email', capture)

    r = client.post(path, json={'email': 'zz_url@example.com'}, headers=OTHER_HOST)

    assert r.status_code == 200
    assert done.wait(5)
    link = sent[0]['link_url']
    assert link.startswith(configured.rstrip('/') + '/')
    assert 'other.example' not in link and 'other.example' not in sent[0]['message']


@pytest.mark.parametrize('configured, expected', [
    (DEFAULT, 'http://portal.lan:8080/kiosk-login/'),
    (PUBLIC, PUBLIC + '/kiosk-login/'),
])
def test_kiosk_qr_follows_request_only_at_the_default(client, app, monkeypatch, setting, configured, expected):
    monkeypatch.setitem(app.config, 'FRONTEND_URL', configured)
    setting('kiosk_enrollment_enabled', True)

    r = client.post('/api/v1/kiosk/session', headers=REQUEST_HOST)

    assert r.status_code == 200
    assert r.get_json()['qr_url'].startswith(expected)
    KioskSessionModel.query.filter_by(session_id=r.get_json()['session_id']).delete()
    _db.session.commit()


@pytest.mark.parametrize('configured, expected', [
    (DEFAULT, 'http://portal.lan:8080'),
    (PUBLIC + '/', PUBLIC),
])
def test_oidc_base_url_follows_request_only_at_the_default(app, monkeypatch, configured, expected):
    monkeypatch.setitem(app.config, 'FRONTEND_URL', configured)
    with app.test_request_context('/', headers=REQUEST_HOST):
        assert get_request_base_url() == expected


@pytest.mark.parametrize('url, warns', [
    (DEFAULT, True),
    ('http://127.0.0.1:5000', True),
    ('http://[::1]:5000', True),
    ('', True),
    ('http://[bad', True),
    (PUBLIC, False),
    ('http://localhost.example.org', False),
])
def test_startup_warning_for_local_frontend_url(app, monkeypatch, caplog, url, warns):
    monkeypatch.setitem(app.config, 'FRONTEND_URL', url)
    with caplog.at_level(logging.WARNING):
        warn_if_frontend_url_is_local(app)
    assert ('FRONTEND_URL' in caplog.text) is warns
