"""
The access matrix in tests/rbac_golden.json was generated from the role-based guards, before they
moved to permissions: every built-in role must keep exactly this access. See tests/rbac_matrix.py.
"""
import json

import pytest

from app.rbac import ADMIN_ROLES
from tests.rbac_matrix import ANONYMOUS, GOLDEN_PATH, IDENTITIES, NO_ROLE, Matrix, to_golden

# Routes that need no login at all (a successful call needs a token from a mail or link at most).
PUBLIC_ENDPOINTS = {
    'api_v1.approve_registration',  # emailed approval token
    'api_v1.create_kiosk_session',  # only with kiosk_enrollment_enabled
    'api_v1.download_tak_profile_by_token',  # single-use download token
    'api_v1.forgot_password',
    'api_v1.get_kiosk_session_status',  # by unguessable session id
    'api_v1.get_oidc_providers',
    'api_v1.get_settings',  # public subset for the login page
    'api_v1.get_version',
    'api_v1.join_event',  # event link token
    'api_v1.login',
    'api_v1.oidc_authorize',
    'api_v1.oidc_callback',
    'api_v1.register',
    'api_v1.reject_registration',  # emailed approval token
    'api_v1.request_magic_link',
    'api_v1.reset_password',
    'api_v1.serve_upload',  # logo and login background
    'api_v1.track_email_open',
    'api_v1.validate_onboarding_code',
    'api_v1.verify_email',
    'api_v1.verify_magic_link',
    'serve_spa',
    'static',
    'swagger_spec',
    'swagger_ui.show',
    'swagger_ui.static',
}
# Routes any logged-in user may call; whatever they return is scoped to that user in the handler.
SELF_SERVICE_ENDPOINTS = {
    'api_v1.authenticate_kiosk_session',
    'api_v1.change_password',
    'api_v1.check_approver_status',
    'api_v1.claim_radio',
    'api_v1.claim_radio_by_node_id',
    'api_v1.compare_config',  # own radio, with user_validate_radio_enabled
    'api_v1.complete_profile',
    'api_v1.create_download_token',
    'api_v1.dismiss_announcement',
    'api_v1.download_tak_profile',
    'api_v1.enroll_radio',
    'api_v1.get_announcement_history',
    'api_v1.get_atak_qr_string',
    'api_v1.get_current_user',
    'api_v1.get_itak_qr_string',
    'api_v1.get_meshtastic_config',
    'api_v1.get_meshtastic_configs',
    'api_v1.get_meshtastic_group',
    'api_v1.get_meshtastic_groups',
    'api_v1.get_my_approvals',
    'api_v1.get_program_config',  # own radio, with user_program_radio_enabled
    'api_v1.get_radio',
    'api_v1.get_radio_by_node_id',
    'api_v1.get_radios',
    'api_v1.get_tak_profile',
    'api_v1.get_tak_profiles',
    'api_v1.get_unread_count',
    'api_v1.get_user',  # self
    'api_v1.get_user_announcements',
    'api_v1.get_user_permissions',
    'api_v1.mark_announcement_read',
    'api_v1.refresh',
    'api_v1.set_password',
    'api_v1.update_user',  # self
}
OPEN_ENDPOINTS = PUBLIC_ENDPOINTS | SELF_SERVICE_ENDPOINTS
# Public, but they answer 403 unless the login method is on or the token is real
PUBLIC_BUT_REFUSED_IN_THE_MATRIX = {'api_v1.download_tak_profile_by_token', 'api_v1.oidc_authorize'}


@pytest.fixture(scope='module')
def matrix():
    with Matrix() as m:
        yield m


@pytest.fixture(scope='module')
def live(matrix):
    return matrix.run()


def status(cell):
    return int(cell.split('|')[0].split('~')[0])


def endpoint_of(matrix, case):
    adapter = matrix.app.url_map.bind('localhost')
    return adapter.match(matrix._path(case, NO_ROLE).split('?')[0], method=case.method)[0]


def test_matches_the_golden_file(live):
    with open(GOLDEN_PATH) as f:
        golden = json.load(f)
    current = to_golden(live)
    differences = []
    for name in sorted(set(golden) | set(current)):
        was = {i: cell for cell, ids in golden.get(name, {}).items() for i in ids}
        now = {i: cell for cell, ids in current.get(name, {}).items() for i in ids}
        differences += [f'{name} / {i}: {was.get(i)} -> {now.get(i)}'
                        for i in sorted(set(was) | set(now)) if was.get(i) != now.get(i)]
    assert not differences, f'{len(differences)} differences:\n' + '\n'.join(differences[:100])


