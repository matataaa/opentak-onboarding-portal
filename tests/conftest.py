"""
Pytest configuration and fixtures
"""
import pytest
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Test environment must be set before anything imports app: app/settings.py reads
# these at import time, and `import app` runs the module-level create_app().
os.environ.update({
    'TESTING': 'True',
    'DEBUG': 'False',
    'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
    'SECRET_KEY': 'test-secret-key',
    'JWT_SECRET_KEY': 'test-jwt-secret-key-at-least-32-bytes',
    'OTS_URL': 'http://localhost:8080',
    'OTS_USERNAME': 'test',
    'OTS_PASSWORD': 'test',
    'MAIL_ENABLED': 'False',
    'MAIL_PORT': '25',
    'MAIL_USE_TLS': 'False',
    'MAIL_USE_SSL': 'False',
})

from app import create_app
from app.models import db as _db


@pytest.fixture(scope='session')
def app():
    """Create application for testing"""
    app = create_app()
    app.config['WTF_CSRF_ENABLED'] = False

    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope='function')
def client(app):
    """Test client for making requests"""
    return app.test_client()


@pytest.fixture(scope='function')
def db(app):
    """Database fixture that provides a clean slate for each test"""
    with app.app_context():
        _db.session.begin_nested()
        yield _db
        _db.session.rollback()
        _db.session.remove()


@pytest.fixture(scope='function')
def auth_headers(client, db):
    """Create a test user and return auth headers"""
    from app.models import UserModel, UserRoleModel

    # Get or create admin role
    admin_role = UserRoleModel.query.filter_by(name='administrator').first()
    if not admin_role:
        admin_role = UserRoleModel.create_role(
            name='administrator',
            description='Admin role'
        )

    # Get or create test user
    user = UserModel.query.filter_by(username='testuser').first()
    if not user:
        user = UserModel.create_user(
            username='testuser',
            email='test@example.com',
            firstname='Test',
            lastname='User',
            callsign='TEST'
        )

    # Ensure user has admin role
    if admin_role not in user.roles:
        user.roles.append(admin_role)
        _db.session.commit()

    # Mock OTS authentication
    import requests_mock
    with requests_mock.Mocker() as m:
        # Mock OTS login
        m.post(
            'http://localhost:8080/api/login',
            json={
                'token': 'mock-ots-token',
                'user': {
                    'username': 'testuser',
                    'email': 'test@example.com',
                    'roles': ['administrator']
                }
            }
        )

        # Mock OTS get_me
        m.get(
            'http://localhost:8080/api/me',
            json={
                'username': 'testuser',
                'email': 'test@example.com',
                'callsign': 'TEST',
                'firstName': 'Test',
                'lastName': 'User',
                'roles': ['administrator']
            }
        )

        # Login to get JWT token
        response = client.post('/api/v1/auth/login', json={
            'username': 'testuser',
            'password': 'password'
        })

        if response.status_code == 200:
            data = response.get_json()
            access_token = data.get('access_token')
            return {'Authorization': f'Bearer {access_token}'}

    return {}


@pytest.fixture
def sample_user(db):
    """Create a sample user for testing"""
    from app.models import UserModel

    # Get or create user
    user = UserModel.query.filter_by(username='sampleuser').first()
    if not user:
        user = UserModel.create_user(
            username='sampleuser',
            email='sample@example.com',
            firstname='Sample',
            lastname='User',
            callsign='SAMPLE'
        )
        # Check if create returned an error dict
        if isinstance(user, dict):
            user = UserModel.query.filter_by(username='sampleuser').first()
        else:
            db.session.commit()
    return user


@pytest.fixture
def sample_role(db):
    """Create a sample role for testing"""
    from app.models import UserRoleModel

    # Get or create role
    role = UserRoleModel.query.filter_by(name='test_role').first()
    if not role:
        role = UserRoleModel.create_role(
            name='test_role',
            description='Test role for testing'
        )
        # Check if create returned an error dict
        if isinstance(role, dict):
            role = UserRoleModel.query.filter_by(name='test_role').first()
        else:
            db.session.commit()
    return role
