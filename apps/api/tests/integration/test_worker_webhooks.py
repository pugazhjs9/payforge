"""Webhooks end to end: API → outbox → relay → RabbitMQ → consumers → merchant's server."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from payforge import messaging, webhook_signing
from payforge.messaging import Topology
from payforge.models import DeliveryStatus
from payforge.services import webhooks
from payforge.worker import webhooks as consumers
from payforge.worker.relay import relay_once

from .conftest import MERCHANT_A, MERCHANT_SITE, merchant, pay, with_key
from .fake_merchant import FakeMerchant

A = merchant(MERCHANT_A)
pytestmark = pytest.mark.usefixtures("bank")


async def eventually(check: Callable[[], Awaitable[bool]], within: float = 8) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def take(channel: AbstractChannel, queue: str, within: float = 8) -> AbstractIncomingMessage:
    """The next message in `queue`, waiting for it. (Polls basic.get: message COUNTS of
    quorum queues, and the management API's stats, lag behind and can't be used here.)"""
    source = await channel.get_queue(queue)
    deadline = asyncio.get_running_loop().time() + within
    while (message := await source.get(no_ack=True, fail=False)) is None:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"nothing arrived in {queue}")
        await asyncio.sleep(0.05)
    return message


@pytest.fixture
async def worker(channel: AbstractChannel) -> AsyncIterator[Topology]:
    """The worker's consumers, running in this test's event loop."""
    topology = await messaging.declare(channel)
    await consumers.start(channel, topology)
    yield topology


async def test_a_payment_reaches_the_merchants_server_signed(
    client: httpx.AsyncClient, worker: Topology, merchant_site: FakeMerchant
) -> None:
    hook = await webhooks.create_endpoint(MERCHANT_A, MERCHANT_SITE + "/hooks/a", ["*"], None)
    payment = await client.post("/v1/payments", json=pay("100.00"), headers=with_key(A))

    assert await relay_once(worker.events) == 1

    async def arrived() -> bool:
        return len(merchant_site.to("/hooks/a")) == 1

    await eventually(arrived)
    [request] = merchant_site.to("/hooks/a")
    body = json.loads(request.body)
    assert body["type"] == "payment.succeeded"
    assert body["data"]["object"]["id"] == payment.json()["id"]
    assert webhook_signing.verify(hook.secret, request.headers["payforge-signature"], request.body)

    async def recorded() -> bool:
        [(delivery, _type)] = await webhooks.list_deliveries(MERCHANT_A, hook.id)
        return delivery.status == DeliveryStatus.SUCCEEDED

    await eventually(recorded)


async def test_a_failed_attempt_waits_in_the_retry_queue(
    client: httpx.AsyncClient,
    worker: Topology,
    channel: AbstractChannel,
    merchant_site: FakeMerchant,
) -> None:
    await webhooks.create_endpoint(MERCHANT_A, MERCHANT_SITE + "/hooks/a", ["*"], None)
    merchant_site.answer("/hooks/a", 500)
    await client.post("/v1/payments", json=pay("100.00"), headers=with_key(A))
    await relay_once(worker.events)

    message = await take(channel, messaging.retry_queue(messaging.RETRY_DELAYS[0]))
    assert json.loads(message.body)["attempt"] == 2


async def test_retry_queues_dead_letter_back_to_delivery(
    client: httpx.AsyncClient,
    worker: Topology,
    channel: AbstractChannel,
    merchant_site: FakeMerchant,
) -> None:
    """Same wiring as the real retry queues, with a 0.2 s wait instead of 10 s."""
    await webhooks.create_endpoint(MERCHANT_A, MERCHANT_SITE + "/hooks/a", ["*"], None)
    merchant_site.answer("/hooks/a", 500)  # attempt 1 fails, attempt 2 gets the default 200
    await client.post("/v1/payments", json=pay("100.00"), headers=with_key(A))
    await relay_once(worker.events)

    parked = await take(channel, messaging.retry_queue(10))
    fast_retry = await channel.declare_queue(
        "webhooks.retry.test",
        exclusive=True,  # RabbitMQ 4 refuses transient non-exclusive queues
        arguments={
            "x-message-ttl": 200,
            "x-dead-letter-exchange": messaging.WEBHOOKS_EXCHANGE,
            "x-dead-letter-routing-key": messaging.DELIVER_KEY,
        },
    )
    await channel.default_exchange.publish(
        aio_pika.Message(parked.body), routing_key=fast_retry.name
    )

    async def delivered_twice() -> bool:
        return len(merchant_site.to("/hooks/a")) == 2

    await eventually(delivered_twice)


async def test_a_message_that_keeps_crashing_ends_in_the_dead_letter_queue(
    worker: Topology, channel: AbstractChannel
) -> None:
    await worker.webhooks.publish(
        aio_pika.Message(b"this is not json"), routing_key=messaging.DELIVER_KEY
    )

    dead = await take(channel, messaging.DEAD_QUEUE, within=20)
    assert dead.body == b"this is not json"
