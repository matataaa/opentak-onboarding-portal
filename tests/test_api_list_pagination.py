"""
Tests for search and pagination on the admin list endpoints
"""
import pytest
from flask_jwt_extended import create_access_token


# Rows are committed and outlive the test, so each fixture starts from and leaves an empty table
@pytest.fixture
def groups(db):
    from app.models import OTSGroupModel
    OTSGroupModel.query.delete()
    # Inserted in reverse so ordering by name is actually exercised
    for i in reversed(range(25)):
        db.session.add(OTSGroupModel(name=f'grp_{i:02d}', display_name=f'Group {i:02d}',
                                     description='alpha team' if i < 3 else 'other'))
    db.session.commit()
    yield
    OTSGroupModel.query.delete()
    db.session.commit()


@pytest.fixture
def channels(db):
    from app.models import MeshtasticModel
    MeshtasticModel.query.delete()
    for i in reversed(range(25)):
        db.session.add(MeshtasticModel(name=f'chan_{i:02d}', url=f'https://meshtastic.org/e/#c{i}',
                                       description='alpha net' if i < 3 else 'other'))
    db.session.commit()
    yield
    MeshtasticModel.query.delete()
    db.session.commit()


class TestGroupsListControls:
    """GET /api/v1/groups with search and pagination"""

    def test_returns_all_sorted_without_per_page(self, client, auth_headers, groups):
        data = client.get('/api/v1/groups', headers=auth_headers).get_json()

        names = [g['name'] for g in data['groups']]
        assert len(names) == 25
        assert names == sorted(names)
        assert data['total'] == 25

    def test_paginates(self, client, auth_headers, groups):
        data = client.get('/api/v1/groups?page=2&per_page=10', headers=auth_headers).get_json()

        assert [g['name'] for g in data['groups']] == [f'grp_{i:02d}' for i in range(10, 20)]
        assert data['total'] == 25
        assert data['page'] == 2
        assert data['per_page'] == 10

    def test_search_matches_name_and_description(self, client, auth_headers, groups):
        by_name = client.get('/api/v1/groups?search=grp_2', headers=auth_headers).get_json()
        by_description = client.get('/api/v1/groups?search=ALPHA', headers=auth_headers).get_json()

        assert [g['name'] for g in by_name['groups']] == ['grp_20', 'grp_21', 'grp_22', 'grp_23', 'grp_24']
        assert by_description['total'] == 3

    def test_sorting_ignores_case(self, client, auth_headers, groups, db):
        from app.models import OTSGroupModel
        db.session.add(OTSGroupModel(name='Grp_10b'))
        db.session.commit()

        names = [g['name'] for g in client.get('/api/v1/groups', headers=auth_headers).get_json()['groups']]

        assert names.index('Grp_10b') == names.index('grp_10') + 1

    def test_search_total_is_before_pagination(self, client, auth_headers, groups):
        data = client.get('/api/v1/groups?search=other&per_page=10', headers=auth_headers).get_json()

        assert len(data['groups']) == 10
        assert data['total'] == 22


class TestMeshtasticAdminListControls:
    """GET /api/v1/meshtastic/admin with search and pagination"""

    def test_returns_all_sorted_without_per_page(self, client, auth_headers, channels):
        data = client.get('/api/v1/meshtastic/admin', headers=auth_headers).get_json()

        names = [c['name'] for c in data['configs']]
        assert len(names) == 25
        assert names == sorted(names)
        assert data['total'] == 25

    def test_paginates(self, client, auth_headers, channels):
        data = client.get('/api/v1/meshtastic/admin?page=3&per_page=10', headers=auth_headers).get_json()

        assert [c['name'] for c in data['configs']] == [f'chan_{i:02d}' for i in range(20, 25)]
        assert data['total'] == 25

    def test_search_matches_name_and_description(self, client, auth_headers, channels):
        by_name = client.get('/api/v1/meshtastic/admin?search=chan_0', headers=auth_headers).get_json()
        by_description = client.get('/api/v1/meshtastic/admin?search=alpha', headers=auth_headers).get_json()

        assert by_name['total'] == 10
        assert by_description['total'] == 3


