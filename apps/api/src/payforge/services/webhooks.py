"""Webhooks: endpoints merchants register, and delivering events to them.

Guarantees (documented for merchants):
- AT LEAST ONCE: an event may arrive twice (e.g. we crash after sending, before
  recording it). Receivers dedupe by event id.
- NOT IN ORDER: payment.succeeded can overtake an earlier event. Receivers use the
  resource in the event, or GET it for the latest state.

A delivery's state lives in MySQL (the source of truth). RabbitMQ only carries
"attempt n of delivery X is due" messages. Each attempt is CLAIMED with a
compare-and-set on the attempt count, so a duplicate or stale message does nothing.
If a message is lost, or a worker dies mid-attempt, the sweeper finds the overdue
delivery in MySQL and sends a fresh message.
"""

import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import CursorResult

from payforge import net
from payforge.api.v1.schemas import EventOut
from payforge.config import settings
from payforge.db import SessionLocal
from payforge.errors import Conflict, NotFound, ValidationFailed
from payforge.ids import new_id
from payforge.logging import log
from payforge.messaging import MAX_ATTEMPTS, RETRY_DELAYS
from payforge.models import (
    DeliveryStatus,
    Event,
    WebhookAttempt,
    WebhookDelivery,
    WebhookEndpoint,
    utcnow,
)
from payforge.webhook_signing import SIGNATURE_HEADER, header_value

SECRET_PREFIX = "whsec_"  # noqa: S105  (a public prefix, not a secret)
MAX_ENDPOINTS = 16
LEASE = timedelta(minutes=5)  # an attempt that takes longer is presumed dead
SWEEP_GRACE = timedelta(minutes=1)  # how overdue a delivery must be before re-sending
RESPONSE_SNIPPET = 500  # bytes of the receiver's answer kept for the dashboard


@dataclass(frozen=True)
class AttemptResult:
    status_code: int | None
    error: str | None
    duration_ms: int
    response_body: str | None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


# ── endpoints ─────────────────────────────────────────────────────────────────


async def create_endpoint(
    merchant_id: str, url: str, event_types: list[str], description: str | None
) -> WebhookEndpoint:
    try:
        await net.check_webhook_url(url)
    except net.UnsafeUrl as exc:
        raise ValidationFailed("url", str(exc)) from exc
    async with SessionLocal.begin() as session:
        active = await session.scalar(
            select(func.count())
            .select_from(WebhookEndpoint)
            .where(WebhookEndpoint.merchant_id == merchant_id, WebhookEndpoint.deleted_at.is_(None))
        )
        if (active or 0) >= MAX_ENDPOINTS:
            raise Conflict("too_many_endpoints", f"At most {MAX_ENDPOINTS} webhook endpoints.")
        endpoint = WebhookEndpoint(
            id=new_id("we"),
            merchant_id=merchant_id,
            url=url,
            description=description,
            secret=SECRET_PREFIX + secrets.token_urlsafe(32),
            event_types=event_types,
        )
        session.add(endpoint)
    return endpoint


async def list_endpoints(merchant_id: str) -> list[WebhookEndpoint]:
    async with SessionLocal() as session:
        return list(
            await session.scalars(
                select(WebhookEndpoint)
                .where(
                    WebhookEndpoint.merchant_id == merchant_id,
                    WebhookEndpoint.deleted_at.is_(None),
                )
                .order_by(WebhookEndpoint.created_at)
            )
        )


async def delete_endpoint(merchant_id: str, endpoint_id: str) -> None:
    """Soft delete (the delivery log keeps pointing at it); pending deliveries stop."""
    async with SessionLocal.begin() as session:
        deleted = cast(
            CursorResult[Any],
            await session.execute(
                update(WebhookEndpoint)
                .where(
                    WebhookEndpoint.id == endpoint_id,
                    WebhookEndpoint.merchant_id == merchant_id,
                    WebhookEndpoint.deleted_at.is_(None),
                )
                .values(deleted_at=utcnow())
            ),
        )
        if deleted.rowcount == 0:
            raise NotFound("Webhook endpoint")
        await session.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.endpoint_id == endpoint_id,
                WebhookDelivery.status == DeliveryStatus.PENDING,
            )
            .values(status=DeliveryStatus.CANCELED, next_attempt_at=None)
        )


