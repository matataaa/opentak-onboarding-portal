"""
SECRET_KEY / JWT_SECRET_KEY: strong environment values are used as they are; weak ones are
replaced by a generated value persisted in instance/. app/settings.py runs more than once per
process, so resolving must be repeatable.
"""
import builtins
import logging
import os
import stat

import pytest

from app import settings

NAME = 'SECRET_KEY'
STRONG = 'a-strong-secret-from-the-environment'


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Resolve as a production process would, with the repo root at tmp_path."""
    monkeypatch.setattr(settings, 'TESTING', False)
    monkeypatch.setattr(settings, '__file__', str(tmp_path / 'app' / 'settings.py'))
    monkeypatch.setenv(NAME, 'dev')  # registers the real value for restoring
    return tmp_path


@pytest.mark.parametrize('weak', ['', 'None', 'dev', 'your-secret-key', 'your-jwt-secret', 'x' * 15])
def test_weak_value_generates_and_persists(root, monkeypatch, weak):
    monkeypatch.setenv(NAME, weak)
    value = settings._resolve_secret(NAME, 'secret_key')
    assert not settings._is_weak_secret(value)
    assert (root / 'instance' / 'secret_key').read_text() == value


def test_unset_value_generates(root, monkeypatch):
    monkeypatch.delenv(NAME)
    assert settings._resolve_secret(NAME, 'secret_key') == (root / 'instance' / 'secret_key').read_text()


def test_second_call_identical_and_warns_once(root, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger='app.settings'):
        first = settings._resolve_secret(NAME, 'secret_key')
        assert settings._resolve_secret(NAME, 'secret_key') == first
    assert len(caplog.records) == 1
    assert NAME in caplog.text and first not in caplog.text


def test_new_process_reads_the_file(root, monkeypatch):
    first = settings._resolve_secret(NAME, 'secret_key')
    monkeypatch.setenv(NAME, 'dev')
    assert settings._resolve_secret(NAME, 'secret_key') == first


def test_file_mode_is_0600(root):
    old_umask = os.umask(0)
    try:
        settings._resolve_secret(NAME, 'secret_key')
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE((root / 'instance' / 'secret_key').stat().st_mode) == 0o600


def test_strong_value_untouched(root, monkeypatch):
    monkeypatch.setenv(NAME, STRONG)
    assert settings._resolve_secret(NAME, 'secret_key') == STRONG
    assert not (root / 'instance').exists()


def test_unwritable_instance_falls_back(root, monkeypatch, caplog):
    (root / 'instance').write_text('a file where the directory should be')
    with caplog.at_level(logging.WARNING, logger='app.settings'):
        value = settings._resolve_secret(NAME, 'secret_key')
        # Stays the same for the rest of this process
        assert settings._resolve_secret(NAME, 'secret_key') == value
    assert not settings._is_weak_secret(value)
    assert len(caplog.records) == 1
    assert 'TEMPORARY' in caplog.text and NAME in caplog.text and value not in caplog.text


def test_permission_denied_falls_back(root, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError('denied')

    monkeypatch.setattr(settings.os, 'open', denied)
    assert not settings._is_weak_secret(settings._resolve_secret(NAME, 'secret_key'))
    assert not (root / 'instance' / 'secret_key').exists()


def test_unreadable_file_falls_back_with_chown_hint(root, monkeypatch, caplog):
    (root / 'instance').mkdir()
    path = root / 'instance' / 'secret_key'
    path.write_text('value-this-process-may-not-read')
    real_open = builtins.open

    def denied_for_key(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError('denied')
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', denied_for_key)
    with caplog.at_level(logging.WARNING, logger='app.settings'):
        value = settings._resolve_secret(NAME, 'secret_key')
    assert value != 'value-this-process-may-not-read' and not settings._is_weak_secret(value)
    assert 'TEMPORARY' in caplog.text and 'chown -R 1000:1000 ./instance' in caplog.text


def test_empty_file_of_a_concurrent_writer_is_waited_for(root, monkeypatch):
    (root / 'instance').mkdir()
    path = root / 'instance' / 'secret_key'
    path.write_text('')

    def writer_finishes(_seconds):
        path.write_text('value-written-by-the-other-process')

    monkeypatch.setattr(settings.time, 'sleep', writer_finishes)
    assert settings._resolve_secret(NAME, 'secret_key') == 'value-written-by-the-other-process'


def test_empty_file_of_a_dead_writer_is_replaced(root, monkeypatch):
    (root / 'instance').mkdir()
    path = root / 'instance' / 'secret_key'
    path.write_text('')
    monkeypatch.setattr(settings.time, 'sleep', lambda _seconds: None)
    value = settings._resolve_secret(NAME, 'secret_key')
    assert not settings._is_weak_secret(value)
    assert path.read_text() == value


def test_testing_bypasses_everything(root, monkeypatch):
    monkeypatch.setattr(settings, 'TESTING', True)
    monkeypatch.setenv(NAME, 'test-secret-key')  # conftest's value: 15 characters
    assert settings._resolve_secret(NAME, 'secret_key') == 'test-secret-key'
    assert not (root / 'instance').exists()
