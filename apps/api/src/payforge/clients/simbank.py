"""Talking to the bank. Two rules:

1. Every call has a timeout. A slow bank must never hang our requests.
2. Anything that is not a clear answer (timeout, connection error, 5xx, garbage)
   is Unknown: the bank MAY have acted, so it is never treated as a failure.
   The reference we send (our payment / refund ID) lets us ask again later.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from payforge.config import settings
from payforge.logging import log


@dataclass(frozen=True)
class Approved:
    charge_id: str


@dataclass(frozen=True)
class Declined:
    charge_id: str
    decline_code: str


@dataclass(frozen=True)
class Pending:
    charge_id: str


@dataclass(frozen=True)
class Unknown:
    reason: str


@dataclass(frozen=True)
class NotFoundAtBank:
    pass


@dataclass(frozen=True)
class RefundSucceeded:
    refund_id: str


@dataclass(frozen=True)
class RefundFailed:
    refund_id: str
    failure_code: str


ChargeOutcome = Approved | Declined | Pending | Unknown
RefundOutcome = RefundSucceeded | RefundFailed | Unknown


class _ChargeAnswer(BaseModel):
    charge_id: str
    status: Literal["approved", "declined", "pending"]
    decline_code: str | None


class _RefundAnswer(BaseModel):
    refund_id: str
    status: Literal["succeeded", "failed"]
    failure_code: str | None


_UNCLEAR = (TimeoutError, httpx.HTTPError, ValueError)  # ValueError covers bad JSON / schema
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _call(method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    async with asyncio.timeout(settings.simbank_timeout_seconds):
        return await _http().request(method, f"{settings.simbank_url}{path}", json=body)


def _charge_outcome(answer: _ChargeAnswer) -> ChargeOutcome:
    if answer.status == "approved":
        return Approved(answer.charge_id)
    if answer.status == "pending":
        return Pending(answer.charge_id)
    return Declined(answer.charge_id, answer.decline_code or "declined")


def _refund_outcome(answer: _RefundAnswer) -> RefundOutcome:
    if answer.status == "succeeded":
        return RefundSucceeded(answer.refund_id)
    return RefundFailed(answer.refund_id, answer.failure_code or "failed")


async def charge(*, reference: str, amount_minor: int, currency: str, token: str) -> ChargeOutcome:
    body = {
        "reference": reference,
        "amount_minor": amount_minor,
        "currency": currency,
        "token": token,
    }
    try:
        res = await _call("POST", "/charges", body)
        if res.status_code != 200:
            return Unknown(f"simbank answered {res.status_code}")
        return _charge_outcome(_ChargeAnswer.model_validate(res.json()))
    except _UNCLEAR as exc:
        log.warning("simbank charge failed: outcome unknown", reference=reference, error=repr(exc))
        return Unknown(repr(exc))


async def get_charge(reference: str) -> ChargeOutcome | NotFoundAtBank:
    """'What happened to this charge?' — used after a timeout."""
    try:
        res = await _call("GET", f"/charges/{reference}")
        if res.status_code == 404:
            return NotFoundAtBank()
        if res.status_code != 200:
            return Unknown(f"simbank answered {res.status_code}")
        return _charge_outcome(_ChargeAnswer.model_validate(res.json()))
    except _UNCLEAR as exc:
        return Unknown(repr(exc))


async def refund(*, reference: str, charge_reference: str, amount_minor: int) -> RefundOutcome:
    body = {
        "reference": reference,
        "charge_reference": charge_reference,
        "amount_minor": amount_minor,
    }
    try:
        res = await _call("POST", "/refunds", body)
        if res.status_code != 200:
            return Unknown(f"simbank answered {res.status_code}")
        return _refund_outcome(_RefundAnswer.model_validate(res.json()))
    except _UNCLEAR as exc:
        log.warning("simbank refund failed: outcome unknown", reference=reference, error=repr(exc))
        return Unknown(repr(exc))


async def get_refund(reference: str) -> RefundOutcome | NotFoundAtBank:
    try:
        res = await _call("GET", f"/refunds/{reference}")
        if res.status_code == 404:
            return NotFoundAtBank()
        if res.status_code != 200:
            return Unknown(f"simbank answered {res.status_code}")
        return _refund_outcome(_RefundAnswer.model_validate(res.json()))
    except _UNCLEAR as exc:
        return Unknown(repr(exc))
