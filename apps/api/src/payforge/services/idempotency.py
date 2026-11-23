"""Idempotency keys: make retrying a POST safe.

A merchant's server sends `Idempotency-Key: <unique id>` with each create request. If
the response is lost (timeout, dropped connection) and it retries, we recognise the
key and return the first response instead of charging the card a second time.

Life of a key (one row in idempotency_keys):

1. claim     INSERT the key as `in_progress` with a random lock_token. The primary key
             (merchant_id, key) makes this atomic: of 50 parallel requests, exactly one
             INSERT succeeds. The others read the row and get a replay, a 409 (still
             running) or a 422 (same key, different request).
2. link      create the payment AND save its id on the key, in ONE transaction.
3. complete  save the response; every later retry replays it.

A worker can crash between steps. Its key then stays `in_progress` and the lock goes
stale; after LOCK_TIMEOUT the next retry takes the lock over and resumes from the
saved resource_id (or starts over if there is none). Every write after the claim
checks lock_token (a "fencing token"), so a slow worker that lost its lock can't
overwrite the new owner's work.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from payforge.db import SessionLocal
from payforge.errors import AppError, Conflict
from payforge.logging import log
from payforge.models import IdempotencyKey, IdempotencyStatus, utcnow

LOCK_TIMEOUT = timedelta(seconds=60)  # longer than any request (the bank timeout is 3 s)
KEY_TTL = timedelta(hours=24)  # a retry after a day is a new request

ER_DUP_ENTRY = 1062
ER_LOCK_DEADLOCK = 1213


@dataclass
class Claim:
    """This request owns the key. resource_id is set when resuming a crashed attempt."""

    merchant_id: str
    key: str
    lock_token: str
    resource_id: str | None = None


@dataclass(frozen=True)
class Replay:
    """The key's request already finished: send this response again."""

    status: int
    body: dict[str, Any]


def fingerprint(method: str, path: str, body: Any) -> str:
    """Same key + different request = a client bug, so we remember what the request was.
    The body is canonical JSON (sorted keys), so key order and spacing don't matter."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{method} {path}\n{canonical}".encode()).hexdigest()


def in_use() -> Conflict:
    return Conflict(
        "idempotency_key_in_use",
        "A request with this Idempotency-Key is still being processed. Retry shortly.",
        headers={"Retry-After": "1"},
    )


def _mysql_error(exc: DBAPIError) -> int | None:
    args = getattr(exc.orig, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def _this_key(merchant_id: str, key: str) -> list[ColumnElement[bool]]:
    return [IdempotencyKey.merchant_id == merchant_id, IdempotencyKey.key == key]


async def _update(session: AsyncSession, where: list[ColumnElement[bool]], **values: Any) -> bool:
    """A conditional UPDATE; True if it matched the row (compare-and-set)."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(IdempotencyKey)
            .where(*where)
            .values(**values)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


async def claim(merchant_id: str, key: str, request_hash: str) -> Claim | Replay:
    """Step 1. Returns a Claim (go ahead) or a Replay, or raises 409 / 422."""
    for _ in range(5):
        token = secrets.token_hex(16)
        now = utcnow()
        try:
            async with SessionLocal.begin() as session:
                session.add(
                    IdempotencyKey(
                        merchant_id=merchant_id,
                        key=key,
                        request_hash=request_hash,
                        lock_token=token,
                        locked_at=now,
                        expires_at=now + KEY_TTL,
                    )
                )
            return Claim(merchant_id, key, token)
        except DBAPIError as exc:
            # duplicate key: someone used it before. A deadlock can happen when several
            # requests wait on the same new key and its first owner rolls back: retry.
            if _mysql_error(exc) not in (ER_DUP_ENTRY, ER_LOCK_DEADLOCK):
                raise
        result = await _existing_key(merchant_id, key, request_hash, token, now)
        if result is not None:
            return result
        # the row changed between our INSERT and SELECT (released, taken over): go again
    raise in_use()


