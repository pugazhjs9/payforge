"""Merchant logos: uploaded by the browser straight to S3, then checked by us.

1. upload_form(): a presigned POST that only accepts ONE key, ONE content type and at
   most MAX_BYTES. S3 enforces all three; the file never passes through the API.
2. The browser uploads, then tells us the key. set_logo() checks the object really is
   what it claims: the signed Content-Type only proves what the browser SAID it was,
   so we read the first bytes (the "magic number" every PNG / JPEG file starts with).
"""

from typing import Any

from sqlalchemy import select, update

from payforge import storage
from payforge.db import SessionLocal
from payforge.errors import ValidationFailed
from payforge.ids import new_id
from payforge.models import Merchant

MAX_BYTES = 512 * 1024
MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
URL_SECONDS = 3600


def _prefix(merchant_id: str) -> str:
    return f"logos/{merchant_id}/"


def upload_form(merchant_id: str, content_type: str) -> dict[str, Any]:
    key = _prefix(merchant_id) + new_id("logo")
    return {**storage.presigned_post(key, content_type, MAX_BYTES), "key": key}


async def set_logo(merchant_id: str, key: str) -> str:
    if not key.startswith(_prefix(merchant_id)):
        raise ValidationFailed("key", "not an upload of this merchant")
    info = await storage.head(key)
    if info is None:
        raise ValidationFailed("key", "nothing was uploaded under this key")
    magic = MAGIC.get(info.content_type)
    is_image = (
        magic is not None
        and info.size <= MAX_BYTES
        and await storage.read_start(key, len(magic)) == magic
    )
    if not is_image:
        await storage.delete(key)
        raise ValidationFailed("key", "the file is not a PNG or JPEG image")

    async with SessionLocal.begin() as session:
        old = await session.scalar(select(Merchant.logo_key).where(Merchant.id == merchant_id))
        await session.execute(
            update(Merchant).where(Merchant.id == merchant_id).values(logo_key=key)
        )
    if old and old != key:
        await storage.delete(old)  # after the commit: never point at a deleted file
    return storage.presigned_get(key, URL_SECONDS)


def logo_url(merchant: Merchant) -> str | None:
    return storage.presigned_get(merchant.logo_key, URL_SECONDS) if merchant.logo_key else None
