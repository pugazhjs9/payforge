"""SimBank pretends to be a bank / card network.

Two features real banks have, and PayForge relies on:
  1. Charges are stored by the caller's own reference (PayForge's payment ID).
     Sending the same reference again returns the first charge, never a second one.
  2. GET /charges/{reference} answers "what happened to this one?" after a timeout.

Test tokens (cards answer synchronously):
  tok_card_ok          approved
  tok_card_declined    declined: card_declined
  tok_card_no_funds    declined: insufficient_funds
  tok_card_slow        approved, but the answer arrives after `slow_seconds`
  tok_card_no_refunds  approved; refunds on it fail with refund_not_allowed

UPI (asynchronous, like the real thing): "upi:<vpa>", e.g. upi:kiran@okbank.
  The charge answers `pending` at once (the customer must approve it in their UPI
  app). After `upi_seconds` it is settled and SimBank sends a signed webhook to
  `webhook_url`, retrying if it doesn't get a 2xx. VPAs starting with "fail" are
  declined (upi_declined), "expire" ones expire (upi_expired), the rest are approved.
"""

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

ChargeStatus = Literal["approved", "declined", "pending"]


class ChargeIn(BaseModel):
    reference: str = Field(min_length=1, max_length=100)
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    token: str = Field(min_length=1, max_length=100)


class Charge(BaseModel):
    charge_id: str
    reference: str
    amount_minor: int
    currency: str
    token: str
    status: ChargeStatus
    decline_code: str | None
    refunded_minor: int = 0
    created_at: str


class RefundIn(BaseModel):
    reference: str = Field(min_length=1, max_length=100)
    charge_reference: str = Field(min_length=1, max_length=100)
    amount_minor: int = Field(gt=0)


class BankRefund(BaseModel):
    refund_id: str
    reference: str
    charge_reference: str
    amount_minor: int
    status: Literal["succeeded", "failed"]
    failure_code: str | None
    created_at: str


CARD_RESULTS: dict[str, tuple[ChargeStatus, str | None]] = {
    "tok_card_ok": ("approved", None),
    "tok_card_slow": ("approved", None),
    "tok_card_no_refunds": ("approved", None),
    "tok_card_declined": ("declined", "card_declined"),
    "tok_card_no_funds": ("declined", "insufficient_funds"),
}


@dataclass
class SentWebhook:
    headers: dict[str, str]
    body: bytes
    delivered: bool


@dataclass
class BankState:
    charges: dict[str, Charge] = field(default_factory=dict)
    refunds: dict[str, BankRefund] = field(default_factory=dict)
    webhooks: list[SentWebhook] = field(default_factory=list)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


def sign(secret: str, body: bytes, now: int | None = None) -> str:
    """SimBank-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "t." + body>."""
    timestamp = int(time.time()) if now is None else now
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def upi_result(vpa: str) -> tuple[ChargeStatus, str | None]:
    if vpa.startswith("fail"):
        return "declined", "upi_declined"
    if vpa.startswith("expire"):
        return "declined", "upi_expired"
    return "approved", None


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(9)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_app(
    slow_seconds: float = 8.0,
    *,
    upi_seconds: float = 3.0,
    webhook_url: str | None = None,
    webhook_secret: str = "",
    webhook_transport: httpx.AsyncBaseTransport | None = None,
    retry_delays: tuple[float, ...] = (0.5, 2.0, 5.0),
) -> FastAPI:
    """webhook_url=None: webhooks are only recorded in `bank.webhooks` (for tests)."""
    bank = BankState()
    app = FastAPI(title="SimBank", summary="A pretend bank for PayForge.")
    app.state.bank = bank

    async def send_webhook(payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if webhook_url is None:
            headers["SimBank-Signature"] = sign(webhook_secret, body)
            bank.webhooks.append(SentWebhook(headers, body, delivered=False))
            return
        async with httpx.AsyncClient(transport=webhook_transport, timeout=5) as client:
            for delay in (0.0, *retry_delays):
                await asyncio.sleep(delay)
                headers["SimBank-Signature"] = sign(webhook_secret, body)  # fresh timestamp
                try:
                    res = await client.post(webhook_url, content=body, headers=headers)
                    if res.is_success:
                        bank.webhooks.append(SentWebhook(dict(headers), body, delivered=True))
                        return
                except httpx.HTTPError:
                    pass
        bank.webhooks.append(SentWebhook(dict(headers), body, delivered=False))

    async def settle_upi(charge: Charge) -> None:
        await asyncio.sleep(upi_seconds)  # the customer approves (or not) in their app
        charge.status, charge.decline_code = upi_result(charge.token.removeprefix("upi:"))
        await send_webhook(
            {
                "id": _id("sbevt"),
                "type": "charge.updated",
                "created_at": _now(),
                "charge": charge.model_dump(),
            }
        )

    def in_background(job: Any) -> None:
        task = asyncio.get_running_loop().create_task(job)
        bank.tasks.add(task)  # keep a reference, or the task may be garbage-collected
        task.add_done_callback(bank.tasks.discard)

    @app.post("/charges")
    async def create_charge(body: ChargeIn) -> Charge:
        # Same reference again → the original charge, never a new one.
        if existing := bank.charges.get(body.reference):
            return existing
        if body.token.startswith("upi:"):
            status, decline_code = "pending", None
        else:
            status, decline_code = CARD_RESULTS.get(body.token, ("declined", "invalid_token"))
        charge = Charge(
            charge_id=_id("ch"),
            reference=body.reference,
            amount_minor=body.amount_minor,
            currency=body.currency,
            token=body.token,
            status=status,
            decline_code=decline_code,
            created_at=_now(),
        )
        # Record first, answer later: a slow bank has still charged the customer.
        bank.charges[body.reference] = charge
        if status == "pending":
            in_background(settle_upi(charge))
        if body.token == "tok_card_slow":
            await asyncio.sleep(slow_seconds)
        return charge

    @app.get("/charges/{reference}")
    async def get_charge(reference: str) -> Charge:
        if charge := bank.charges.get(reference):
            return charge
        raise HTTPException(404, "charge_not_found")

    @app.post("/refunds")
    async def create_refund(body: RefundIn) -> BankRefund:
        if existing := bank.refunds.get(body.reference):
            return existing
        charge = bank.charges.get(body.charge_reference)
        failure: str | None = None
        if charge is None or charge.status != "approved":
            failure = "charge_not_refundable"
        elif charge.token == "tok_card_no_refunds":
            failure = "refund_not_allowed"
        elif charge.refunded_minor + body.amount_minor > charge.amount_minor:
            failure = "amount_too_large"
        if charge is not None and failure is None:
            charge.refunded_minor += body.amount_minor
        refund = BankRefund(
            refund_id=_id("rf"),
            reference=body.reference,
            charge_reference=body.charge_reference,
            amount_minor=body.amount_minor,
            status="failed" if failure else "succeeded",
            failure_code=failure,
            created_at=_now(),
        )
        bank.refunds[body.reference] = refund
        return refund

    @app.get("/refunds/{reference}")
    async def get_refund(reference: str) -> BankRefund:
        if refund := bank.refunds.get(reference):
            return refund
        raise HTTPException(404, "refund_not_found")

    @app.get("/debug/stats")
    async def stats() -> dict[str, int]:
        """For tests and demos: how many charges/refunds the bank has really made."""
        return {"charges": len(bank.charges), "refunds": len(bank.refunds)}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
