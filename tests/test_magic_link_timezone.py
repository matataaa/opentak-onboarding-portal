"""
Magic link tokens must expire in the timezone they are validated in: the docs recommend
setting TZ, and a link issued in UTC is already expired east of UTC.
"""
import os
import time

import pytest

from app.api_v1 import magic_link as magic_link_mod
from app.models import OneTimeTokenModel, SystemSettingsModel, UserModel, db as _db

SETTING = 'magic_link_login_enabled'


@pytest.fixture
def east_of_utc():
    old = os.environ.get('TZ')
    os.environ['TZ'] = 'XXX-9'  # POSIX form: needs no tzdata
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            del os.environ['TZ']
        else:
            os.environ['TZ'] = old
        time.tzset()


@pytest.fixture
def magic_user(app, monkeypatch):
    monkeypatch.setattr(magic_link_mod, 'send_html_email', lambda **kwargs: None)
    with app.app_context():
        old = SystemSettingsModel.get_setting(SETTING, False)
        SystemSettingsModel.set_setting(SETTING, True)
        user = UserModel(username='zz_magic', email='zz_magic@example.com')
        _db.session.add(user)
        _db.session.commit()
        yield user
        _db.session.rollback()
        _db.session.delete(_db.session.get(UserModel, user.id))
        _db.session.commit()
        SystemSettingsModel.set_setting(SETTING, old)


def test_magic_link_works_under_non_utc_timezone(client, magic_user, east_of_utc):
    assert client.post('/api/v1/auth/magic-link', json={'email': 'zz_magic@example.com'}).status_code == 200
    token = OneTimeTokenModel.query.filter_by(user_id=magic_user.id, token_type='magic_login').one().token

    r = client.post('/api/v1/auth/magic-link/verify', json={'token': token})

    assert r.status_code == 200, r.get_json()
    assert r.get_json()['user']['username'] == 'zz_magic'
