"""PayForge as an OAuth 2.0 authorization server (client credentials grant).

A merchant's server sends its API key ID + secret once and gets a 15-minute JWT.
The long-lived secret travels rarely; the short-lived token is what every API call
carries, and any service can verify it with the public key from the JWKS endpoint.
"""

import base64
import binascii
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel

from payforge.auth.tokens import issue_access_token, jwks
from payforge.config import settings
from payforge.errors import OAuthError
from payforge.services import api_keys

router = APIRouter(tags=["Authentication"])


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105  (a token type, not a secret)
    expires_in: int
    scope: str


def _basic_credentials(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise OAuthError(401, "invalid_client", "Malformed Basic credentials.") from exc
    key_id, sep, secret = decoded.partition(":")
    if not sep:
        raise OAuthError(401, "invalid_client", "Malformed Basic credentials.")
    return key_id, secret


@router.post("/v1/auth/token", summary="Exchange an API key for an access token")
async def token(
    request: Request,
    response: Response,
    grant_type: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
) -> TokenOut:
    """Send `grant_type=client_credentials` (form-encoded) with HTTP Basic auth
    `base64(key_id:secret)`. Errors use the OAuth format: `{"error": "invalid_client"}`."""
    if grant_type is None:
        raise OAuthError(400, "invalid_request", "grant_type is required (form-encoded).")
    if grant_type != "client_credentials":
        raise OAuthError(400, "unsupported_grant_type", "Only client_credentials is supported.")
    key_id, secret = _basic_credentials(request.headers.get("authorization")) or (
        client_id,
        client_secret,
    )
    if not key_id or not secret:
        raise OAuthError(
            401, "invalid_client", "Send the API key ID and secret with HTTP Basic auth."
        )

    key = await api_keys.verify_client_credentials(key_id, secret)
    response.headers["Cache-Control"] = "no-store"  # tokens must never be cached
    return TokenOut(
        access_token=issue_access_token(
            merchant_id=key.merchant_id, key_id=key.id, scopes=key.scopes
        ),
        expires_in=settings.access_token_ttl_seconds,
        scope=" ".join(key.scopes),
    )


@router.get("/.well-known/jwks.json", summary="Public keys that verify PayForge access tokens")
async def jwks_endpoint() -> dict[str, list[dict[str, Any]]]:
    return jwks()
