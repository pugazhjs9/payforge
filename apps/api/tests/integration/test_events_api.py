import httpx
import pytest
from sqlalchemy import select

from payforge.db import SessionLocal
from payforge.models import Event

from .conftest import MERCHANT_A, MERCHANT_B, merchant, pay, with_key

A = merchant(MERCHANT_A)
pytestmark = pytest.mark.usefixtures("bank")


async def all_events() -> list[Event]:
    async with SessionLocal() as session:
        return list(await session.scalars(select(Event).order_by(Event.created_at, Event.id)))


async def create(client: httpx.AsyncClient, token: str = "tok_card_ok") -> httpx.Response:
    return await client.post("/v1/payments", json=pay("100.00", token), headers=with_key(A))


async def test_list_newest_first_with_type_filter_and_pagination(
    client: httpx.AsyncClient,
) -> None:
    for token in ("tok_card_ok", "tok_card_declined", "tok_card_ok"):
        await create(client, token)

    everything = (await client.get("/v1/events", headers=A)).json()
    assert [e["type"] for e in everything["data"]] == [
        "payment.succeeded",
        "payment.failed",
        "payment.succeeded",
    ]
    assert everything["data"][0]["object"] == "event"
    assert everything["data"][0]["data"]["object"]["object"] == "payment"

    failed = await client.get("/v1/events", params={"type": "payment.failed"}, headers=A)
    assert len(failed.json()["data"]) == 1

    first = (await client.get("/v1/events", params={"limit": 2}, headers=A)).json()
    rest = await client.get(
        "/v1/events", params={"limit": 2, "cursor": first["next_cursor"]}, headers=A
    )
    assert len(first["data"]) == 2
    assert first["has_more"] is True
    assert len(rest.json()["data"]) == 1


async def test_other_merchants_events_are_invisible(client: httpx.AsyncClient) -> None:
    await create(client)
    [event] = await all_events()
    b = merchant(MERCHANT_B)

    assert (await client.get("/v1/events", headers=b)).json()["data"] == []
    assert (await client.get(f"/v1/events/{event.id}", headers=b)).status_code == 404
    own = await client.get(f"/v1/events/{event.id}", headers=A)
    assert own.json()["id"] == event.id


async def test_unknown_type_filter_is_422(client: httpx.AsyncClient) -> None:
    res = await client.get("/v1/events", params={"type": "payment.exploded"}, headers=A)
    assert res.status_code == 422
