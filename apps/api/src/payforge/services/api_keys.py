"""API keys: credentials for merchant servers.

The secret is shown once. Only SHA-256(secret) is stored: a 32-byte random secret
can't be guessed, so a fast hash is safe (unlike passwords, which need argon2).
The readable prefix makes leaked keys easy to spot, e.g. by secret scanners.
"""

import hashlib
import secrets
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, update

from payforge.db import SessionLocal
from payforge.errors import NotFound, OAuthError
from payforge.ids import new_id
from payforge.models import ApiKey, utcnow
from payforge.redis import redis

Scope = Literal["payments:read", "payments:write", "refunds:write"]
SCOPES: tuple[Scope, ...] = ("payments:read", "payments:write", "refunds:write")
SECRET_PREFIX = "pf_sk_test_"  # noqa: S105  (a public prefix, not a secret)
REVOKED_KEYS = "revoked_keys"  # Redis set checked on every API request


@dataclass(frozen=True)
class NewApiKey:
    key: ApiKey
    secret: str


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def create_api_key(merchant_id: str, name: str, scopes: list[str]) -> NewApiKey:
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(
        id=new_id("key"),
        merchant_id=merchant_id,
        name=name,
        secret_hash=_hash(secret),
        secret_last4=secret[-4:],
        scopes=sorted(set(scopes)),
    )
    async with SessionLocal.begin() as session:
        session.add(key)
    return NewApiKey(key=key, secret=secret)


async def list_api_keys(merchant_id: str) -> list[ApiKey]:
    async with SessionLocal() as session:
        return list(
            await session.scalars(
                select(ApiKey).where(ApiKey.merchant_id == merchant_id).order_by(ApiKey.created_at)
            )
        )


async def revoke_api_key(merchant_id: str, key_id: str) -> None:
    async with SessionLocal.begin() as session:
        key = await session.scalar(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.merchant_id == merchant_id)
        )
        if key is None:
            raise NotFound("API key")
        key.revoked_at = key.revoked_at or utcnow()
    # tokens already issued for this key stop working at once (see require_access_token)
    await redis.sadd(REVOKED_KEYS, key_id)


async def verify_client_credentials(key_id: str, secret: str) -> ApiKey:
    async with SessionLocal() as session:
        key = await session.get(ApiKey, key_id)
    valid = key is not None and key.revoked_at is None
    # compare in constant time even when the key doesn't exist
    expected = key.secret_hash if key is not None else _hash("")
    if not secrets.compare_digest(_hash(secret), expected) or not valid or key is None:
        raise OAuthError(401, "invalid_client", "Unknown key or wrong secret.")
    async with SessionLocal.begin() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=utcnow())
        )
    return key