def _make_items(db, model, build):
    """Add 25 rows named lp_00..lp_24 (inserted in reverse) and remove them after the test."""
    rows = [model(**build(i)) for i in reversed(range(25))]
    db.session.add_all(rows)
    db.session.commit()
    yield
    for row in rows:
        db.session.delete(row)
    db.session.commit()


@pytest.fixture
def roles(db):
    from app.models import UserRoleModel
    yield from _make_items(db, UserRoleModel, lambda i: {
        'name': f'lp_{i:02d}', 'description': 'alpha' if i < 3 else 'other'})


@pytest.fixture
def onboarding_codes(db):
    from app.models import OnboardingCodeModel
    yield from _make_items(db, OnboardingCodeModel, lambda i: {
        'name': f'lp_{i:02d}', 'onboardingCode': f'LPCODE{i:02d}',
        'description': 'alpha' if i < 3 else 'other'})


@pytest.fixture
def tak_profiles(db):
    from app.models import TakProfileModel
    yield from _make_items(db, TakProfileModel, lambda i: {
        'name': f'lp_{i:02d}', 'description': 'alpha' if i < 3 else 'other', 'isPublic': i % 2 == 0})


@pytest.fixture
def radios(db):
    from app.models import RadioModel
    yield from _make_items(db, RadioModel, lambda i: {
        'name': f'lp_{i:02d}', 'platform': 'esp32', 'description': 'alpha' if i < 3 else 'other'})


@pytest.mark.parametrize('path,key,fixture', [
    ('/api/v1/roles', 'roles', 'roles'),
    ('/api/v1/onboarding-codes', 'codes', 'onboarding_codes'),
    ('/api/v1/tak-profiles', 'profiles', 'tak_profiles'),
    ('/api/v1/radios', 'radios', 'radios'),
])
class TestOtherListControls:
    """Search and pagination on roles, onboarding codes, TAK profiles and radios (admin)"""

    def test_paginates_sorted(self, request, client, auth_headers, path, key, fixture):
        request.getfixturevalue(fixture)

        data = client.get(f'{path}?search=lp_&page=3&per_page=10', headers=auth_headers).get_json()

        assert [x['name'] for x in data[key]] == [f'lp_{i:02d}' for i in range(20, 25)]
        assert data['total'] == 25

    def test_search_description_and_all_without_per_page(self, request, client, auth_headers, path, key, fixture):
        request.getfixturevalue(fixture)

        by_description = client.get(f'{path}?search=ALPHA', headers=auth_headers).get_json()
        everything = client.get(path, headers=auth_headers).get_json()

        assert by_description['total'] == 3
        assert len(everything[key]) == everything['total'] >= 25


def test_onboarding_code_search_matches_code(client, auth_headers, onboarding_codes):
    data = client.get('/api/v1/onboarding-codes?search=lpcode07', headers=auth_headers).get_json()

    assert [c['name'] for c in data['codes']] == ['lp_07']


def test_tak_profiles_non_admin_still_filtered(app, client, db, tak_profiles):
    from app.models import UserModel
    user = UserModel.query.filter_by(username='lp_plain_user').first() or UserModel.create_user(username='lp_plain_user')
    token = create_access_token(identity=str(user.id), additional_claims={'roles': []})

    data = client.get('/api/v1/tak-profiles?search=lp_&per_page=5',
                      headers={'Authorization': f'Bearer {token}'}).get_json()

    # Only the 13 public, unrestricted profiles (even numbers) are visible
    assert data['total'] == 13
    assert [p['name'] for p in data['profiles']] == ['lp_00', 'lp_02', 'lp_04', 'lp_06', 'lp_08']