# ── delivering ────────────────────────────────────────────────────────────────


async def fan_out(event_id: str) -> list[str]:
    """Creates one delivery per endpoint that wants this event, and returns the ids of
    deliveries not attempted yet. Safe to run twice for the same event: the unique
    (event_id, endpoint_id) key makes the second insert a no-op."""
    now = utcnow()
    async with SessionLocal.begin() as session:
        event = await session.get(Event, event_id)
        if event is None:
            log.warning("fan_out_unknown_event", event_id=event_id)
            return []
        endpoints = [
            e
            for e in await session.scalars(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.merchant_id == event.merchant_id,
                    WebhookEndpoint.deleted_at.is_(None),
                )
            )
            if e.wants(event.type)
        ]
        if not endpoints:
            return []
        statement = insert(WebhookDelivery).values(
            [
                {
                    "id": new_id("wd"),
                    "merchant_id": event.merchant_id,
                    "endpoint_id": endpoint.id,
                    "event_id": event.id,
                    "status": DeliveryStatus.PENDING.value,
                    "attempts": 0,
                    "next_attempt_at": now,  # due now; the sweeper's safety net
                    "created_at": now,
                }
                for endpoint in endpoints
            ]
        )
        # an existing (event, endpoint) row is left as it is
        await session.execute(
            statement.on_duplicate_key_update(event_id=statement.inserted.event_id)
        )
        return list(
            await session.scalars(
                select(WebhookDelivery.id).where(
                    WebhookDelivery.event_id == event.id,
                    WebhookDelivery.status == DeliveryStatus.PENDING,
                    WebhookDelivery.attempts == 0,
                )
            )
        )


async def attempt_delivery(delivery_id: str, attempt: int) -> int | None:
    """Runs attempt number `attempt` of a delivery.

    Returns the number of seconds until the next attempt should run, or None when
    there's nothing more to do (delivered, gave up, or this message was stale).
    """
    now = utcnow()
    async with SessionLocal.begin() as session:
        # Claim the attempt: only the message for the NEXT attempt matches. The lease
        # lets the sweeper retry if this worker dies before recording the result.
        claimed = cast(
            CursorResult[Any],
            await session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == delivery_id,
                    WebhookDelivery.status == DeliveryStatus.PENDING,
                    WebhookDelivery.attempts == attempt - 1,
                )
                .values(attempts=attempt, last_attempt_at=now, next_attempt_at=now + LEASE)
            ),
        )
        if claimed.rowcount == 0:
            return None
        delivery = await session.get_one(WebhookDelivery, delivery_id)
        endpoint = await session.get_one(WebhookEndpoint, delivery.endpoint_id)
        event = await session.get_one(Event, delivery.event_id)

    result = await send(endpoint, event, delivery_id)

    give_up = not result.ok and attempt >= MAX_ATTEMPTS
    retry_in = None if result.ok or give_up else RETRY_DELAYS[attempt - 1]
    async with SessionLocal.begin() as session:
        session.add(
            WebhookAttempt(
                delivery_id=delivery_id,
                number=attempt,
                status_code=result.status_code,
                error=result.error,
                duration_ms=result.duration_ms,
                response_body=result.response_body,
            )
        )
        await session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id, WebhookDelivery.attempts == attempt)
            .values(
                status=DeliveryStatus.SUCCEEDED
                if result.ok
                else DeliveryStatus.FAILED
                if give_up
                else DeliveryStatus.PENDING,
                next_attempt_at=None
                if retry_in is None
                else utcnow() + timedelta(seconds=retry_in),
                last_status_code=result.status_code,
                last_error=result.error,
            )
        )
    log.info(
        "webhook_attempt",
        delivery_id=delivery_id,
        attempt=attempt,
        status_code=result.status_code,
        error=result.error,
        retry_in=retry_in,
    )
    return retry_in


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=settings.webhook_timeout_seconds,
            follow_redirects=False,  # a redirect could point at an internal address
            headers={"User-Agent": "PayForge-Webhooks/1.0"},
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def send(endpoint: WebhookEndpoint, event: Event, delivery_id: str) -> AttemptResult:
    """One signed POST. Never raises: every outcome becomes an AttemptResult."""
    try:
        await net.check_webhook_url(endpoint.url)  # again: DNS may have changed
    except net.UnsafeUrl as exc:
        return AttemptResult(None, f"blocked: {exc}", 0, None)

    body = EventOut.of(event).model_dump_json().encode()
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: header_value(endpoint.secret, body),
        "PayForge-Event-Id": event.id,
        "PayForge-Delivery-Id": delivery_id,
    }
    started = time.perf_counter()
    try:
        async with _http().stream("POST", endpoint.url, content=body, headers=headers) as res:
            snippet = b""
            async for chunk in res.aiter_raw():  # read only the start of the answer
                snippet += chunk
                if len(snippet) >= RESPONSE_SNIPPET:
                    break
        error = None if 200 <= res.status_code < 300 else f"HTTP {res.status_code}"
        return AttemptResult(
            res.status_code,
            error,
            _ms_since(started),
            snippet[:RESPONSE_SNIPPET].decode(errors="replace") or None,
        )
    except httpx.TimeoutException:
        return AttemptResult(None, "timeout", _ms_since(started), None)
    except httpx.HTTPError as exc:
        return AttemptResult(None, f"{type(exc).__name__}: {exc}"[:300], _ms_since(started), None)


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


