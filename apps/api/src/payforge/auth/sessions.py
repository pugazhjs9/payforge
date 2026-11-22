"""Dashboard sessions.

The browser only holds a random ID in an httpOnly cookie; the data lives in Redis
under sess:<id>. Deleting the key logs the user out immediately, something a JWT
can't do.
"""

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass

from fastapi import Response

from payforge.config import settings
from payforge.models import User, UserRole
from payforge.redis import redis

SESSION_COOKIE = "pf_sid"
_SESSION_ID = re.compile(r"[\w-]{43}")  # 32 random bytes, base64url


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    merchant_id: str
    role: UserRole
    csrf: str  # per-session CSRF token
    created_at: float  # epoch seconds, for the absolute lifetime limit


def _key(session_id: str) -> str:
    return f"sess:{session_id}"


async def create_session(user: User) -> Session:
    session = Session(
        id=secrets.token_urlsafe(32),  # 256 bits: unguessable
        user_id=user.id,
        merchant_id=user.merchant_id,
        role=user.role,
        csrf=secrets.token_urlsafe(32),
        created_at=time.time(),
    )
    data = {k: v for k, v in asdict(session).items() if k != "id"}
    await redis.set(_key(session.id), json.dumps(data), ex=settings.session_idle_seconds)
    return session


async def get_session(session_id: str) -> Session | None:
    if not _SESSION_ID.fullmatch(session_id):
        return None
    raw = await redis.get(_key(session_id))
    if raw is None:
        return None
    data = json.loads(raw)
    if time.time() - data["created_at"] > settings.session_max_seconds:
        await redis.delete(_key(session_id))  # absolute limit reached, even if active
        return None
    await redis.expire(_key(session_id), settings.session_idle_seconds)  # sliding idle timeout
    return Session(id=session_id, **{**data, "role": UserRole(data["role"])})


async def destroy_session(session_id: str) -> None:
    await redis.delete(_key(session_id))


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=settings.session_max_seconds,
        httponly=True,  # JavaScript can't read it, so an XSS bug can't steal it
        secure=settings.environment == "production",  # HTTPS only in production
        samesite="lax",  # not sent on cross-site POSTs
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
