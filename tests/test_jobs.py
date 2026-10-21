"""
Tests for the nightly expired-account removal job and its settings default
"""
from datetime import datetime, timedelta

import pytest

from app import jobs
from app.extensions import scheduler
from app.models import db as _db, SystemSettingsModel, UserModel, UserRoleModel

SETTING = 'remove_expired_accounts_enabled'


class FakeOTS:
    def __init__(self):
        self.deleted = []
        self.fail_for = set()

    def delete_user(self, username):
        if username in self.fail_for:
            raise Exception('ots failure')
        self.deleted.append(username)


@pytest.fixture
def ots(monkeypatch, app):
    fake = FakeOTS()
    monkeypatch.setattr(jobs, 'otsClient', fake)
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    return fake


@pytest.fixture
def data(app):
    """Removes only the users, roles and settings created during the test."""
    models = (UserModel, UserRoleModel, SystemSettingsModel)
    with app.app_context():
        before = {m: {o.id for o in m.query.all()} for m in models}
        yield _db
        _db.session.rollback()
        for m in models:
            for obj in m.query.all():
                if obj.id not in before[m]:
                    _db.session.delete(obj)
        _db.session.commit()


def set_enabled(value):
    SystemSettingsModel.set_setting(SETTING, value, 'registration', 'test')


def add_user(name, expiry='past', admin=False, role_name='administrator'):
    user = UserModel(username=name)
    if expiry == 'past':
        user.expiryDate = datetime.now() - timedelta(days=1)
    elif expiry == 'future':
        user.expiryDate = datetime.now() + timedelta(days=1)
    if admin:
        role = UserRoleModel.query.filter_by(name=role_name).first()
        if not role:
            role = UserRoleModel(name=role_name)
            _db.session.add(role)
        user.roles.append(role)
    _db.session.add(user)
    _db.session.commit()
    return user


def present(name):
    return UserModel.query.filter_by(username=name).first() is not None


def test_disabled_or_missing_setting_does_nothing(data, ots):
    add_user('zz_exp')
    jobs.remove_expired_accounts()  # row missing
    set_enabled('false')
    jobs.remove_expired_accounts()
    assert ots.deleted == [] and present('zz_exp')


def test_enabled_removes_only_expired_users(data, ots):
    set_enabled('true')
    add_user('zz_exp')
    add_user('zz_future', expiry='future')
    add_user('zz_never', expiry=None)
    jobs.remove_expired_accounts()
    assert ots.deleted == ['zz_exp']
    assert not present('zz_exp') and present('zz_future') and present('zz_never')


@pytest.mark.parametrize('role_name', ['administrator', 'admin'])
def test_expired_administrator_is_kept(data, ots, role_name):
    set_enabled('true')
    add_user('zz_admin', admin=True, role_name=role_name)
    jobs.remove_expired_accounts()
    assert ots.deleted == [] and present('zz_admin')


def test_failed_local_delete_never_touches_ots(data, ots, monkeypatch):
    set_enabled('true')
    for name in ('zz_a', 'zz_b'):
        add_user(name)
    real_flush = _db.session.flush

    def flaky_flush(*args, **kwargs):
        if any(isinstance(o, UserModel) and o.username == 'zz_a' for o in _db.session.deleted):
            raise Exception('FK violation')
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(_db.session, 'flush', flaky_flush)
    jobs.remove_expired_accounts()
    assert ots.deleted == ['zz_b']
    assert present('zz_a') and not present('zz_b')


def test_ots_failure_for_one_user_does_not_stop_others(data, ots):
    set_enabled('true')
    for name in ('zz_a', 'zz_b', 'zz_c'):
        add_user(name)
    ots.fail_for = {'zz_a'}
    jobs.remove_expired_accounts()
    assert present('zz_a') and not present('zz_b') and not present('zz_c')
    assert sorted(ots.deleted) == ['zz_b', 'zz_c']


def test_count_and_usernames_printed_without_admins(data, ots, capsys):
    set_enabled('true')
    add_user('zz_x')
    add_user('zz_admin', admin=True)
    jobs.remove_expired_accounts()
    out = capsys.readouterr().out
    assert "Expired accounts to remove: 1 ['zz_x']" in out
    assert 'skipping administrator zz_admin' in out


def test_local_commit_failure_does_not_stop_others(data, ots, monkeypatch):
    set_enabled('true')
    add_user('zz_a')
    add_user('zz_b')
    real_commit = _db.session.commit
    calls = {'n': 0}

    def flaky_commit():
        calls['n'] += 1
        if calls['n'] == 1:
            raise Exception('db down')
        return real_commit()

    monkeypatch.setattr(_db.session, 'commit', flaky_commit)
    jobs.remove_expired_accounts()
    assert len(ots.deleted) == 2
    assert sum(present(n) for n in ('zz_a', 'zz_b')) == 1


def test_initialize_defaults_seeds_disabled_and_keeps_existing(data):
    original = SystemSettingsModel.query.filter_by(key=SETTING).first()
    original_value = original.value if original else None
    try:
        SystemSettingsModel.initialize_defaults()
        row = SystemSettingsModel.query.filter_by(key=SETTING).one()
        if original is None:
            assert (row.value, row.category) == ('false', 'registration')
        row.value = 'true'
        _db.session.commit()
        SystemSettingsModel.initialize_defaults()
        assert SystemSettingsModel.query.filter_by(key=SETTING).one().value == 'true'
    finally:
        row = SystemSettingsModel.query.filter_by(key=SETTING).first()
        if row is not None and original_value is not None:
            row.value = original_value
            _db.session.commit()
