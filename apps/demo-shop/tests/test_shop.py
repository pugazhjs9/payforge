import hashlib
import hmac
import json
import time

import httpx

from demo_shop.app import create_app
from demo_shop.payforge_client import PayForge, verify_webhook

SECRET = "whsec_demo"


def fake_payforge(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        return httpx.Response(202, json={"id": "pay_1", "status": "processing"})

    return httpx.MockTransport(handle)


def shop(seen: list[httpx.Request]) -> httpx.AsyncClient:
    payforge = PayForge(
        "http://payforge",
        "key_1",
        "pf_sk_test_x",
        http=httpx.AsyncClient(transport=fake_payforge(seen)),
    )
    app = create_app(payforge, SECRET)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shop")


def signed(event: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(event).encode()
    t = int(time.time())
    v1 = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"PayForge-Signature": f"t={t},v1={v1}"}


def payment_event(event_id: str, status: str) -> dict[str, object]:
    return {
        "id": event_id,
        "type": f"payment.{status}",
        "data": {"object": {"id": "pay_1", "status": status, "metadata": {"order_id": "1001"}}},
    }


async def test_an_order_pays_with_the_order_id_as_idempotency_key() -> None:
    seen: list[httpx.Request] = []
    async with shop(seen) as client:
        await client.post("/orders", content=b"method=upi%3Akiran%40okbank")

    payment = seen[-1]
    assert payment.headers["idempotency-key"] == "order-1001"
    assert payment.headers["authorization"] == "Bearer tok"
    assert json.loads(payment.content)["payment_method"] == "upi:kiran@okbank"


async def test_a_signed_webhook_updates_the_order_and_duplicates_are_ignored() -> None:
    async with shop([]) as client:
        await client.post("/orders", content=b"method=tok_card_ok")
        body, headers = signed(payment_event("evt_1", "succeeded"))
        first = await client.post("/webhooks/payforge", content=body, headers=headers)
        again = await client.post("/webhooks/payforge", content=body, headers=headers)
        page = (await client.get("/")).text

    assert first.status_code == again.status_code == 200
    assert "<td>succeeded</td>" in page


async def test_a_late_event_never_moves_a_finished_order_back() -> None:
    async with shop([]) as client:
        await client.post("/orders", content=b"method=tok_card_ok")
        for event in (payment_event("evt_2", "succeeded"), payment_event("evt_1", "failed")):
            body, headers = signed(event)
            await client.post("/webhooks/payforge", content=body, headers=headers)
        page = (await client.get("/")).text

    assert "<td>succeeded</td>" in page


async def test_forged_webhooks_are_rejected() -> None:
    async with shop([]) as client:
        body, headers = signed(payment_event("evt_1", "succeeded"))
        res = await client.post("/webhooks/payforge", content=body + b" ", headers=headers)
    assert res.status_code == 400


def test_old_signatures_fail_verification() -> None:
    body = b"{}"
    assert not verify_webhook(SECRET, "t=1000,v1=abc", body)
    assert not verify_webhook(SECRET, "garbage", body)
