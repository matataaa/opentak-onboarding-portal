"""
Password-reset tokens must expire in the timezone the cleanup job compares them in: the docs
recommend setting TZ, and a token issued in UTC is already expired east of UTC.
"""
import os
import time

import pytest

from app import jobs
from app.api_v1 import auth as auth_mod
from app.extensions import scheduler
from app.models import OneTimeTokenModel, UserModel, db as _db


class FakeOTS:
    def __call__(self, *args):
        return self

    def reset_user_password(self, username, password):
        return {}


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
def reset_user(app, monkeypatch):
    monkeypatch.setattr(auth_mod, 'send_html_email', lambda **kwargs: None)
    monkeypatch.setattr(auth_mod, 'OTSClient', FakeOTS())
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    with app.app_context():
        user = UserModel(username='zz_reset', email='zz_reset@example.com')
        _db.session.add(user)
        _db.session.commit()
        yield user
        _db.session.rollback()
        _db.session.delete(_db.session.get(UserModel, user.id))
        _db.session.commit()


def test_reset_token_survives_cleanup_job_and_works(client, reset_user, east_of_utc):
    assert client.post('/api/v1/auth/forgot-password', json={'email': 'zz_reset@example.com'}).status_code == 200

    jobs.cleanup_temp_downloads()

    token = OneTimeTokenModel.query.filter_by(user_id=reset_user.id, token_type='password_reset').one().token
    r = client.post('/api/v1/auth/reset-password', json={'token': token, 'new_password': 'a-new-password'})
    assert r.status_code == 200, r.get_json()
