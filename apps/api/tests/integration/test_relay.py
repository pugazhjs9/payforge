import asyncio
import json

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue
from sqlalchemy import select

from payforge import messaging
from payforge.db import SessionLocal
from payforge.models import Event
from payforge.services import events
from payforge.worker.relay import relay_once

from .conftest import MERCHANT_A


async def add_events(n: int) -> list[str]:
    async with SessionLocal.begin() as session:
        added = [
            await events.record(
                session,
                merchant_id=MERCHANT_A,
                type="payment.succeeded",
                resource_id=f"pay_{i}",
                data={"id": f"pay_{i}"},
            )
            for i in range(n)
        ]
    return [e.id for e in added]


async def drain(queue: AbstractQueue) -> list[aio_pika.abc.AbstractIncomingMessage]:
    messages = []
    while (message := await queue.get(no_ack=True, fail=False)) is not None:
        messages.append(message)
    return messages


async def unpublished() -> int:
    async with SessionLocal() as session:
        return len(list(await session.scalars(select(Event).where(Event.published_at.is_(None)))))


async def test_relay_publishes_new_events_and_marks_them(channel: AbstractChannel) -> None:
    [event_id] = await add_events(1)
    exchange = await channel.get_exchange(messaging.EVENTS_EXCHANGE)

    assert await relay_once(exchange) == 1
    assert await relay_once(exchange) == 0  # nothing left
    assert await unpublished() == 0

    [message] = await drain(await channel.get_queue(messaging.FANOUT_QUEUE))
    assert message.message_id == event_id
    assert message.routing_key == "payment.succeeded"
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    body = json.loads(message.body)
    assert body["id"] == event_id
    assert body["data"]["object"] == {"id": "pay_0"}


async def test_parallel_relays_publish_each_event_once(channel: AbstractChannel) -> None:
    """FOR UPDATE SKIP LOCKED: relays running side by side take different rows."""
    ids = await add_events(30)
    exchange = await channel.get_exchange(messaging.EVENTS_EXCHANGE)

    counts = await asyncio.gather(*(relay_once(exchange, batch_size=10) for _ in range(5)))

    assert sum(counts) == 30
    messages = await drain(await channel.get_queue(messaging.FANOUT_QUEUE))
    assert sorted(m.message_id or "" for m in messages) == sorted(ids)


class BrokerDown:
    async def publish(self, *_args: object, **_kwargs: object) -> None:
        raise ConnectionError("broker unreachable")


async def test_events_stay_in_the_outbox_when_the_broker_is_down() -> None:
    await add_events(3)
    broken: AbstractExchange = BrokerDown()  # type: ignore[assignment]

    with pytest.raises(ConnectionError):
        await relay_once(broken)
    assert await unpublished() == 3  # nothing lost: the next round sends them
