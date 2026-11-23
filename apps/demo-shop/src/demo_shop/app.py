"""Kiran's Books: a demo shop that sells one book through PayForge.

Run (after creating an API key and a webhook endpoint in the PayForge dashboard):
    PAYFORGE_KEY_ID=... PAYFORGE_KEY_SECRET=... PAYFORGE_WEBHOOK_SECRET=... \\
        uv run uvicorn demo_shop.app:app --port 5050

How it treats webhooks, as every merchant should:
- verify the signature over the raw body before trusting anything;
- dedupe by event id (PayForge delivers at least once);
- don't rely on order: a "succeeded" order never goes back to "processing";
- answer 2xx quickly.
"""

import html
import itertools
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from demo_shop.payforge_client import PayForge, verify_webhook

FINAL = {"succeeded", "failed"}


@dataclass
class Order:
    id: str
    method: str
    status: str = "new"
    payment_id: str | None = None


@dataclass
class Shop:
    orders: dict[str, Order] = field(default_factory=dict)
    seen_events: set[str] = field(default_factory=set)
    numbers: Iterator[int] = field(default_factory=lambda: itertools.count(1001))


def create_app(payforge: PayForge, webhook_secret: str) -> FastAPI:
    app = FastAPI(title="Kiran's Books (demo shop)")
    shop = Shop()
    app.state.shop = shop

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        rows = "".join(
            f"<tr><td>{o.id}</td><td>{html.escape(o.method)}</td><td>{o.status}</td>"
            f"<td><code>{o.payment_id or ''}</code></td></tr>"
            for o in reversed(shop.orders.values())
        )
        return f"""<!doctype html><meta charset="utf-8"><title>Kiran's Books</title>
<h1>Kiran's Books</h1><p>"Designing Data-Intensive Applications" · ₹499.00</p>
<form method="post" action="/orders"><button name="method" value="tok_card_ok">Pay by card</button>
<button name="method" value="upi:kiran@okbank">Pay by UPI</button>
<button name="method" value="tok_card_declined">Pay with a card that gets declined</button></form>
<h2>Orders</h2><table border="1" cellpadding="6"><tr><th>Order</th><th>Method</th><th>Status</th>
<th>Payment</th></tr>{rows}</table><p><a href="/">Refresh</a></p>"""

    @app.post("/orders")
    async def place_order(request: Request) -> RedirectResponse:
        method = parse_qs((await request.body()).decode()).get("method", [""])[0]
        order = Order(id=str(next(shop.numbers)), method=method)
        shop.orders[order.id] = order
        payment = await payforge.create_payment(
            order_id=order.id, amount="499.00", payment_method=method
        )
        order.payment_id = str(payment["id"])
        order.status = str(payment["status"])
        return RedirectResponse("/", status_code=303)

    @app.post("/webhooks/payforge")
    async def webhook(request: Request) -> Response:
        body = await request.body()
        if not verify_webhook(webhook_secret, request.headers.get("payforge-signature", ""), body):
            raise HTTPException(400, "bad signature")
        event = json.loads(body)
        if event["id"] in shop.seen_events:
            return Response(status_code=200)  # a duplicate: already handled
        shop.seen_events.add(event["id"])
        payment = event["data"]["object"]
        order = shop.orders.get(payment.get("metadata", {}).get("order_id", ""))
        if order is not None and order.status not in FINAL:
            order.status = payment["status"]
        return Response(status_code=200)

    return app


def _from_env() -> FastAPI:
    return create_app(
        PayForge(
            base_url=os.environ.get("PAYFORGE_URL", "http://127.0.0.1:3000"),
            key_id=os.environ.get("PAYFORGE_KEY_ID", ""),
            key_secret=os.environ.get("PAYFORGE_KEY_SECRET", ""),
        ),
        webhook_secret=os.environ.get("PAYFORGE_WEBHOOK_SECRET", ""),
    )


app = _from_env()
