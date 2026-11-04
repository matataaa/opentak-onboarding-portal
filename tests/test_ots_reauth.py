"""
OTSClient logs in again once when OTS rejects a token it accepted before (OTS SECRET_KEY
changed, service account password reset), so the shared otsClient recovers without a restart.
"""
import pytest
import requests_mock

from app.exceptions import AuthenticationError, AuthorizationError, BadRequestError
from app.ots import OTSClient

OTS_URL = 'http://localhost:8080'
LOGIN = f'{OTS_URL}/api/login'
ME = f'{OTS_URL}/api/me'


def _login_ok(token):
    return {'json': {'response': {'user': {'authentication_token': token}}}, 'status_code': 200}


def _me(status):
    return {'json': {'username': 'svc'} if status == 200 else {'error': 'no'}, 'status_code': status}


@pytest.fixture
def ots():
    with requests_mock.Mocker() as m:
        yield m


def _calls(m, url):
    return [r for r in m.request_history if r.url.split('?')[0] == url]


@pytest.mark.parametrize('status', [401, 403])
def test_rejected_token_logs_in_again_once(ots, status):
    ots.post(LOGIN, [_login_ok('first'), _login_ok('second')])
    ots.get(ME, [_me(200), _me(status), _me(200)])
    client = OTSClient(OTS_URL, 'svc', 'pw')
    client.get_me()

    result = client.get_me()

    assert result == {'response': {'username': 'svc'}, 'status_code': 200}
    assert len(_calls(ots, LOGIN)) == 2
    me = _calls(ots, ME)
    assert len(me) == 3
    assert me[1].headers['Authentication-Token'] == 'first'
    assert me[2].headers['Authentication-Token'] == 'second'
    assert client.auth_token == 'second'


def test_still_rejected_after_relogin_raises_without_looping(ots):
    ots.post(LOGIN, [_login_ok('first'), _login_ok('second')])
    ots.get(ME, [_me(200), _me(401), _me(401)])
    client = OTSClient(OTS_URL, 'svc', 'pw')
    client.get_me()

    with pytest.raises(AuthorizationError):
        client.get_me()
    assert len(_calls(ots, LOGIN)) == 2
    assert len(_calls(ots, ME)) == 3


def test_bad_request_is_not_retried(ots):
    ots.post(LOGIN, [_login_ok('first')])
    ots.get(ME, [_me(200), _me(400)])
    client = OTSClient(OTS_URL, 'svc', 'pw')
    client.get_me()

    with pytest.raises(BadRequestError):
        client.get_me()
    assert len(_calls(ots, LOGIN)) == 1
    assert len(_calls(ots, ME)) == 2


def test_failing_relogin_raises_authentication_error(ots):
    ots.post(LOGIN, [_login_ok('first'), {'json': {'error': 'bad credentials'}, 'status_code': 400}])
    ots.get(ME, [_me(200), _me(401)])
    client = OTSClient(OTS_URL, 'svc', 'pw')
    client.get_me()

    with pytest.raises(AuthenticationError):
        client.get_me()
    assert len(_calls(ots, LOGIN)) == 2
    assert len(_calls(ots, ME)) == 2


def test_first_call_without_token_logs_in_only_once(ots):
    ots.post(LOGIN, [_login_ok('first'), _login_ok('second')])
    ots.get(ME, [_me(403), _me(200)])
    client = OTSClient(OTS_URL, 'svc', 'pw')

    with pytest.raises(Exception) as exc:
        client.get_me()
    assert exc.value.args[0]['status_code'] == 403
    assert len(_calls(ots, LOGIN)) == 1
    assert len(_calls(ots, ME)) == 1
