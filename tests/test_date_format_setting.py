"""
The portal-wide date_format setting: seeded default, public exposure and enum validation.
"""
import pytest
from flask_jwt_extended import create_access_token

from app.models import SystemSettingsModel, UserModel, UserRoleModel


@pytest.fixture
def admin_headers(db):
    role = UserRoleModel.query.filter_by(name='administrator').first() or \
        UserRoleModel.create_role(name='administrator', description='Admin role')
    admin = UserModel.query.filter_by(username='datefmtadmin').first() or \
        UserModel.create_user(username='datefmtadmin', email='datefmtadmin@example.com')
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
    setting = SystemSettingsModel.query.filter_by(key='date_format').first()
    if setting:
        setting.value = 'eu'
        db.session.commit()


def test_date_format_seeded_with_eu_default(db):
    setting = SystemSettingsModel.query.filter_by(key='date_format').first()
    assert setting is not None
    assert setting.value == 'eu'
    assert setting.category == 'appearance'


def test_public_settings_expose_date_format(client):
    response = client.get('/api/v1/settings')
    assert response.status_code == 200
    assert response.get_json()['date_format'] == 'eu'


@pytest.mark.parametrize('value', ['eu', 'us', 'iso'])
def test_put_by_key_accepts_valid_formats(client, admin_headers, value):
    response = client.put('/api/v1/admin/settings/key/date_format', headers=admin_headers, json={'value': value})
    assert response.status_code == 200
    assert client.get('/api/v1/settings').get_json()['date_format'] == value


@pytest.mark.parametrize('value', ['fr', 'EU', ''])
def test_put_by_key_rejects_invalid_formats(client, admin_headers, value):
    response = client.put('/api/v1/admin/settings/key/date_format', headers=admin_headers, json={'value': value})
    assert response.status_code == 400
    assert 'error' in response.get_json()
    assert SystemSettingsModel.query.filter_by(key='date_format').first().value == 'eu'


@pytest.mark.parametrize('value, status', [('us', 200), ('fr', 400)])
def test_put_by_id_validates_format(client, admin_headers, value, status):
    setting = SystemSettingsModel.query.filter_by(key='date_format').first()
    response = client.put(f'/api/v1/admin/settings/{setting.id}', headers=admin_headers, json={'value': value})
    assert response.status_code == status


def test_put_requires_settings_admin(client, db):
    user = UserModel.query.filter_by(username='datefmtuser').first() or \
        UserModel.create_user(username='datefmtuser', email='datefmtuser@example.com')
    token = create_access_token(identity=str(user.id), additional_claims={'roles': []})
    response = client.put('/api/v1/admin/settings/key/date_format',
                          headers={'Authorization': f'Bearer {token}'}, json={'value': 'us'})
    assert response.status_code == 403
