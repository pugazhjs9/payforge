"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Header, Request

from payforge.db import SessionLocal
from payforge.errors import AppError, Unauthorized
from payforge.models import Merchant
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
