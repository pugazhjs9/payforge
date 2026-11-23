"""Endpoints the bank calls. Authenticated by signature, not by a token."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from payforge import webhook_signing
from payforge.clients import simbank
from payforge.config import settings
from payforge.errors import AppError, Unauthorized
from payforge.services import bank_webhooks

router = APIRouter(tags=["Bank webhooks"])


class Received(BaseModel):
    status: bank_webhooks.Outcome


@router.post("/webhooks/simbank")
async def simbank_webhook(request: Request) -> Received:
    """Answers 2xx once the event is safely recorded (even a duplicate or an ignored
    one), so the bank stops resending it. 401 / 400 make the bank retry or alert."""
    if not settings.simbank_webhook_secret:
        raise AppError(503, "not_configured", "Bank webhooks are not configured.")
    body = await request.body()  # the RAW bytes: the signature covers exactly these
    signature = request.headers.get("simbank-signature", "")
    if not webhook_signing.verify(settings.simbank_webhook_secret, signature, body):
        raise Unauthorized("invalid_signature", "Missing, wrong or expired SimBank-Signature.")
    try:
        update = simbank.parse_charge_update(body)
    except ValueError as exc:
        raise AppError(400, "invalid_payload", "Not a webhook PayForge understands.") from exc
    return Received(status=await bank_webhooks.handle_charge_update(update))
