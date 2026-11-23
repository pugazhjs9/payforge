"""Webhook signatures: how a receiver knows a webhook really came from PayForge.

    PayForge-Signature: t=1763870000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e7…

v1 = hex(HMAC-SHA256(endpoint secret, f"{t}." + raw body))

The timestamp is part of what's signed and receivers reject old ones (5 minutes by
default), so a captured webhook can't be replayed later. Receivers must check the RAW
body bytes: parsing and re-serialising the JSON can change them. During a secret
rotation a header may carry several v1 values; any one matching is enough.
"""

import hashlib
import hmac
import time

SIGNATURE_HEADER = "PayForge-Signature"
TOLERANCE_SECONDS = 300


def compute(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def header_value(secret: str, body: bytes, now: int | None = None) -> str:
    timestamp = int(time.time()) if now is None else now
    return f"t={timestamp},v1={compute(secret, timestamp, body)}"


def verify(
    secret: str,
    header: str,
    body: bytes,
    *,
    tolerance: int = TOLERANCE_SECONDS,
    now: int | None = None,
) -> bool:
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isascii() and value.isdigit():
            timestamp = int(value)
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance:
        return False  # too old (a replay?) or from the future
    expected = compute(secret, timestamp, body)
    # constant-time comparison: no timing hints about how much of a guess was right
    return any(hmac.compare_digest(expected, s) for s in signatures)
