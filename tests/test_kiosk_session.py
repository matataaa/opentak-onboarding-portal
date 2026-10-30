"""
Kiosk sessions are short-lived shared-terminal sessions: refreshing a kiosk token must
keep the kiosk_session claim and the kiosk lifetime instead of minting a 12-hour session.
"""
from datetime import timedelta

import pytest
from flask_jwt_extended import create_access_token, create_refresh_token, decode_token

from app.models import KioskSessionModel, SystemSettingsModel, UserModel

PREFIX = '/api/v1'


@pytest.fixture
def kiosk(db):
    old_enabled = SystemSettingsModel.get_setting('kiosk_enrollment_enabled', False)
    old_timeout = SystemSettingsModel.get_setting('kiosk_session_timeout_minutes', '10')
    SystemSettingsModel.set_setting('kiosk_enrollment_enabled', True)
    SystemSettingsModel.set_setting('kiosk_session_timeout_minutes', '10')
    yield
    db.session.rollback()
    KioskSessionModel.query.delete()
    db.session.commit()
    SystemSettingsModel.set_setting('kiosk_enrollment_enabled', old_enabled)
    SystemSettingsModel.set_setting('kiosk_session_timeout_minutes', old_timeout)


def test_kiosk_refresh_keeps_kiosk_claim_and_lifetime(client, db, kiosk):
    user = UserModel.query.filter_by(username='kioskuser').first() or \
        UserModel.create_user(username='kioskuser', email='kioskuser@example.com')
    r = client.post(PREFIX + '/kiosk/session')
    assert r.status_code == 200
    session_id = r.get_json()['session_id']

    h = {'Authorization': 'Bearer ' + create_access_token(identity=str(user.id))}
    r = client.post(PREFIX + f'/kiosk/session/{session_id}/authenticate', headers=h)
    assert r.status_code == 200

    status = client.get(PREFIX + f'/kiosk/session/{session_id}/status').get_json()
    assert status['status'] == 'authenticated'

    r = client.post(PREFIX + '/auth/refresh',
                    headers={'Authorization': 'Bearer ' + status['refresh_token']})
    assert r.status_code == 200
    claims = decode_token(r.get_json()['access_token'])
    assert claims.get('kiosk_session') is True
    assert claims['exp'] - claims['iat'] <= 10 * 60


def test_kiosk_refresh_never_outlives_refresh_token(client, db, kiosk):
    user = UserModel.query.filter_by(username='kioskuser').first() or \
        UserModel.create_user(username='kioskuser', email='kioskuser@example.com')
    # A kiosk refresh token with only 60 seconds left, while the kiosk timeout is 10 minutes.
    refresh_token = create_refresh_token(identity=str(user.id),
                                         additional_claims={'kiosk_session': True},
                                         expires_delta=timedelta(seconds=60))

    r = client.post(PREFIX + '/auth/refresh',
                    headers={'Authorization': 'Bearer ' + refresh_token})
    assert r.status_code == 200
    claims = decode_token(r.get_json()['access_token'])
    assert claims.get('kiosk_session') is True
    assert claims['exp'] <= decode_token(refresh_token)['exp']
