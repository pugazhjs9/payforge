"""Dashboard authentication: sign-up, login, logout, who am I."""

import hashlib
import math
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from redis.exceptions import RedisError

from payforge import ratelimit
from payforge.api.deps import (
    CurrentSession,
    check_origin,
    optional_session,
    require_csrf_token,
    require_json,
)
from payforge.auth.sessions import (
    Session,
    clear_session_cookie,
    create_session,
    destroy_session,
    set_session_cookie,
)
from payforge.config import settings
from payforge.errors import AppError
from payforge.logging import log
from payforge.redis import redis
from payforge.services import logos, users

# signup/login have no session yet, so the Origin check is their CSRF protection
router = APIRouter(
    prefix="/auth",
    tags=["Dashboard auth"],
    dependencies=[Depends(require_json), Depends(check_origin)],
)


class SignupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=12, max_length=128)
    merchant_name: str = Field(min_length=1, max_length=100)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class UserOut(BaseModel):
    id: str
    email: str
    name: str | None = None
    role: str


class SignupOut(BaseModel):
    user: UserOut
    merchant_id: str


class MerchantOut(BaseModel):
    id: str
    name: str
    logo_url: str | None = None


class MeOut(BaseModel):
    user: UserOut
    merchant: MerchantOut
    csrf_token: str = Field(description="Send back in the X-CSRF-Token header on dashboard writes.")


LOGIN_WINDOW_SECONDS = 15 * 60
SIGNUP_WINDOW_SECONDS = 60 * 60


def client_ip(request: Request) -> str:
    """The caller's IP. Behind a load balancer, run uvicorn with --proxy-headers and
    --forwarded-allow-ips=<balancer IP>: it then takes this from X-Forwarded-For, and
    only when the header comes from the balancer (clients can't fake it)."""
    return request.client.host if request.client else "unknown"


def account_key(email: str, ip: str) -> str:
    # hashed: no email addresses sitting in Redis key names
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]
    return f"rl:login:acct:{digest}:{ip}"


async def check_attempts(*limits: tuple[str, int, int]) -> None:
    """Counts an attempt against each (key, limit, window) and refuses if any is over.

    Fails CLOSED (503) when Redis is down: these limits stop password guessing, and
    without them an attacker gets unlimited tries. It costs nothing extra, because
    logging in needs Redis anyway (that's where sessions live).
    """
    try:
        decisions = [
            await ratelimit.count_attempt(key, limit=limit, window_seconds=window)
            for key, limit, window in limits
        ]
    except RedisError as exc:
        log.error("rate_limiter_unavailable", error=str(exc))
        raise AppError(
            503,
            "temporarily_unavailable",
            "Login is temporarily unavailable. Try again in a moment.",
            headers={"Retry-After": "30"},
        ) from exc
    refused = [d for d in decisions if not d.allowed]
    if refused:
        wait = max(d.retry_after_seconds for d in refused)
        raise AppError(
            429,
            "too_many_attempts",
            f"Too many attempts. Try again in {math.ceil(wait / 60)} minutes.",
            headers={"Retry-After": str(wait)},
        )


@router.post("/signup", status_code=201)
async def signup(body: SignupIn, request: Request, response: Response) -> SignupOut:
    await check_attempts(
        (f"rl:signup:ip:{client_ip(request)}", settings.signups_per_ip, SIGNUP_WINDOW_SECONDS)
    )
    user = await users.sign_up(
        email=str(body.email), password=body.password, merchant_name=body.merchant_name
    )
    session = await create_session(user)
    set_session_cookie(response, session.id)
    return SignupOut(
        user=UserOut(id=user.id, email=user.email, role=user.role), merchant_id=user.merchant_id
    )


@router.post("/login", status_code=204)
async def login(body: LoginIn, request: Request) -> Response:
    """Limited per email + IP (slows guessing one account's password) and per IP (slows
    one machine trying many accounts). Not per email alone: then anyone could lock a
    real user out just by typing their email with wrong passwords."""
    ip = client_ip(request)
    acct = account_key(body.email, ip)
    await check_attempts(
        (acct, settings.login_attempts_per_account, LOGIN_WINDOW_SECONDS),
        (f"rl:login:ip:{ip}", settings.login_attempts_per_ip, LOGIN_WINDOW_SECONDS),
    )
    user = await users.log_in(body.email, body.password)
    await redis.delete(acct)  # a successful login clears this account's failed attempts
    session = await create_session(user)  # a NEW session id at every login (no session fixation)
    response = Response(status_code=204)
    set_session_cookie(response, session.id)
    return response


@router.post("/logout", status_code=204, dependencies=[Depends(require_csrf_token)])
async def logout(session: Annotated[Session | None, Depends(optional_session)]) -> Response:
    if session is not None:
        await destroy_session(session.id)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me")
async def me(session: CurrentSession) -> MeOut:
    user, merchant = await users.get_user(session.user_id)
    return MeOut(
        user=UserOut(id=user.id, email=user.email, name=user.name, role=user.role),
        merchant=MerchantOut(id=merchant.id, name=merchant.name, logo_url=logos.logo_url(merchant)),
        csrf_token=session.csrf,
    )
