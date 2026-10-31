"""id_token verification against a provider's JWKS (app/api_v1/oidc.py)"""
import base64
import json

import pytest
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, OctKey, RSAKey

from app.api_v1.oidc import _decode_id_token

CLAIMS = {'sub': 'user-1', 'nonce': 'abc'}


def _b64(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b'=').decode()


def _sign(key, alg='RS256', kid=True):
    header = {'alg': alg}
    if kid:
        header['kid'] = key.kid
    return jwt.encode(header, CLAIMS, key, algorithms=[alg])


def _jwks(*keys):
    return {'keys': [k.as_dict(private=False) for k in keys]}


@pytest.fixture(scope='module')
def rsa():
    return RSAKey.generate_key(2048, parameters={'kid': 'rsa-1'}, private=True)


@pytest.fixture(scope='module')
def other_rsa():
    return RSAKey.generate_key(2048, parameters={'kid': 'rsa-2'}, private=True)


@pytest.fixture(scope='module')
def ec():
    return ECKey.generate_key('P-256', parameters={'kid': 'ec-1'}, private=True)


def test_valid_tokens_return_claims(rsa, other_rsa, ec):
    jwks = _jwks(rsa, other_rsa, ec)
    assert _decode_id_token(_sign(other_rsa), jwks) == CLAIMS
    assert _decode_id_token(_sign(ec, 'ES256'), jwks) == CLAIMS
    assert _decode_id_token(_sign(rsa, 'RS384'), jwks) == CLAIMS


def test_token_without_kid_needs_a_single_key(rsa, other_rsa):
    assert _decode_id_token(_sign(rsa, kid=False), _jwks(rsa)) == CLAIMS
    with pytest.raises(JoseError):
        _decode_id_token(_sign(rsa, kid=False), _jwks(rsa, other_rsa))


def test_tampered_payload_is_rejected(rsa):
    header, _, signature = _sign(rsa).split('.')
    forged = '.'.join([header, _b64({'sub': 'someone-else', 'nonce': 'abc'}), signature])
    with pytest.raises(JoseError):
        _decode_id_token(forged, _jwks(rsa))


def test_unknown_signing_key_is_rejected(rsa, other_rsa):
    with pytest.raises(JoseError):
        _decode_id_token(_sign(other_rsa), _jwks(rsa))


def test_unsigned_token_is_rejected(rsa):
    unsigned = _b64({'alg': 'none'}) + '.' + _b64(CLAIMS) + '.'
    with pytest.raises(JoseError):
        _decode_id_token(unsigned, _jwks(rsa))


def test_hmac_with_the_public_key_as_secret_is_rejected(rsa):
    secret = OctKey.import_key(json.dumps(rsa.as_dict(private=False)).encode())
    forged = jwt.encode({'alg': 'HS256', 'kid': rsa.kid}, CLAIMS, secret, algorithms=['HS256'])
    with pytest.raises(JoseError):
        _decode_id_token(forged, _jwks(rsa))
