from os import environ
from dotenv import load_dotenv
from urllib.parse import urlparse
import logging
import os
import secrets
import time


def strtobool(value):
    """Convert a truthy/falsy string to a bool.

    Replaces distutils.util.strtobool, which was removed in Python 3.12.
    Keeps the same accepted values and raises ValueError on anything else.
    """
    value = str(value).strip().lower()
    if value in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    if value in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    raise ValueError(f"invalid truth value {value!r}")


def _is_weak_secret(value):
    """Same strength rule as _link_secret in app/api_v1/events.py, plus the old documented placeholders."""
    return not value or value in ('None', 'dev', 'your-secret-key', 'your-jwt-secret') or len(value) < 16


def _read_or_create_secret(path):
    """Return the secret stored at path, creating it atomically (mode 0600) when missing."""
    for attempt in range(30):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path) as f:
                value = f.read().strip()
            if not _is_weak_secret(value):
                return value
            # Another process is between creating and writing the file; if that takes
            # this long it died, so start over.
            if attempt == 20:
                os.unlink(path)
            time.sleep(0.1)
            continue
        value = secrets.token_hex(32)
        with os.fdopen(fd, 'w') as f:
            f.write(value)
        return value
    raise OSError(f'could not read or create {path}')


def _resolve_secret(name, filename):
    """Return env var name, or a generated value persisted in instance/filename when it is weak.

    A generated value is put back in the environment: this module runs more than once
    per process (from_pyfile and imports) and must yield the same value without warning twice.
    """
    if TESTING:
        return str(environ.get(name, secrets.token_hex(32)))
    value = environ.get(name)
    if not _is_weak_secret(value):
        return value
    instance_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'instance')
    path = os.path.join(instance_dir, filename)
    logger = logging.getLogger('app.settings')
    try:
        os.makedirs(instance_dir, exist_ok=True)
        value = _read_or_create_secret(path)
        logger.warning('%s is empty or weak: using the generated value persisted in %s', name, path)
    except OSError as e:
        value = secrets.token_hex(32)
        logger.warning('%s is empty or weak and %s is not usable (%s): using a TEMPORARY random value. '
                       'Logins and links signed with it stop working at the next restart. '
                       'Set %s or give the portal user access to the instance directory '
                       '(Docker: sudo chown -R 1000:1000 ./instance).', name, path, e, name)
    environ[name] = value
    return value


load_dotenv()
TESTING = strtobool(environ.get('TESTING', 'False'))
API_KEY = str(environ.get('API_KEY', secrets.token_hex(32)))
SECRET_KEY=_resolve_secret('SECRET_KEY', 'secret_key')
OTS_USERNAME=str(environ.get('OTS_USERNAME'))
OTS_PASSWORD=str(environ.get('OTS_PASSWORD'))
OTS_URL=str(environ.get('OTS_URL'))
OTS_HOSTNAME=urlparse(OTS_URL).hostname
OTS_VERIFY_SSL=strtobool(environ.get('OTS_VERIFY_SSL', 'True'))
DEBUG=strtobool(environ.get('DEBUG'))
SQLALCHEMY_DATABASE_URI=str(environ.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///db.sqlite'))
# Replace connections the database closed while idle instead of failing the request
SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True}
MAIL_SERVER=str(environ.get('MAIL_SERVER'))
MAIL_PORT=int(environ.get('MAIL_PORT'))
MAIL_USE_TLS=strtobool(environ.get('MAIL_USE_TLS'))
MAIL_USE_SSL=strtobool(environ.get('MAIL_USE_SSL'))
MAIL_USERNAME=str(environ.get('MAIL_USERNAME'))
MAIL_PASSWORD=str(environ.get('MAIL_PASSWORD'))
MAIL_DEFAULT_SENDER=str(environ.get('MAIL_DEFAULT_SENDER'))
MAIL_ENABLED=strtobool(environ.get('MAIL_ENABLED', 'False'))
DATAPACKAGE_UPLOAD_FOLDER = (environ.get('DATAPACKAGE_UPLOAD_FOLDER','datapackages'))
UPDATES_UPLOAD_FOLDER = str(environ.get('UPDATES_UPLOAD_FOLDER', 'updates'))

JWT_SECRET_KEY = _resolve_secret('JWT_SECRET_KEY', 'jwt_secret_key')
# Allow JWT identity to be any JSON-serializable type (not just string)
JWT_IDENTITY_CLAIM = 'sub'
JWT_ALGORITHM = 'HS256'

# Frontend URL for password reset links
FRONTEND_URL = str(environ.get('FRONTEND_URL', 'http://localhost:5000'))

# CORS origins for API
CORS_ORIGINS = str(environ.get('CORS_ORIGINS', '*'))