async def _existing_key(
    merchant_id: str, key: str, request_hash: str, token: str, now: datetime
) -> Claim | Replay | None:
    this_key = _this_key(merchant_id, key)
    async with SessionLocal.begin() as session:
        row = await session.get(IdempotencyKey, (merchant_id, key))
        if row is None:
            return None  # released in the meantime: INSERT again

        if row.expires_at <= now:
            # an expired key not cleaned up yet: reuse it as if it were brand new
            reclaimed = await _update(
                session,
                [*this_key, IdempotencyKey.expires_at <= now],
                request_hash=request_hash,
                status=IdempotencyStatus.IN_PROGRESS,
                resource_id=None,
                response_status=None,
                response_body=None,
                lock_token=token,
                locked_at=now,
                created_at=now,
                expires_at=now + KEY_TTL,
            )
            return Claim(merchant_id, key, token) if reclaimed else None

        if row.request_hash != request_hash:
            raise AppError(
                422,
                "idempotency_key_reused",
                "This Idempotency-Key was already used for a different request. "
                "Use a new key for each new request.",
            )

        if row.status == IdempotencyStatus.COMPLETED:
            if row.response_status is None or row.response_body is None:
                raise RuntimeError("completed idempotency key without a response")
            return Replay(row.response_status, row.response_body)

        if row.lock_token is not None and row.locked_at and row.locked_at > now - LOCK_TIMEOUT:
            raise in_use()

        # in progress, but its worker crashed (stale lock) or gave up (no lock): take over
        took_over = await _update(
            session,
            [
                *this_key,
                IdempotencyKey.status == IdempotencyStatus.IN_PROGRESS,
                or_(
                    IdempotencyKey.lock_token.is_(None),
                    IdempotencyKey.locked_at <= now - LOCK_TIMEOUT,
                ),
            ],
            lock_token=token,
            locked_at=now,
        )
        if not took_over:
            return None
        # re-read now that we hold the row lock: the old owner may have linked a
        # resource after our first read, and resuming it is what prevents a 2nd payment
        resource_id = await session.scalar(select(IdempotencyKey.resource_id).where(*this_key))
        log.warning("idempotency_key_taken_over", key=key, resource_id=resource_id)
        return Claim(merchant_id, key, token, resource_id)


async def link(session: AsyncSession, claim: Claim, resource_id: str) -> None:
    """Step 2, inside the transaction that creates the resource: both commit, or neither.
    Fenced: if another request took the lock over meanwhile, this raises and the caller's
    transaction (with the new payment in it) rolls back."""
    linked = await _update(
        session,
        [
            *_this_key(claim.merchant_id, claim.key),
            IdempotencyKey.lock_token == claim.lock_token,
            IdempotencyKey.resource_id.is_(None),
        ],
        resource_id=resource_id,
    )
    if not linked:
        raise in_use()
    claim.resource_id = resource_id


async def complete(claim: Claim, status: int, body: dict[str, Any]) -> None:
    """Step 3: save the response for replays and release the lock."""
    async with SessionLocal.begin() as session:
        saved = await _update(
            session,
            [
                *_this_key(claim.merchant_id, claim.key),
                IdempotencyKey.lock_token == claim.lock_token,
            ],
            status=IdempotencyStatus.COMPLETED,
            response_status=status,
            response_body=body,
            lock_token=None,
            locked_at=None,
        )
    if not saved:  # another request took over; it will save the (same) outcome
        log.warning("idempotency_lock_lost", key=claim.key)


async def release(claim: Claim) -> None:
    """The request failed with an error.

    Nothing was created: forget the key, so a corrected retry starts fresh.
    Something was created: keep the key but unlock it, so a retry resumes right away
    instead of waiting LOCK_TIMEOUT.
    """
    mine = [
        *_this_key(claim.merchant_id, claim.key),
        IdempotencyKey.lock_token == claim.lock_token,
    ]
    try:
        async with SessionLocal.begin() as session:
            await session.execute(
                delete(IdempotencyKey).where(*mine, IdempotencyKey.resource_id.is_(None))
            )
            await session.execute(
                update(IdempotencyKey).where(*mine).values(lock_token=None, locked_at=None)
            )
    except Exception:  # never hide the original error; a stale lock expires by itself
        log.exception("idempotency_release_failed", key=claim.key)
