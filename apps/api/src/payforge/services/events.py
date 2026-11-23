"""Events: "this happened" records, e.g. payment.succeeded.

An event is written in the SAME database transaction as the change it describes
(the transactional outbox). So there is never a succeeded payment without its event,
nor an event for a change that was rolled back. A relay publishes new events to
RabbitMQ afterwards; publishing can repeat, so consumers must dedupe by event id.

`data` is a snapshot of the resource in its v1 API shape at the moment it happened,
the same JSON a GET returned then.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from payforge.db import SessionLocal
from payforge.domain.cursor import Cursor, encode_cursor
from payforge.errors import NotFound
from payforge.ids import new_id
from payforge.models import Event

EVENT_TYPES = ("payment.succeeded", "payment.failed", "refund.succeeded", "refund.failed")


@dataclass(frozen=True)
class EventPage:
    data: list[Event]
    has_more: bool
    next_cursor: str | None


async def record(
    session: AsyncSession, *, merchant_id: str, type: str, resource_id: str, data: dict[str, Any]
) -> Event:
    """Adds an event to the caller's transaction."""
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type}")
    event = Event(
        id=new_id("evt"), merchant_id=merchant_id, type=type, resource_id=resource_id, data=data
    )
    session.add(event)
    return event


async def get_event(merchant_id: str, event_id: str) -> Event:
    async with SessionLocal() as session:
        event = await session.scalar(
            select(Event).where(Event.id == event_id, Event.merchant_id == merchant_id)
        )
    if event is None:
        raise NotFound("Event")
    return event


async def list_events(
    merchant_id: str, *, limit: int, cursor: Cursor | None = None, type: str | None = None
) -> EventPage:
    conditions = [Event.merchant_id == merchant_id]
    if type is not None:
        conditions.append(Event.type == type)
    if cursor is not None:
        conditions.append(
            or_(
                Event.created_at < cursor.created_at,
                and_(Event.created_at == cursor.created_at, Event.id < cursor.id),
            )
        )
    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(Event)
                .where(*conditions)
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(limit + 1)
            )
        )
    has_more = len(rows) > limit
    data = rows[:limit]
    next_cursor = (
        encode_cursor(Cursor(created_at=data[-1].created_at, id=data[-1].id))
        if has_more and data
        else None
    )
    return EventPage(data=data, has_more=has_more, next_cursor=next_cursor)
