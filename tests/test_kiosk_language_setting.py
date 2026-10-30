"""
The kiosk_language setting: seeded default, public exposure and enum validation.
"""
import pytest
from flask_jwt_extended import create_access_token

from app.models import SystemSettingsModel, UserModel, UserRoleModel


@pytest.fixture
def admin_headers(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    admin = UserModel.query.filter_by(username='kiosklangadmin').first() or \
        UserModel.create_user(username='kiosklangadmin', email='kiosklangadmin@example.com')
    if role not in admin.roles:
        admin.roles.append(role)
        db.session.commit()
    token = create_access_token(identity=str(admin.id), additional_claims={'roles': ['administrator']})
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture(autouse=True)
def _seed_defaults(db):
    SystemSettingsModel.initialize_defaults()
    yield
    db.session.rollback()
    setting = SystemSettingsModel.query.filter_by(key='kiosk_language').first()
    if setting:
        setting.value = ''
        db.session.commit()


def test_kiosk_language_seeded_with_empty_default(db):
    setting = SystemSettingsModel.query.filter_by(key='kiosk_language').first()
    assert setting is not None
    assert setting.value == ''
    assert setting.category == 'kiosk'


def test_public_settings_expose_kiosk_language(client):
    response = client.get('/api/v1/settings')
    assert response.status_code == 200
    assert response.get_json()['kiosk_language'] == ''


@pytest.mark.parametrize('value', ['', 'en', 'nl', 'de'])
def test_put_by_key_accepts_valid_languages(client, admin_headers, value):
    response = client.put('/api/v1/admin/settings/key/kiosk_language', headers=admin_headers, json={'value': value})
    assert response.status_code == 200
    assert client.get('/api/v1/settings').get_json()['kiosk_language'] == value


@pytest.mark.parametrize('value', ['fr', 'EN', None])
def test_put_by_key_rejects_invalid_languages(client, admin_headers, value):
    response = client.put('/api/v1/admin/settings/key/kiosk_language', headers=admin_headers, json={'value': value})
    assert response.status_code == 400
    assert 'error' in response.get_json()
    assert SystemSettingsModel.query.filter_by(key='kiosk_language').first().value == ''


def test_put_requires_settings_admin(client, db):
    user = UserModel.query.filter_by(username='kiosklanguser').first() or \
        UserModel.create_user(username='kiosklanguser', email='kiosklanguser@example.com')
    token = create_access_token(identity=str(user.id), additional_claims={'roles': []})
    response = client.put('/api/v1/admin/settings/key/kiosk_language',
                          headers={'Authorization': f'Bearer {token}'}, json={'value': 'nl'})
    assert response.status_code == 403
