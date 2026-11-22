"""Dashboard authentication: sign-up, login, logout, who am I."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from payforge.api.deps import CurrentSession, optional_session, require_json
from payforge.auth.sessions import (
    Session,
    clear_session_cookie,
    create_session,
    destroy_session,
    set_session_cookie,
)
from payforge.services import users

router = APIRouter(prefix="/auth", tags=["Dashboard auth"], dependencies=[Depends(require_json)])


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


class MeOut(BaseModel):
    user: UserOut
    merchant: MerchantOut
    csrf_token: str = Field(description="Send back in the X-CSRF-Token header on dashboard writes.")


@router.post("/signup", status_code=201)
async def signup(body: SignupIn, response: Response) -> SignupOut:
    user = await users.sign_up(
        email=str(body.email), password=body.password, merchant_name=body.merchant_name
    )
    session = await create_session(user)
    set_session_cookie(response, session.id)
    return SignupOut(
        user=UserOut(id=user.id, email=user.email, role=user.role), merchant_id=user.merchant_id
    )


@router.post("/login", status_code=204)
async def login(body: LoginIn) -> Response:
    user = await users.log_in(body.email, body.password)
    session = await create_session(user)  # a NEW session id at every login (no session fixation)
    response = Response(status_code=204)
    set_session_cookie(response, session.id)
    return response


@router.post("/logout", status_code=204)
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
        merchant=MerchantOut(id=merchant.id, name=merchant.name),
        csrf_token=session.csrf,
    )
