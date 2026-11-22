"""Access tokens for merchant servers: ES256-signed JWTs.

A JWT is a signed note, not a secret note: anyone can read the payload (it's only
base64), the signature just proves nobody changed it. ES256 is asymmetric: the
private key signs (only this service has it), the public key verifies and is
published at /.well-known/jwks.json, so other services can verify tokens without
being able to create them. `kid` lets keys be rotated.
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from payforge.config import settings
from payforge.errors import Unauthorized
from payforge.logging import log


def _load_private_key() -> ec.EllipticCurvePrivateKey:
    if settings.jwt_private_key_file:
        key = serialization.load_pem_private_key(
            Path(settings.jwt_private_key_file).read_bytes(), password=None
        )
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise TypeError("JWT_PRIVATE_KEY_FILE must hold an EC (P-256) private key")
        return key
    if settings.environment == "production":
        raise RuntimeError("JWT_PRIVATE_KEY_FILE is required in production")
    log.warning("using a throwaway JWT signing key; tokens die when the process restarts")
    return ec.generate_private_key(ec.SECP256R1())


_private_key = _load_private_key()
_public_jwk: dict[str, Any] = json.loads(ECAlgorithm.to_jwk(_private_key.public_key()))
# kid = RFC 7638 thumbprint of the public key
_thumbprint_input = json.dumps(
    {k: _public_jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True
)
KID = (
    base64.urlsafe_b64encode(hashlib.sha256(_thumbprint_input.encode()).digest())
    .decode()
    .rstrip("=")
)


@dataclass(frozen=True)
class AccessToken:
    merchant_id: str
    key_id: str
    scopes: frozenset[str]


def issue_access_token(*, merchant_id: str, key_id: str, scopes: list[str]) -> str:
    now = int(time.time())
    claims = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": merchant_id,
        "scope": " ".join(scopes),
        "key_id": key_id,
        "iat": now,
        "exp": now + settings.access_token_ttl_seconds,
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(claims, _private_key, algorithm="ES256", headers={"kid": KID})


def verify_access_token(token: str) -> AccessToken:
    try:
        if jwt.get_unverified_header(token).get("kid") != KID:
            raise Unauthorized("invalid_token", "Unknown signing key.")
        claims = jwt.decode(
            token,
            _private_key.public_key(),
            algorithms=["ES256"],  # allow-list: never "none", never a surprise algorithm
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=5,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("token_expired", "The access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("invalid_token", "The access token is not valid.") from exc
    return AccessToken(
        merchant_id=str(claims["sub"]),
        key_id=str(claims.get("key_id", "")),
        scopes=frozenset(str(claims.get("scope", "")).split()),
    )


def jwks() -> dict[str, list[dict[str, Any]]]:
    return {"keys": [{**_public_jwk, "kid": KID, "use": "sig", "alg": "ES256"}]}
