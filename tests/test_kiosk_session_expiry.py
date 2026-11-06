"""An authenticated kiosk session outlives its QR expiry: the next kiosk's cleanup must not delete its tokens."""
import datetime as datetime_module
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

from app import models as models_mod
from app.api_v1 import kiosk as kiosk_mod
from app.models import KioskSessionModel, SystemSettingsModel, UserModel

PREFIX = '/api/v1'


@pytest.fixture
def kiosk(db):
    old_enabled = SystemSettingsModel.get_setting('kiosk_enrollment_enabled', False)
    old_timeout = SystemSettingsModel.get_setting('kiosk_session_timeout_minutes', '10')
    SystemSettingsModel.set_setting('kiosk_enrollment_enabled', True)
    SystemSettingsModel.set_setting('kiosk_session_timeout_minutes', '30')
    yield
    db.session.rollback()
    KioskSessionModel.query.delete()
    UserModel.query.filter_by(username='kioskexpiry').delete()
    db.session.commit()
    SystemSettingsModel.set_setting('kiosk_enrollment_enabled', old_enabled)
    SystemSettingsModel.set_setting('kiosk_session_timeout_minutes', old_timeout)


def _clock_ahead(monkeypatch, minutes):
    """Move utcnow() forward in the kiosk endpoints and in KioskSessionModel.cleanup_expired."""
    class Later(datetime):
        @classmethod
        def utcnow(cls):
            return datetime.utcnow() + timedelta(minutes=minutes)
    monkeypatch.setattr(kiosk_mod, 'datetime', Later)
    monkeypatch.setattr(models_mod, 'datetime', SimpleNamespace(**{**vars(datetime_module), 'datetime': Later}))


def test_authenticated_session_survives_cleanup_after_qr_expiry(client, db, kiosk, monkeypatch):
    user = UserModel.create_user(username='kioskexpiry', email='kioskexpiry@example.com')
    authenticated = client.post(PREFIX + '/kiosk/session').get_json()['session_id']
    pending = client.post(PREFIX + '/kiosk/session').get_json()['session_id']

    headers = {'Authorization': 'Bearer ' + create_access_token(identity=str(user.id))}
    before = datetime.utcnow()
    r = client.post(PREFIX + f'/kiosk/session/{authenticated}/authenticate', headers=headers)
    assert r.status_code == 200
    assert KioskSessionModel.get_by_session_id(authenticated).expires_at >= before + timedelta(minutes=30)

    # 15 minutes later: past the 10-minute QR expiry, within the 30-minute token lifetime
    _clock_ahead(monkeypatch, 15)
    assert client.post(PREFIX + '/kiosk/session').status_code == 200

    assert KioskSessionModel.get_by_session_id(pending) is None
    status = client.get(PREFIX + f'/kiosk/session/{authenticated}/status').get_json()
    assert status['status'] == 'authenticated'
    assert status['access_token'] and status['refresh_token']
