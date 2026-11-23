from typing import Annotated, Literal

from fastapi import APIRouter, Query

from payforge.api.deps import PaymentsRead
from payforge.api.v1.schemas import EventList, EventOut
from payforge.domain.cursor import decode_cursor
from payforge.services import events

router = APIRouter(tags=["Events"])

EventType = Literal["payment.succeeded", "payment.failed", "refund.succeeded", "refund.failed"]


@router.get("/events")
async def list_events(
    merchant: PaymentsRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    type: EventType | None = None,
) -> EventList:
    """Everything that happened, newest first. The same events are sent as webhooks, so
    this is also how a merchant catches up on webhooks it missed."""
    page = await events.list_events(
        merchant.id, limit=limit, cursor=decode_cursor(cursor) if cursor else None, type=type
    )
    return EventList(
        data=[EventOut.of(e) for e in page.data],
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


@router.get("/events/{event_id}")
async def get_event(event_id: str, merchant: PaymentsRead) -> EventOut:
    return EventOut.of(await events.get_event(merchant.id, event_id))
