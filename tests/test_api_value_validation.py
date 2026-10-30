"""
Values limited by a CHECK constraint are rejected with 400 before they reach the database
"""
import pytest

from app.models import db as _db, AnnouncementModel, RadioModel, SystemSettingsModel, UserModel


@pytest.fixture
def cleanup(app):
    """Removes the announcements/radios created during the test and restores settings."""
    with app.app_context():
        before = {m: {o.id for o in m.query.all()} for m in (AnnouncementModel, RadioModel)}
        setting = SystemSettingsModel.get_setting('user_radio_enrollment_enabled', None)
        yield
        _db.session.rollback()
        for m, ids in before.items():
            for obj in m.query.all():
                if obj.id not in ids:
                    _db.session.delete(obj)
        _db.session.commit()
        if setting is not None:
            SystemSettingsModel.set_setting('user_radio_enrollment_enabled', setting)
        else:
            SystemSettingsModel.query.filter_by(key='user_radio_enrollment_enabled').delete()
            _db.session.commit()


@pytest.fixture
def announcement(auth_headers, cleanup):
    creator = UserModel.query.filter_by(username='testuser').first()
    a = AnnouncementModel(title='Draft', content='Body', target_type='all', status='draft',
                          created_by=creator.id)
    _db.session.add(a)
    _db.session.commit()
    return a


@pytest.fixture
def radio(cleanup):
    r = RadioModel(name='val-radio', platform='meshtastic', radioType='meshtastic')
    _db.session.add(r)
    _db.session.commit()
    return r


def test_update_announcement_rejects_invalid_target_type(client, auth_headers, announcement):
    resp = client.put(f'/api/v1/admin/announcements/{announcement.id}', headers=auth_headers,
                      json={'targetType': 'everyone', 'title': 'Changed'})

    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Invalid target type'
    _db.session.expire_all()
    a = AnnouncementModel.query.get(announcement.id)
    assert (a.target_type, a.title) == ('all', 'Draft')


def test_update_announcement_accepts_valid_target_type(client, auth_headers, announcement):
    resp = client.put(f'/api/v1/admin/announcements/{announcement.id}', headers=auth_headers,
                      json={'targetType': 'users', 'userIds': []})

    assert resp.status_code == 200
    _db.session.expire_all()
    assert AnnouncementModel.query.get(announcement.id).target_type == 'users'


def test_create_radio_rejects_invalid_radio_type(client, auth_headers, cleanup):
    resp = client.post('/api/v1/radios', headers=auth_headers,
                       json={'name': 'val-new', 'radioType': 'lora'})

    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Invalid radio type'
    assert RadioModel.query.filter_by(name='val-new').count() == 0


def test_create_radio_accepts_valid_radio_type(client, auth_headers, cleanup):
    resp = client.post('/api/v1/radios', headers=auth_headers,
                       json={'name': 'val-new', 'radioType': 'other'})

    assert resp.status_code == 201
    assert RadioModel.query.filter_by(name='val-new').one().radioType == 'other'


def test_update_radio_rejects_invalid_radio_type(client, auth_headers, radio):
    resp = client.put(f'/api/v1/radios/{radio.id}', headers=auth_headers,
                      json={'radioType': None, 'name': 'renamed'})

    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Invalid radio type'
    _db.session.expire_all()
    r = RadioModel.query.get(radio.id)
    assert (r.radioType, r.name) == ('meshtastic', 'val-radio')


def test_enroll_radio_rejects_invalid_radio_type(client, auth_headers, cleanup):
    SystemSettingsModel.set_setting('user_radio_enrollment_enabled', 'true')

    resp = client.post('/api/v1/radios/enroll', headers=auth_headers,
                       json={'name': 'val-enroll', 'radioType': 'heltec_v3'})

    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Invalid radio type'
    assert RadioModel.query.filter_by(name='val-enroll').count() == 0
