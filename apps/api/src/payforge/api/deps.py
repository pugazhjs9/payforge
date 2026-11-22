"""Shared FastAPI dependencies."""

import secrets
from typing import Annotated, Any

from fastapi import Cookie, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from payforge.auth.sessions import SESSION_COOKIE, Session, get_session
from payforge.auth.tokens import AccessToken, verify_access_token
from payforge.config import settings
from payforge.db import SessionLocal
from payforge.errors import AppError, Forbidden, Unauthorized
from payforge.models import User, UserRole
from payforge.redis import redis
from payforge.services.api_keys import REVOKED_KEYS, Scope
from payforge.services.payments import MerchantRef


async def require_json(request: Request) -> None:
    """A request WITH a body must send JSON. No body at all is fine (e.g. a full refund).
    Many clients send "Content-Length: 0" for an empty POST, so that counts as no body."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return
    length = request.headers.get("content-length")
    has_body = "transfer-encoding" in request.headers or length not in (None, "0")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if has_body and content_type != "application/json":
        raise AppError(415, "unsupported_media_type", "Send the body as application/json.")


async def optional_session(
    pf_sid: Annotated[str | None, Cookie(alias=SESSION_COOKIE, include_in_schema=False)] = None,
) -> Session | None:
    """The dashboard session, if the browser sent a valid session cookie. Never rejects."""
    return await get_session(pf_sid) if pf_sid else None


async def require_session(
    session: Annotated[Session | None, Depends(optional_session)],
) -> Session:
    if session is None:
        raise Unauthorized("not_logged_in", "Log in first.")
    return session


CurrentSession = Annotated[Session, Depends(require_session)]


def require_role(*roles: UserRole) -> Any:
    """Checks the user's CURRENT role in the database, not the one cached in the session,
    so a demoted or removed user loses access immediately."""

    async def check(session: CurrentSession) -> Session:
        async with SessionLocal() as db:
            role = await db.scalar(select(User.role).where(User.id == session.user_id))
        if role is None:
            raise Unauthorized("not_logged_in", "Log in first.")
        if role not in roles:
            raise Forbidden(
                "insufficient_role", f"This needs one of these roles: {', '.join(roles)}."
            )
        return session

    return Depends(check)


# CSRF: the browser attaches our session cookie to EVERY request to us, even one started
# by an evil website (e.g. a hidden form). Three layers stop that:
#   1. SameSite=Lax on the cookie (the browser won't send it on cross-site POSTs);
#   2. check_origin: browsers send an Origin header; another site's origin is refused;
#   3. require_csrf_token: a per-session secret other sites can't read, which our
#      dashboard JavaScript sends back in X-CSRF-Token.
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def check_origin(request: Request) -> None:
    """Refuse state-changing requests whose Origin is not our dashboard.
    No Origin header means the caller is not a browser, so CSRF doesn't apply."""
    origin = request.headers.get("origin")
    if (
        request.method in UNSAFE_METHODS
        and origin is not None
        and origin != settings.dashboard_origin
    ):
        raise Forbidden("bad_origin", "Requests from other sites are not allowed.")


async def require_csrf_token(
    request: Request, session: Annotated[Session | None, Depends(optional_session)]
) -> None:
    """State-changing requests made with a session cookie must carry its CSRF token."""
    if request.method not in UNSAFE_METHODS or session is None:
        return
    sent = request.headers.get("x-csrf-token", "")
    if not secrets.compare_digest(sent.encode(), session.csrf.encode()):  # constant time
        raise Forbidden("csrf_token_invalid", "Missing or wrong X-CSRF-Token header.")


_bearer = HTTPBearer(auto_error=False, description="Access token from POST /v1/auth/token")


async def require_access_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AccessToken:
    """Merchant servers authenticate with a short-lived JWT: checked with the public key,
    no database lookup. One cheap Redis check makes revoked keys stop working at once."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise Unauthorized("missing_token", "Send Authorization: Bearer <access token>.")
    token = verify_access_token(credentials.credentials)
    if await redis.sismember(REVOKED_KEYS, token.key_id):
        raise Unauthorized("key_revoked", "The API key behind this token was revoked.")
    return token


def require_scope(scope: Scope) -> Any:
    """The merchant acting, if the token carries `scope`. The merchant ID always comes
    from the token, never from the URL or body: that is what isolates tenants."""

    async def check(token: Annotated[AccessToken, Depends(require_access_token)]) -> MerchantRef:
        if scope not in token.scopes:
            raise Forbidden("insufficient_scope", f"This needs the {scope} scope.")
        return MerchantRef(id=token.merchant_id)

    return Depends(check)


PaymentsRead = Annotated[MerchantRef, require_scope("payments:read")]
PaymentsWrite = Annotated[MerchantRef, require_scope("payments:write")]
RefundsWrite = Annotated[MerchantRef, require_scope("refunds:write")]
