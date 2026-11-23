"""RabbitMQ consumers for webhooks.

webhooks.fanout   an event arrived → create its deliveries → publish attempt 1 of each
webhooks.deliver  attempt n is due → POST it → if it failed, publish attempt n+1 to the
                  retry queue for the right delay (RabbitMQ sends it back here later)

A message is acknowledged only after its work is done (and its follow-up message is
published). If the worker dies in between, RabbitMQ redelivers it, and the attempt
claim in services/webhooks.py turns the duplicate into a no-op.
"""

import json
from functools import partial

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from payforge import messaging
from payforge.messaging import Topology
from payforge.services import webhooks

PREFETCH = 20  # messages in flight per worker: bounds concurrent outgoing requests


def deliver_message(delivery_id: str, attempt: int) -> aio_pika.Message:
    return aio_pika.Message(
        json.dumps({"delivery_id": delivery_id, "attempt": attempt}).encode(),
        content_type="application/json",
        message_id=f"{delivery_id}:{attempt}",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )


async def publish_attempt(topology: Topology, delivery_id: str, attempt: int) -> None:
    await topology.webhooks.publish(
        deliver_message(delivery_id, attempt), routing_key=messaging.DELIVER_KEY
    )


async def on_fanout(topology: Topology, message: AbstractIncomingMessage) -> None:
    # requeue=True: a crash (e.g. MySQL briefly down) retries the message; the queue's
    # delivery limit sends it to the dead-letter queue if it keeps failing
    async with message.process(requeue=True):
        if message.message_id is None:
            return
        for delivery_id in await webhooks.fan_out(message.message_id):
            await publish_attempt(topology, delivery_id, 1)


async def on_deliver(channel: AbstractChannel, message: AbstractIncomingMessage) -> None:
    async with message.process(requeue=True):
        task = json.loads(message.body)
        delivery_id, attempt = str(task["delivery_id"]), int(task["attempt"])
        retry_in = await webhooks.attempt_delivery(delivery_id, attempt)
        if retry_in is not None:
            # into the waiting room; when its TTL ends it's dead-lettered back to deliver
            await channel.default_exchange.publish(
                deliver_message(delivery_id, attempt + 1),
                routing_key=messaging.retry_queue(retry_in),
            )


async def start(channel: AbstractChannel, topology: Topology) -> list[str]:
    """Starts consuming; returns the consumer tags (to cancel them on shutdown)."""
    await channel.set_qos(prefetch_count=PREFETCH)
    fanout = await channel.get_queue(messaging.FANOUT_QUEUE)
    deliver = await channel.get_queue(messaging.DELIVER_QUEUE)
    return [
        await fanout.consume(partial(on_fanout, topology)),
        await deliver.consume(partial(on_deliver, channel)),
    ]
