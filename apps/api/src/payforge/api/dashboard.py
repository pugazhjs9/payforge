"""Dashboard API (browser, session cookie): manage API keys."""

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
from payforge.models import ApiKey, UserRole
from payforge.services import api_keys
from payforge.services.api_keys import Scope

router = APIRouter(
    prefix="/dashboard",
    tags=["Dashboard"],
    dependencies=[Depends(require_json), Depends(check_origin), Depends(require_csrf_token)],
)

can_manage_keys = require_role(UserRole.OWNER, UserRole.DEVELOPER)


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