def test_only_the_spa_catch_all_may_lack_a_frontend_build(matrix):
    class Response:
        status_code = 404

        @staticmethod
        def get_json(silent=False):
            return {'error': 'Frontend not built. Run: cd frontend && npm run build'}
    assert matrix._is_unbuilt_frontend(matrix.case('spa path'), NO_ROLE, Response)
    assert matrix._is_unbuilt_frontend(matrix.case('spa root'), NO_ROLE, Response)
    assert not matrix._is_unbuilt_frontend(matrix.case('list users'), NO_ROLE, Response)
    assert not matrix._is_unbuilt_frontend(matrix.case('static file'), NO_ROLE, Response)


def test_every_route_and_method_has_a_case(matrix):
    adapter = matrix.app.url_map.bind('localhost')
    covered = {(adapter.match(matrix._path(c, NO_ROLE).split('?')[0], method=c.method)[0], c.method)
               for c in matrix.cases}
    routes = {(rule.endpoint, method) for rule in matrix.app.url_map.iter_rules()
              for method in rule.methods - {'HEAD', 'OPTIONS'}}
    assert routes - covered == set()


def test_matrix_can_tell_roles_apart(live):
    """Not all 401s or 404s: the calls reach the guards and the rows behind them."""
    for identity in ADMIN_ROLES:
        statuses = [status(row[identity]) for row in live.values()]
        allowed, denied = sum(s < 400 for s in statuses), statuses.count(403)
        assert allowed >= 50 and denied >= 8, identity
        assert statuses.count(401) == 0, identity
        assert statuses.count(404) <= 5, identity
    per_role = {i: frozenset(n for n, row in live.items() if status(row[i]) < 400) for i in ADMIN_ROLES}
    assert len(set(per_role.values())) == len(ADMIN_ROLES)  # no two roles have the same access
    administrator = [status(row['administrator']) for row in live.values()]
    assert sum(s < 400 for s in administrator) > 250
    assert all(status(row[ANONYMOUS]) in (200, 302, 400, 401, 403, 404) for row in live.values())


def test_a_delete_does_not_leak_into_later_calls(matrix):
    for _ in range(2):
        assert matrix.call(matrix.case('delete user'), 'administrator')[0] == '200'
        assert matrix.call(matrix.case('get other user'), 'administrator')[0].startswith('200~')
        assert matrix.call(matrix.case('delete logo'), 'administrator')[0] == '200'
        assert matrix.call(matrix.case('upload file'), ANONYMOUS)[0] == '200'


def test_no_route_is_open_by_accident(matrix, live):
    reached_anonymously, reached_without_role = set(), set()
    for case in matrix.cases:
        endpoint = endpoint_of(matrix, case)
        if status(live[case.name][ANONYMOUS]) not in (401, 403):
            reached_anonymously.add(endpoint)
        if status(live[case.name][NO_ROLE]) not in (401, 403):
            reached_without_role.add(endpoint)
    assert reached_anonymously - PUBLIC_ENDPOINTS == set()
    assert reached_without_role - OPEN_ENDPOINTS == set()
    # The allowlist must not outlive what it allows
    assert PUBLIC_ENDPOINTS - PUBLIC_BUT_REFUSED_IN_THE_MATRIX - reached_anonymously == set()
    assert SELF_SERVICE_ENDPOINTS - reached_without_role == set()


# ---------------------------------------------------------------- scoped and masked responses

def body(matrix, case, identity):
    cell, resp = matrix.call(matrix.case(case), identity)
    assert status(cell) == 200, (case, identity, cell)
    return resp.get_json()


def names(items):
    return sorted(i['name'] for i in items)


ALL_RADIOS = ['assigned-to-no-role', 'owned-by-no-role', 'someone-else', 'unassigned']


@pytest.mark.parametrize('identity', ['administrator', 'radio_admin', 'radio_readonly'])
def test_radio_viewers_see_all_radios(matrix, identity):
    assert names(body(matrix, 'list radios', identity)['radios']) == ALL_RADIOS


def test_others_see_only_radios_assigned_to_them(matrix):
    assert names(body(matrix, 'list radios', NO_ROLE)['radios']) == ['assigned-to-no-role']
    assert body(matrix, 'list radios', 'user_admin')['radios'] == []
    assert body(matrix, 'list radios', 'kiosk')['radios'] == []  # the account behind it is an administrator


