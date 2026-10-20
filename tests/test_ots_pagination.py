"""
Tests for syncing paginated OTS list endpoints
"""
import math
import pytest
import requests_mock

OTS_URL = 'http://localhost:8080'


def paginated(items):
    """Mimic OTS's paginate(): reads `per_page` (default 10) and `page` (default 1)."""
    def callback(request, context):
        page = int(request.qs.get('page', ['1'])[0])
        per_page = int(request.qs.get('per_page', ['10'])[0])
        start = (page - 1) * per_page
        return {
            'results': items[start:start + per_page],
            'total_pages': math.ceil(len(items) / per_page),
            'current_page': page,
            'per_page': per_page,
            'total': len(items),
        }
    return callback


@pytest.fixture
def ots_mock():
    with requests_mock.Mocker() as m:
        m.post(f'{OTS_URL}/api/login',
               json={'response': {'user': {'authentication_token': 'mock-ots-token'}}})
        yield m


class TestOTSPagination:
    """Syncs must fetch every page from OTS, not just the first"""

    def test_group_sync_fetches_all_pages(self, client, auth_headers, ots_mock):
        groups = [{'name': f'group_{i}', 'description': '', 'active': True} for i in range(25)]
        ots_mock.get(f'{OTS_URL}/api/groups', json=paginated(groups))

        response = client.post('/api/v1/groups/sync', headers=auth_headers)

        assert response.status_code == 200
        assert response.get_json()['synced'] == 25

    def test_meshtastic_sync_fetches_all_pages(self, app, db, ots_mock):
        from app.services.meshtastic_sync import MeshtasticSyncService
        channels = [{'id': i, 'name': f'channel_{i}', 'url': f'https://meshtastic.org/e/#chan{i}'}
                    for i in range(25)]
        ots_mock.get(f'{OTS_URL}/api/meshtastic/channel', json=paginated(channels))

        created, updated, errors = MeshtasticSyncService.sync_from_ots()

        assert errors == []
        assert created + updated == 25
