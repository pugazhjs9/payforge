"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Cookie, Depends, Header, Request

from payforge.auth.sessions import SESSION_COOKIE, Session, get_session
from payforge.db import SessionLocal
from payforge.errors import AppError, Forbidden, Unauthorized
from payforge.models import Merchant, UserRole
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


async def dev_merchant(
    x_merchant_id: Annotated[
        str | None, Header(description="TEMPORARY development identity")
    ] = None,
) -> MerchantRef:
    """TEMPORARY: identifies the merchant from an X-Merchant-Id header.

    Anyone can send any ID, so this is NOT authentication. It exists only until
    API keys + JWT replace it.
    """
    if not x_merchant_id:
        raise Unauthorized("missing_merchant", "Send an X-Merchant-Id header.")
    async with SessionLocal() as session:
        merchant = await session.get(Merchant, x_merchant_id)
    if merchant is None:
        raise Unauthorized("unknown_merchant", "Unknown merchant.")
    return MerchantRef(id=merchant.id, fee_bps=merchant.fee_bps)


CurrentMerchant = Annotated[MerchantRef, Depends(dev_merchant)]


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


def require_role(*roles: UserRole) -> object:
    async def check(session: CurrentSession) -> Session:
        if session.role not in roles:
            raise Forbidden(
                "insufficient_role", f"This needs one of these roles: {', '.join(roles)}."
            )
        return session

    return Depends(check)
