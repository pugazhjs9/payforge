"""Dashboard users: sign-up and password login."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from payforge.auth.passwords import DUMMY_HASH, hash_password, verify_password
from payforge.db import SessionLocal
from payforge.errors import Conflict, Unauthorized
from payforge.ids import new_id
from payforge.models import Merchant, User, UserRole


async def sign_up(*, email: str, password: str, merchant_name: str) -> User:
    """Creates a merchant and its first user (the owner)."""
    password_hash = await hash_password(password)
    try:
        async with SessionLocal.begin() as session:
            merchant = Merchant(id=new_id("mer"), name=merchant_name)
            session.add(merchant)
            await session.flush()
            user = User(
                id=new_id("usr"),
                merchant_id=merchant.id,
                email=email.lower(),
                password_hash=password_hash,
                role=UserRole.OWNER,
            )
            session.add(user)
    except IntegrityError as exc:
        raise Conflict("email_taken", "An account with this email already exists.") from exc
    return user


async def log_in(email: str, password: str) -> User:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email.lower()))
    # Always run argon2 (against a dummy hash if needed): the response time must not
    # reveal whether the email exists.
    stored = user.password_hash if user and user.password_hash else DUMMY_HASH
    ok = await verify_password(stored, password)
    if user is None or user.password_hash is None or not ok:
        raise Unauthorized("invalid_credentials", "Email or password is wrong.")
    return user


async def get_user(user_id: str) -> tuple[User, Merchant]:
    async with SessionLocal() as session:
        user = await session.get_one(User, user_id)
        merchant = await session.get_one(Merchant, user.merchant_id)
    return user, merchant
