"""HTTP side of idempotency keys: the header, and running a create request once."""

import re
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from payforge.db import SessionLocal
from payforge.errors import AppError, ValidationFailed
from payforge.services import idempotency

KEY_FORMAT = re.compile(r"[\x21-\x7e]{1,255}")  # printable ASCII, no spaces; a UUID fits


async def idempotency_key(
    key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=(
                "Required. A unique value (e.g. a UUID) per request. Retrying with the same key "
                "returns the first response instead of acting twice. Kept for 24 hours."
            ),
        ),
    ] = None,
) -> str:
    if key is None:
        raise AppError(
            400,
            "idempotency_key_missing",
            "Send an Idempotency-Key header (e.g. a UUID) so that retries are safe.",
        )
    if not KEY_FORMAT.fullmatch(key):
        raise ValidationFailed("Idempotency-Key", "must be 1-255 printable ASCII characters")
    return key


IdempotencyKey = Annotated[str, Depends(idempotency_key)]


async def run_once(
    *,
    request: Request,
    response: Response,
    merchant_id: str,
    key: str,
    body: BaseModel,
    create: Callable[[AsyncSession], Awaitable[str]],
    finish: Callable[[str], Awaitable[tuple[int, BaseModel]]],
) -> JSONResponse:
    """Runs a create request at most once per key.

    create(session) inserts the resource (inside a transaction that also links it to the
    key) and returns its id; finish(id) does the rest (e.g. calls the bank) and returns
    (status, body). On a retry after a crash, create is skipped and finish resumes.
    """
    request_hash = idempotency.fingerprint(
        request.method, request.url.path, body.model_dump(mode="json")
    )
    claimed = await idempotency.claim(merchant_id, key, request_hash)
    if isinstance(claimed, idempotency.Replay):
        return _respond(claimed.status, claimed.body, response, {"Idempotent-Replayed": "true"})

    try:
        resource_id = claimed.resource_id
        if resource_id is None:
            async with SessionLocal.begin() as session:
                resource_id = await create(session)
                await idempotency.link(session, claimed, resource_id)
        status, out = await finish(resource_id)
    except Exception:
        await idempotency.release(claimed)
        raise

    payload = out.model_dump(mode="json")
    await idempotency.complete(claimed, status, payload)
    return _respond(status, payload, response)


def _respond(
    status: int, payload: object, response: Response, headers: dict[str, str] | None = None
) -> JSONResponse:
    result = JSONResponse(payload, status_code=status, headers=headers)
    # a returned Response skips FastAPI's merge of headers set by dependencies
    # (RateLimit-*), so copy them over
    result.headers.raw.extend(response.headers.raw)
    return result
