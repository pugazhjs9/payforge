"""'Sign in with GitHub': OAuth 2.0 authorization code flow with PKCE.

1. /auth/github: create `state` + a PKCE verifier, remember them for 10 minutes,
   redirect the browser to GitHub with state + SHA-256(verifier).
2. The user approves on GitHub; GitHub redirects back with ?code&state.
3. /auth/github/callback: `state` must be one we created (CSRF protection for OAuth).
   Then, server to server, exchange code + client secret + verifier for an access
   token, and read the user's profile and verified emails.
The client secret and the access token never pass through the browser.
"""

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel
from sqlalchemy import select

from payforge.config import settings
from payforge.db import SessionLocal
from payforge.errors import AppError
from payforge.ids import new_id
from payforge.models import Merchant, User, UserRole
from payforge.redis import redis


def github_login_enabled() -> bool:
    return bool(settings.github_client_id and settings.github_client_secret)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _state_key(state: str) -> str:
    return f"oauth:github:{state}"


async def authorize_url() -> str:
    """Step 1: where to send the browser."""
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(32)
    await redis.set(_state_key(state), verifier, ex=600)
    query = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.github_callback_url,
            "scope": "read:user user:email",
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.github_oauth_base}/login/oauth/authorize?{query}"


class _GithubUser(BaseModel):
    id: int
    login: str
    name: str | None = None


class _GithubEmail(BaseModel):
    email: str
    primary: bool
    verified: bool


async def _github(method: str, url: str, **kwargs: Any) -> Any:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.request(method, url, **kwargs)
            res.raise_for_status()
            return res.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppError(502, "github_unavailable", "GitHub did not answer correctly.") from exc


async def finish_login(code: str, state: str) -> User:
    """Step 3: turn the callback's code into a PayForge user (found, linked or created)."""
    verifier = await redis.getdel(_state_key(state))  # one-time use
    if verifier is None:
        raise AppError(
            400, "invalid_oauth_state", "This login link is invalid or expired. Try again."
        )

    token = await _github(
        "POST",
        f"{settings.github_oauth_base}/login/oauth/access_token",
        headers={"accept": "application/json"},
        data={
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
            "redirect_uri": settings.github_callback_url,
            "code_verifier": verifier,
        },
    )
    if "access_token" not in token:
        raise AppError(
            400, "github_login_failed", str(token.get("error_description", "login failed"))
        )

    auth = {"authorization": f"Bearer {token['access_token']}", "accept": "application/json"}
    gh_user = _GithubUser.model_validate(
        await _github("GET", f"{settings.github_api_base}/user", headers=auth)
    )
    emails = [
        _GithubEmail.model_validate(e)
        for e in await _github("GET", f"{settings.github_api_base}/user/emails", headers=auth)
    ]
    verified = next((e.email.lower() for e in emails if e.primary and e.verified), None)
    return await _find_or_create(str(gh_user.id), gh_user.login, gh_user.name, verified)


async def _find_or_create(
    github_id: str, login: str, name: str | None, verified_email: str | None
) -> User:
    async with SessionLocal.begin() as session:
        user = await session.scalar(select(User).where(User.github_id == github_id))
        if user is not None:
            return user

        # Link to an existing account ONLY through a verified email. Otherwise anyone
        # could add your (unverified) email to their GitHub account and log in as you.
        if verified_email is not None:
            user = await session.scalar(select(User).where(User.email == verified_email))
            if user is not None:
                user.github_id = github_id
                return user

        merchant = Merchant(id=new_id("mer"), name=f"{login}'s shop")
        session.add(merchant)
        await session.flush()
        user = User(
            id=new_id("usr"),
            merchant_id=merchant.id,
            email=verified_email or f"{github_id}+{login}@users.noreply.github.com".lower(),
            github_id=github_id,
            name=name,
            role=UserRole.OWNER,
        )
        session.add(user)
        return user
