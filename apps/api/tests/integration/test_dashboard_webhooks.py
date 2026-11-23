import httpx
import pytest
from sqlalchemy import update

from payforge.config import settings
from payforge.db import SessionLocal
from payforge.models import User, UserRole
from payforge.services import events, webhooks

from .conftest import MERCHANT_SITE
from .fake_merchant import FakeMerchant

SIGNUP = {
    "email": "kiran@example.com",
    "password": "a long enough password",
    "merchant_name": "Kiran's Books",
}
URL = MERCHANT_SITE + "/hooks/a"


async def logged_in(client: httpx.AsyncClient, signup: dict[str, str] = SIGNUP) -> dict[str, str]:
    await client.post("/auth/signup", json=signup)
    return {"X-CSRF-Token": (await client.get("/auth/me")).json()["csrf_token"]}


async def merchant_id(client: httpx.AsyncClient) -> str:
    return str((await client.get("/auth/me")).json()["merchant"]["id"])


async def test_create_shows_the_secret_once(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    res = await client.post(
        "/dashboard/webhook-endpoints",
        json={"url": URL, "event_types": ["payment.succeeded", "payment.succeeded"]},
        headers=headers,
    )

    assert res.status_code == 201
    assert res.json()["secret"].startswith("whsec_")
    assert res.json()["event_types"] == ["payment.succeeded"]
    listed = (await client.get("/dashboard/webhook-endpoints")).json()["data"]
    assert [e["id"] for e in listed] == [res.json()["id"]]
    assert "secret" not in listed[0]


async def test_internal_urls_are_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "allow_insecure_webhooks", False)
    headers = await logged_in(client)
    res = await client.post(
        "/dashboard/webhook-endpoints",
        json={"url": "https://169.254.169.254/latest/meta-data/"},
        headers=headers,
    )

    assert res.status_code == 422
    assert res.json()["error"]["details"][0]["field"] == "url"


async def test_unknown_event_type_is_422(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    res = await client.post(
        "/dashboard/webhook-endpoints",
        json={"url": URL, "event_types": ["payment.exploded"]},
        headers=headers,
    )
    assert res.status_code == 422


async def test_viewers_cannot_add_endpoints(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    async with SessionLocal.begin() as session:
        await session.execute(update(User).values(role=UserRole.VIEWER))

    res = await client.post("/dashboard/webhook-endpoints", json={"url": URL}, headers=headers)
    assert res.status_code == 403


async def test_delivery_log_attempts_and_resend(
    client: httpx.AsyncClient, merchant_site: FakeMerchant
) -> None:
    headers = await logged_in(client)
    endpoint_id = (
        await client.post("/dashboard/webhook-endpoints", json={"url": URL}, headers=headers)
    ).json()["id"]
    async with SessionLocal.begin() as session:
        event = await events.record(
            session,
            merchant_id=await merchant_id(client),
            type="payment.failed",
            resource_id="pay_1",
            data={},
        )
    merchant_site.answer("/hooks/a", 500)
    [delivery_id] = await webhooks.fan_out(event.id)
    await webhooks.attempt_delivery(delivery_id, 1)

    [delivery] = (
        await client.get(f"/dashboard/webhook-endpoints/{endpoint_id}/deliveries")
    ).json()["data"]
    assert delivery["event_type"] == "payment.failed"
    assert (delivery["status"], delivery["attempts"], delivery["last_status_code"]) == (
        "pending",
        1,
        500,
    )
    [attempt] = (await client.get(f"/dashboard/webhook-deliveries/{delivery_id}/attempts")).json()[
        "data"
    ]
    assert attempt["response_body"] == "answer 500"

    pending = await client.post(
        f"/dashboard/webhook-deliveries/{delivery_id}/resend", headers=headers
    )
    assert pending.status_code == 404  # only finished deliveries can be resent


async def test_another_merchants_endpoint_is_404(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    endpoint_id = (
        await client.post("/dashboard/webhook-endpoints", json={"url": URL}, headers=headers)
    ).json()["id"]
    await client.post("/auth/logout", headers=headers)
    other = await logged_in(client, {**SIGNUP, "email": "other@example.com"})

    assert (
        await client.get(f"/dashboard/webhook-endpoints/{endpoint_id}/deliveries")
    ).status_code == 404
    assert (
        await client.delete(f"/dashboard/webhook-endpoints/{endpoint_id}", headers=other)
    ).status_code == 404
