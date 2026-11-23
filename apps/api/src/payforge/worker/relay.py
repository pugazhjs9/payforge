"""The outbox relay: moves events from MySQL to RabbitMQ.

Each round locks a batch of unpublished events with FOR UPDATE SKIP LOCKED (so several
relays never take the same rows), publishes them, waits for RabbitMQ to confirm each
one, and only then marks them published, in the same transaction.

If the process dies after publishing but before the commit, those events are
published again next round: delivery is AT LEAST ONCE, never "at most once".
Consumers dedupe by event id.
"""

import asyncio
import contextlib

import aio_pika
from aio_pika.abc import AbstractExchange
from sqlalchemy import select, update

from payforge.api.v1.schemas import EventOut
from payforge.db import SessionLocal
from payforge.logging import log
from payforge.models import Event, utcnow

BATCH_SIZE = 100


def event_message(event: Event) -> aio_pika.Message:
    return aio_pika.Message(
        EventOut.of(event).model_dump_json().encode(),
        message_id=event.id,
        type=event.type,
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,  # survives a broker restart
        headers={"merchant_id": event.merchant_id},
        timestamp=event.created_at,
    )


async def relay_once(exchange: AbstractExchange, batch_size: int = BATCH_SIZE) -> int:
    """Publishes one batch. Returns how many events were published."""
    async with SessionLocal.begin() as session:
        # Lock IDs only, read in order from the (published_at, created_at, id) index.
        # Selecting whole rows here let MySQL pick a full scan + sort on small tables,
        # and FOR UPDATE then locks EVERY row it read, so other relays got nothing.
        ids = list(
            await session.scalars(
                select(Event.id)
                .where(Event.published_at.is_(None))
                .order_by(Event.created_at, Event.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        if not ids:
            return 0
        batch = list(
            await session.scalars(
                select(Event).where(Event.id.in_(ids)).order_by(Event.created_at, Event.id)
            )
        )
        # all at once: each publish returns when the broker has confirmed it
        await asyncio.gather(
            *(exchange.publish(event_message(e), routing_key=e.type) for e in batch)
        )
        await session.execute(
            update(Event)
            .where(Event.id.in_(ids))
            .values(published_at=utcnow())
            .execution_options(synchronize_session=False)
        )
    return len(batch)


async def run_relay(exchange: AbstractExchange, stop: asyncio.Event, idle: float = 0.5) -> None:
    """Relays until `stop` is set. A full batch means more may be waiting: go again now."""
    while not stop.is_set():
        try:
            published = await relay_once(exchange)
        except Exception:
            log.exception("relay_failed")  # e.g. broker down: the events wait in MySQL
            published = 0
        if published < BATCH_SIZE:
            await sleep_unless_stopped(stop, idle)


async def sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    """Like asyncio.sleep, but returns at once when the worker is shutting down."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
