"""What a merchant's server needs to talk to PayForge: tokens, payments, webhooks.

Deliberately independent of the PayForge code base, like a real merchant's would be.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class PayForge:
    base_url: str
    key_id: str
    key_secret: str
    http: httpx.AsyncClient = field(default_factory=lambda: httpx.AsyncClient(timeout=10))
    _token: str | None = None
    _token_expires: float = 0.0

    async def _access_token(self) -> str:
        """API key → 15-minute access token, reused until shortly before it expires."""
        if self._token is None or time.monotonic() > self._token_expires - 60:
            res = await self.http.post(
                f"{self.base_url}/v1/auth/token",
                auth=(self.key_id, self.key_secret),
                data={"grant_type": "client_credentials"},
            )
            res.raise_for_status()
            self._token = res.json()["access_token"]
            self._token_expires = time.monotonic() + res.json()["expires_in"]
        return self._token

    async def create_payment(
        self, *, order_id: str, amount: str, payment_method: str
    ) -> dict[str, object]:
        """The ORDER id is the idempotency key: retrying this call after a timeout
        can never charge the customer twice for the same order."""
        res = await self.http.post(
            f"{self.base_url}/v1/payments",
            json={
                "amount": amount,
                "currency": "INR",
                "payment_method": payment_method,
                "description": f"Order {order_id}",
                "metadata": {"order_id": order_id},
            },
            headers={
                "Authorization": f"Bearer {await self._access_token()}",
                "Idempotency-Key": f"order-{order_id}",
            },
        )
        res.raise_for_status()
        result: dict[str, object] = res.json()
        return result


def verify_webhook(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    """Checks `PayForge-Signature: t=…,v1=…` over the RAW request body."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp, signature = parts.get("t", ""), parts.get("v1", "")
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > tolerance:
        return False  # missing, or too old: a replayed webhook
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), signature)