async def sweep_overdue(limit: int = 100) -> list[tuple[str, int]]:
    """Deliveries whose next attempt is more than SWEEP_GRACE overdue: their message
    was lost, or a worker died mid-attempt. Returns (delivery_id, next attempt) pairs
    to publish again, and pushes their due time out by LEASE so they aren't swept
    again while that message is on its way."""
    now = utcnow()
    async with SessionLocal.begin() as session:
        rows = (
            await session.execute(
                select(WebhookDelivery.id, WebhookDelivery.attempts)
                .where(
                    WebhookDelivery.status == DeliveryStatus.PENDING,
                    WebhookDelivery.next_attempt_at < now - SWEEP_GRACE,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if rows:
            await session.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id.in_([r.id for r in rows]))
                .values(next_attempt_at=now + LEASE)
            )
    return [(r.id, r.attempts + 1) for r in rows]


# ── the delivery log (dashboard) ──────────────────────────────────────────────


async def list_deliveries(
    merchant_id: str, endpoint_id: str, limit: int = 50
) -> list[tuple[WebhookDelivery, str]]:
    """Newest first, with each delivery's event type."""
    async with SessionLocal() as session:
        endpoint = await session.scalar(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == endpoint_id, WebhookEndpoint.merchant_id == merchant_id
            )
        )
        if endpoint is None:
            raise NotFound("Webhook endpoint")
        rows = (
            await session.execute(
                select(WebhookDelivery, Event.type)
                .join(Event, Event.id == WebhookDelivery.event_id)
                .where(WebhookDelivery.endpoint_id == endpoint_id)
                .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
                .limit(limit)
            )
        ).all()
    return [(r[0], r[1]) for r in rows]


async def list_attempts(merchant_id: str, delivery_id: str) -> list[WebhookAttempt]:
    async with SessionLocal() as session:
        owner = await session.scalar(
            select(WebhookDelivery.merchant_id).where(WebhookDelivery.id == delivery_id)
        )
        if owner != merchant_id:
            raise NotFound("Webhook delivery")
        return list(
            await session.scalars(
                select(WebhookAttempt)
                .where(WebhookAttempt.delivery_id == delivery_id)
                .order_by(WebhookAttempt.number)
            )
        )


async def resend(merchant_id: str, delivery_id: str) -> None:
    """Sends a finished delivery once more (e.g. after the merchant fixed their server).

    The API never talks to RabbitMQ: this only marks the delivery as due, and the
    worker's sweeper sends it within seconds.
    """
    async with SessionLocal.begin() as session:
        changed = cast(
            CursorResult[Any],
            await session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == delivery_id,
                    WebhookDelivery.merchant_id == merchant_id,
                    WebhookDelivery.status.in_([DeliveryStatus.FAILED, DeliveryStatus.SUCCEEDED]),
                )
                .values(
                    status=DeliveryStatus.PENDING,
                    next_attempt_at=utcnow() - SWEEP_GRACE - timedelta(seconds=1),
                )
            ),
        )
    if changed.rowcount == 0:
        raise NotFound("Finished webhook delivery")
