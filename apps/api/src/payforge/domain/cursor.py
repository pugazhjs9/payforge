"""Cursor pagination: "give me the next page after this item".

The bookmark is the last item's (created_at, id); the id breaks ties between rows
with the same timestamp. Clients get it as an opaque base64url string.
"""

import base64
import json
from dataclasses import dataclass
from datetime import datetime

from payforge.errors import AppError


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    id: str


def encode_cursor(cursor: Cursor) -> str:
    raw = json.dumps({"t": cursor.created_at.isoformat(), "id": cursor.id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> Cursor:
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        created_at = datetime.fromisoformat(data["t"])
        if created_at.tzinfo is None or not isinstance(data["id"], str) or not data["id"]:
            raise ValueError("bad cursor")
        return Cursor(created_at=created_at, id=data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AppError(
            400, "invalid_cursor", "The cursor is not valid. Use next_cursor from a previous page."
        ) from exc
