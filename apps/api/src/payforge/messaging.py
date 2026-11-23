"""RabbitMQ: the exchanges and queues PayForge uses, declared in one place.

    payforge.events (topic, routing key = event type, e.g. "payment.succeeded")
        └─► webhooks.fanout        one message per event → which endpoints want it?
    payforge.webhooks (direct)
        └─► webhooks.deliver       one message per delivery attempt → POST it
    webhooks.retry.<delay>         a waiting room per delay: when a message's time is
                                   up, RabbitMQ dead-letters it back to webhooks.deliver
    webhooks.dead                  messages that kept crashing their consumer

Work queues are QUORUM queues: replicated, and with a delivery limit, so a message
that crashes its consumer 5 times goes to the dead queue instead of looping forever.
Every declaration is idempotent: each process declares everything at start-up.
"""

from dataclasses import dataclass

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection
from pamqp.common import FieldTable

from payforge.config import settings

EVENTS_EXCHANGE = "payforge.events"
WEBHOOKS_EXCHANGE = "payforge.webhooks"
FANOUT_QUEUE = "webhooks.fanout"
DELIVER_QUEUE = "webhooks.deliver"
DELIVER_KEY = "deliver"
DEAD_QUEUE = "webhooks.dead"

# After attempt n fails, wait RETRY_DELAYS[n - 1] seconds. 6 attempts over ~7 hours.
RETRY_DELAYS = (10, 60, 600, 3600, 6 * 3600)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1


def retry_queue(delay_seconds: int) -> str:
    return f"webhooks.retry.{delay_seconds}s"


def _work_queue_arguments() -> FieldTable:
    return {
        "x-queue-type": "quorum",
        "x-delivery-limit": 5,
        "x-dead-letter-exchange": "",  # the default exchange routes by queue name
        "x-dead-letter-routing-key": DEAD_QUEUE,
    }


@dataclass(frozen=True)
class Topology:
    events: AbstractExchange
    webhooks: AbstractExchange


async def connect() -> AbstractRobustConnection:
    """A connection that reconnects by itself (and re-declares what it declared)."""
    return await aio_pika.connect_robust(settings.rabbitmq_url, timeout=5)


async def declare(channel: AbstractChannel) -> Topology:
    events = await channel.declare_exchange(
        EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
    )
    webhooks = await channel.declare_exchange(
        WEBHOOKS_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )
    await channel.declare_queue(DEAD_QUEUE, durable=True, arguments={"x-queue-type": "quorum"})

    fanout = await channel.declare_queue(
        FANOUT_QUEUE, durable=True, arguments=_work_queue_arguments()
    )
    await fanout.bind(events, routing_key="#")  # every event type

    deliver = await channel.declare_queue(
        DELIVER_QUEUE, durable=True, arguments=_work_queue_arguments()
    )
    await deliver.bind(webhooks, routing_key=DELIVER_KEY)

    for delay in RETRY_DELAYS:
        # Each delay has its own queue: RabbitMQ only expires messages at the head of a
        # queue, so mixing delays in one queue would make a 6 h message block a 10 s one.
        await channel.declare_queue(
            retry_queue(delay),
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-message-ttl": delay * 1000,
                "x-dead-letter-exchange": WEBHOOKS_EXCHANGE,
                "x-dead-letter-routing-key": DELIVER_KEY,
            },
        )

    return Topology(events=events, webhooks=webhooks)