def test_single_radio_also_goes_by_owner(matrix):
    assert matrix.call(matrix.case('get radio assigned to no_role'), NO_ROLE)[0] == '200'
    assert matrix.call(matrix.case('get radio owned by no_role'), NO_ROLE)[0] == '200'
    assert status(matrix.call(matrix.case('get radio of someone else'), NO_ROLE)[0]) == 403
    assert matrix.call(matrix.case('get radio of someone else'), 'radio_readonly')[0] == '200'


def test_radio_readonly_may_program_and_read_claim_codes(matrix):
    for case in ('claim code', 'program config', 'compare config'):
        assert matrix.call(matrix.case(case), 'radio_readonly')[0] == '200', case
        assert status(matrix.call(matrix.case(case), NO_ROLE)[0]) == 403, case
    assert status(matrix.call(matrix.case('update radio'), 'radio_readonly')[0]) == 403


def test_users_may_program_their_own_radio_when_enabled(matrix):
    assert matrix.call(matrix.case('program config, users allowed, own radio'), NO_ROLE)[0] == '200'
    assert matrix.call(matrix.case('compare config, users allowed, own radio'), NO_ROLE)[0] == '200'
    assert status(matrix.call(matrix.case('program config, users allowed, other radio'), NO_ROLE)[0]) == 403
    assert status(matrix.call(matrix.case('program config'), NO_ROLE)[0]) == 403


@pytest.mark.parametrize('identity,visible', [('administrator', True), ('announcement_admin', True),
                                              ('announcement_readonly', False)])
def test_announcement_masks(matrix, identity, visible):
    listed = body(matrix, 'list announcements (admin)', identity)['announcements']
    sent = next(a for a in listed if a['title'] == 'sent')
    assert sent['emailLastError'] == ('raw smtp error' if visible else None)

    detail = body(matrix, 'get announcement (admin)', identity)
    assert detail['emailLastError'] == ('raw smtp error' if visible else None)
    assert detail['emailPending'][0]['username'] == 'matrix_target'
    assert detail['emailPending'][0]['email'] == ('matrix_target@example.com' if visible else None)
    assert detail['emailFailures'][0]['email'] == ('matrix_target@example.com' if visible else None)
    assert detail['emailFailures'][0]['error'] == ('smtp secret detail' if visible else None)


def test_tak_profile_visibility(matrix):
    assert names(body(matrix, 'list profiles', 'administrator')['profiles']) == ['private', 'public', 'role-bound']
    # Only the administrator sees everything: the module roles get what any user gets
    for identity in ('tak_profile_admin', 'tak_profile_readonly', NO_ROLE):
        assert names(body(matrix, 'list profiles', identity)['profiles']) == ['public'], identity
        assert status(matrix.call(matrix.case('get profile private'), identity)[0]) == 403, identity
    assert names(body(matrix, 'list profiles', 'audience')['profiles']) == ['public', 'role-bound']
    assert matrix.call(matrix.case('get profile role-bound'), 'audience')[0] == '200'


def test_tak_profile_files_honour_readonly(matrix):
    assert matrix.call(matrix.case('profile files'), 'tak_profile_readonly')[0] == '200'
    assert status(matrix.call(matrix.case('profile files'), NO_ROLE)[0]) == 403


def test_meshtastic_readonly_sees_channels_but_not_groups_admin(matrix):
    identity = 'meshtastic_readonly'
    assert names(body(matrix, 'meshtastic admin list', identity)['configs']) == ['private', 'public', 'role-bound']
    assert matrix.call(matrix.case('get meshtastic private'), identity)[0] == '200'
    for case in ('meshtastic groups admin', 'ungrouped channels', 'meshtastic channel groups',
                 'get meshtastic group private'):
        assert status(matrix.call(matrix.case(case), identity)[0]) == 403, case
        assert matrix.call(matrix.case(case), 'meshtastic_admin')[0].startswith('200'), case


def test_meshtastic_lists_have_no_automatic_admin_access(matrix):
    for identity in ('administrator', 'meshtastic_admin'):
        assert names(body(matrix, 'list meshtastic groups', identity)['groups']) == ['public'], identity
        assert names(body(matrix, 'list meshtastic', identity)['configs']) == ['public'], identity
    assert names(body(matrix, 'list meshtastic', 'audience')['configs']) == ['public', 'role-bound']


@pytest.mark.parametrize('identity', sorted(set(IDENTITIES) - {'administrator'}))
def test_impersonation_is_administrator_only(matrix, identity):
    assert status(matrix.call(matrix.case('impersonate'), identity)[0]) in (401, 403)
