"""Webhook delivery (services/webhooks.py) against real MySQL and a real HTTP receiver."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from payforge import webhook_signing
from payforge.config import settings
from payforge.db import SessionLocal
from payforge.errors import AppError
from payforge.messaging import MAX_ATTEMPTS, RETRY_DELAYS
from payforge.models import (
    DeliveryStatus,
    WebhookAttempt,
    WebhookDelivery,
    WebhookEndpoint,
    utcnow,
)
from payforge.services import events, webhooks

from .conftest import MERCHANT_A, MERCHANT_SITE
from .fake_merchant import FakeMerchant

pytestmark = pytest.mark.usefixtures("merchant_site")


async def new_event(type: str = "payment.succeeded", merchant_id: str = MERCHANT_A) -> str:
    async with SessionLocal.begin() as session:
        event = await events.record(
            session, merchant_id=merchant_id, type=type, resource_id="pay_1", data={"id": "pay_1"}
        )
    return event.id


async def endpoint(path: str = "/hooks/a", types: list[str] | None = None) -> WebhookEndpoint:
    return await webhooks.create_endpoint(MERCHANT_A, MERCHANT_SITE + path, types or ["*"], None)


async def delivery(delivery_id: str) -> WebhookDelivery:
    async with SessionLocal() as session:
        return await session.get_one(WebhookDelivery, delivery_id)


async def attempts(delivery_id: str) -> list[WebhookAttempt]:
    async with SessionLocal() as session:
        return list(
            await session.scalars(
                select(WebhookAttempt).where(WebhookAttempt.delivery_id == delivery_id)
            )
        )


# ── endpoints ─────────────────────────────────────────────────────────────────


async def test_new_endpoint_gets_a_secret() -> None:
    created = await endpoint()
    assert created.secret.startswith("whsec_")
    assert len(created.secret) > 40


async def test_unsafe_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_insecure_webhooks", False)
    with pytest.raises(AppError) as error:
        await webhooks.create_endpoint(MERCHANT_A, "http://127.0.0.1:4996/hooks/a", ["*"], None)
    assert error.value.status == 422


# ── fan-out ───────────────────────────────────────────────────────────────────


async def test_fan_out_only_to_interested_endpoints_and_only_once() -> None:
    everything = await endpoint("/hooks/all")
    await endpoint("/hooks/refunds", ["refund.succeeded"])
    event_id = await new_event("payment.succeeded")

    first = await webhooks.fan_out(event_id)
    again = await webhooks.fan_out(event_id)  # e.g. RabbitMQ redelivered the event

    assert len(first) == 1
    assert again == first
    assert (await delivery(first[0])).endpoint_id == everything.id


async def test_no_endpoints_no_deliveries() -> None:
    assert await webhooks.fan_out(await new_event()) == []


# ── attempts ──────────────────────────────────────────────────────────────────


async def test_successful_delivery_is_signed_and_logged(merchant_site: FakeMerchant) -> None:
    hook = await endpoint()
    event_id = await new_event()
    [delivery_id] = await webhooks.fan_out(event_id)

    assert await webhooks.attempt_delivery(delivery_id, 1) is None

    [request] = merchant_site.to("/hooks/a")
    assert json.loads(request.body)["id"] == event_id
    assert request.headers["payforge-event-id"] == event_id
    assert webhook_signing.verify(hook.secret, request.headers["payforge-signature"], request.body)
    done = await delivery(delivery_id)
    assert done.status == DeliveryStatus.SUCCEEDED
    assert done.next_attempt_at is None
    [logged] = await attempts(delivery_id)
    assert (logged.number, logged.status_code, logged.response_body) == (1, 200, "answer 200")


async def test_failure_schedules_the_next_attempt(merchant_site: FakeMerchant) -> None:
    await endpoint()
    merchant_site.answer("/hooks/a", 500)
    [delivery_id] = await webhooks.fan_out(await new_event())

    retry_in = await webhooks.attempt_delivery(delivery_id, 1)

    assert retry_in == RETRY_DELAYS[0]
    pending = await delivery(delivery_id)
    assert pending.status == DeliveryStatus.PENDING
    assert pending.attempts == 1
    assert pending.last_error == "HTTP 500"
    assert pending.next_attempt_at is not None
    assert pending.next_attempt_at > utcnow() + timedelta(seconds=RETRY_DELAYS[0] - 2)


async def test_a_duplicate_or_stale_message_does_nothing(merchant_site: FakeMerchant) -> None:
    await endpoint()
    [delivery_id] = await webhooks.fan_out(await new_event())

    await webhooks.attempt_delivery(delivery_id, 1)
    assert await webhooks.attempt_delivery(delivery_id, 1) is None  # same message again
    assert await webhooks.attempt_delivery(delivery_id, 5) is None  # out of turn

    assert len(merchant_site.to("/hooks/a")) == 1


async def test_gives_up_after_the_last_attempt(merchant_site: FakeMerchant) -> None:
    await endpoint()
    merchant_site.answer("/hooks/a", 503)
    [delivery_id] = await webhooks.fan_out(await new_event())
    async with SessionLocal.begin() as session:
        await session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id)
            .values(attempts=MAX_ATTEMPTS - 1)
        )

    assert await webhooks.attempt_delivery(delivery_id, MAX_ATTEMPTS) is None
    assert (await delivery(delivery_id)).status == DeliveryStatus.FAILED


async def test_slow_receiver_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    await webhooks.close()  # the next client picks up the shorter timeout
    monkeypatch.setattr(settings, "webhook_timeout_seconds", 0.3)
    await endpoint("/hooks/slow")
    [delivery_id] = await webhooks.fan_out(await new_event())

    assert await webhooks.attempt_delivery(delivery_id, 1) == RETRY_DELAYS[0]
    assert (await delivery(delivery_id)).last_error == "timeout"
    await webhooks.close()


async def test_redirects_are_not_followed(merchant_site: FakeMerchant) -> None:
    await endpoint("/hooks/redirect")
    [delivery_id] = await webhooks.fan_out(await new_event())

    await webhooks.attempt_delivery(delivery_id, 1)

    assert (await delivery(delivery_id)).last_status_code == 302  # a failure, not followed
    assert len(merchant_site.received) == 1


async def test_url_is_checked_again_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    await endpoint()
    [delivery_id] = await webhooks.fan_out(await new_event())
    monkeypatch.setattr(settings, "allow_insecure_webhooks", False)  # as if DNS changed

    await webhooks.attempt_delivery(delivery_id, 1)

    assert (await delivery(delivery_id)).last_error == "blocked: must use https://"


# ── sweeper, delete, resend ───────────────────────────────────────────────────


async def test_sweeper_resends_overdue_deliveries_once() -> None:
    await endpoint()
    [delivery_id] = await webhooks.fan_out(await new_event())
    async with SessionLocal.begin() as session:  # its message was lost 10 minutes ago
        await session.execute(
            update(WebhookDelivery).values(next_attempt_at=utcnow() - timedelta(minutes=10))
        )

    assert await webhooks.sweep_overdue() == [(delivery_id, 1)]
    assert await webhooks.sweep_overdue() == []  # not again while that message travels


async def test_deleting_an_endpoint_cancels_its_pending_deliveries() -> None:
    hook = await endpoint()
    [delivery_id] = await webhooks.fan_out(await new_event())

    await webhooks.delete_endpoint(MERCHANT_A, hook.id)

    assert (await delivery(delivery_id)).status == DeliveryStatus.CANCELED
    assert await webhooks.list_endpoints(MERCHANT_A) == []
    assert await webhooks.attempt_delivery(delivery_id, 1) is None


async def test_resend_makes_a_finished_delivery_due_for_the_sweeper(
    merchant_site: FakeMerchant,
) -> None:
    await endpoint()
    [delivery_id] = await webhooks.fan_out(await new_event())
    await webhooks.attempt_delivery(delivery_id, 1)

    await webhooks.resend(MERCHANT_A, delivery_id)

    assert await webhooks.sweep_overdue() == [(delivery_id, 2)]
    await webhooks.attempt_delivery(delivery_id, 2)
    assert len(merchant_site.to("/hooks/a")) == 2
