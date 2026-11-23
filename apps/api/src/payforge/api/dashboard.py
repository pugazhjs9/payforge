"""Dashboard API (browser, session cookie): API keys and webhook endpoints."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from payforge.api.deps import (
    CurrentSession,
    check_origin,
    require_csrf_token,
    require_json,
    require_role,
)
from payforge.models import ApiKey, UserRole, WebhookAttempt, WebhookDelivery, WebhookEndpoint
from payforge.services import api_keys, webhooks
from payforge.services.api_keys import Scope

router = APIRouter(
    prefix="/dashboard",
    tags=["Dashboard"],
    dependencies=[Depends(require_json), Depends(check_origin), Depends(require_csrf_token)],
)

can_manage_keys = require_role(UserRole.OWNER, UserRole.DEVELOPER)
can_manage_webhooks = require_role(UserRole.OWNER, UserRole.DEVELOPER)


class CreateApiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    scopes: Annotated[list[Scope], Field(min_length=1)] = list(api_keys.SCOPES)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    scopes: list[str]
    secret_last4: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def of(cls, k: ApiKey) -> "ApiKeyOut":
        return cls(
            id=k.id,
            name=k.name,
            scopes=k.scopes,
            secret_last4=k.secret_last4,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            revoked_at=k.revoked_at,
        )


class NewApiKeyOut(ApiKeyOut):
    secret: str = Field(description="Shown ONCE. Store it now; it can't be shown again.")


class ApiKeyList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ApiKeyOut]


@router.post("/api-keys", status_code=201, dependencies=[can_manage_keys])
async def create_api_key(body: CreateApiKey, session: CurrentSession) -> NewApiKeyOut:
    created = await api_keys.create_api_key(session.merchant_id, body.name, list(body.scopes))
    return NewApiKeyOut(**ApiKeyOut.of(created.key).model_dump(), secret=created.secret)


@router.get("/api-keys")
async def list_api_keys(session: CurrentSession) -> ApiKeyList:
    return ApiKeyList(
        data=[ApiKeyOut.of(k) for k in await api_keys.list_api_keys(session.merchant_id)]
    )


@router.delete("/api-keys/{key_id}", status_code=204, dependencies=[can_manage_keys])
async def revoke_api_key(key_id: str, session: CurrentSession) -> Response:
    await api_keys.revoke_api_key(session.merchant_id, key_id)
    return Response(status_code=204)


# ── webhook endpoints ─────────────────────────────────────────────────────────

EventTypeOrAll = Literal[
    "*", "payment.succeeded", "payment.failed", "refund.succeeded", "refund.failed"
]


class CreateWebhookEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=500, examples=["https://shop.example/webhooks"])
    event_types: Annotated[list[EventTypeOrAll], Field(min_length=1, max_length=10)] = ["*"]
    description: str | None = Field(default=None, max_length=100)


class WebhookEndpointOut(BaseModel):
    id: str
    url: str
    description: str | None
    event_types: list[str]
    created_at: datetime

    @classmethod
    def of(cls, e: WebhookEndpoint) -> "WebhookEndpointOut":
        return cls(
            id=e.id,
            url=e.url,
            description=e.description,
            event_types=e.event_types,
            created_at=e.created_at,
        )


class NewWebhookEndpointOut(WebhookEndpointOut):
    secret: str = Field(description="Verifies the PayForge-Signature header. Shown ONCE.")


class WebhookEndpointList(BaseModel):
    object: Literal["list"] = "list"
    data: list[WebhookEndpointOut]


class DeliveryOut(BaseModel):
    id: str
    event_id: str
    event_type: str
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str | None
    next_attempt_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, d: WebhookDelivery, event_type: str) -> "DeliveryOut":
        return cls(
            id=d.id,
            event_id=d.event_id,
            event_type=event_type,
            status=d.status,
            attempts=d.attempts,
            last_status_code=d.last_status_code,
            last_error=d.last_error,
            next_attempt_at=d.next_attempt_at,
            created_at=d.created_at,
        )


class DeliveryList(BaseModel):
    object: Literal["list"] = "list"
    data: list[DeliveryOut]


class AttemptOut(BaseModel):
    number: int
    status_code: int | None
    error: str | None
    duration_ms: int
    response_body: str | None
    created_at: datetime

    @classmethod
    def of(cls, a: WebhookAttempt) -> "AttemptOut":
        return cls(
            number=a.number,
            status_code=a.status_code,
            error=a.error,
            duration_ms=a.duration_ms,
            response_body=a.response_body,
            created_at=a.created_at,
        )


class AttemptList(BaseModel):
    object: Literal["list"] = "list"
    data: list[AttemptOut]


@router.post("/webhook-endpoints", status_code=201, dependencies=[can_manage_webhooks])
async def create_webhook_endpoint(
    body: CreateWebhookEndpoint, session: CurrentSession
) -> NewWebhookEndpointOut:
    """`https://` only; the host must resolve to a public address (SSRF protection)."""
    types: list[str] = ["*"] if "*" in body.event_types else sorted(set(body.event_types))
    endpoint = await webhooks.create_endpoint(
        session.merchant_id, body.url, types, body.description
    )
    return NewWebhookEndpointOut(
        **WebhookEndpointOut.of(endpoint).model_dump(), secret=endpoint.secret
    )


@router.get("/webhook-endpoints")
async def list_webhook_endpoints(session: CurrentSession) -> WebhookEndpointList:
    return WebhookEndpointList(
        data=[WebhookEndpointOut.of(e) for e in await webhooks.list_endpoints(session.merchant_id)]
    )


@router.delete(
    "/webhook-endpoints/{endpoint_id}", status_code=204, dependencies=[can_manage_webhooks]
)
async def delete_webhook_endpoint(endpoint_id: str, session: CurrentSession) -> Response:
    await webhooks.delete_endpoint(session.merchant_id, endpoint_id)
    return Response(status_code=204)


@router.get("/webhook-endpoints/{endpoint_id}/deliveries")
async def list_webhook_deliveries(endpoint_id: str, session: CurrentSession) -> DeliveryList:
    """The delivery log: the 50 most recent deliveries to this endpoint."""
    rows = await webhooks.list_deliveries(session.merchant_id, endpoint_id)
    return DeliveryList(data=[DeliveryOut.of(d, event_type) for d, event_type in rows])


@router.get("/webhook-deliveries/{delivery_id}/attempts")
async def list_webhook_attempts(delivery_id: str, session: CurrentSession) -> AttemptList:
    attempts = await webhooks.list_attempts(session.merchant_id, delivery_id)
    return AttemptList(data=[AttemptOut.of(a) for a in attempts])


@router.post(
    "/webhook-deliveries/{delivery_id}/resend", status_code=202, dependencies=[can_manage_webhooks]
)
async def resend_webhook_delivery(delivery_id: str, session: CurrentSession) -> Response:
    """Sends a finished delivery again, within seconds (202: queued, not sent yet)."""
    await webhooks.resend(session.merchant_id, delivery_id)
    return Response(status_code=202)
