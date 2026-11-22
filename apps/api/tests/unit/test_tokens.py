import base64
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from payforge.auth.tokens import KID, issue_access_token, verify_access_token
from payforge.config import settings
from payforge.errors import Unauthorized


def b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def test_round_trip() -> None:
    token = issue_access_token(merchant_id="mer_1", key_id="key_1", scopes=["payments:read"])
    claims = verify_access_token(token)

    assert claims.merchant_id == "mer_1"
    assert claims.key_id == "key_1"
    assert claims.scopes == {"payments:read"}
    assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": KID, "typ": "JWT"}


def test_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "access_token_ttl_seconds", -60)
    token = issue_access_token(merchant_id="mer_1", key_id="key_1", scopes=[])
    with pytest.raises(Unauthorized) as err:
        verify_access_token(token)
    assert err.value.code == "token_expired"


def test_wrong_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    token = issue_access_token(merchant_id="mer_1", key_id="key_1", scopes=[])
    monkeypatch.setattr(settings, "jwt_audience", "some-other-api")
    with pytest.raises(Unauthorized):
        verify_access_token(token)


def test_tampered_payload_is_rejected() -> None:
    header, payload, signature = issue_access_token(
        merchant_id="mer_1", key_id="k", scopes=[]
    ).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["sub"] = "mer_someone_else"
    with pytest.raises(Unauthorized):
        verify_access_token(f"{header}.{b64(claims)}.{signature}")


def test_alg_none_is_rejected() -> None:
    """The classic attack: an unsigned token that says it needs no signature."""
    header = b64({"alg": "none", "typ": "JWT", "kid": KID})
    payload = b64(
        {"sub": "mer_1", "iss": "payforge", "aud": "payforge-api", "iat": 1, "exp": 9999999999}
    )
    with pytest.raises(Unauthorized):
        verify_access_token(f"{header}.{payload}.")


def test_token_signed_by_another_key_is_rejected() -> None:
    other_key = ec.generate_private_key(ec.SECP256R1())
    forged = jwt.encode(
        {"sub": "mer_1", "iss": "payforge", "aud": "payforge-api", "iat": 1, "exp": 9999999999},
        other_key,
        algorithm="ES256",
        headers={"kid": KID},
    )
    with pytest.raises(Unauthorized):
        verify_access_token(forged)
