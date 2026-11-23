"""Double-entry ledger, stored as transfers.

Money never appears or disappears: it moves FROM one account TO another. Each
transfer row balances by itself, so the books always sum to zero. A balance is
computed from transfers (money in - money out), never stored and edited.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from payforge import cache
from payforge.db import SessionLocal
from payforge.ids import new_id
from payforge.models import LedgerTransaction, LedgerTransfer

BALANCE_TTL_SECONDS = 30
WORLD_BANK = "world:bank"  # money coming in from / going out to the outside world
PLATFORM_FEES = "platform:fees"  # PayForge's earnings


def merchant_account(merchant_id: str) -> str:
    """What PayForge owes a merchant."""
    return f"merchant:{merchant_id}:balance"


@dataclass(frozen=True)
class Transfer:
    from_account: str
    to_account: str
    amount_minor: int


async def post_transaction(
    session: AsyncSession, *, key: str, merchant_id: str, transfers: list[Transfer]
) -> None:
    """Write one ledger transaction inside the caller's database transaction.

    `key` is unique (e.g. "payment:pay_…"), so posting the same thing twice fails
    instead of counting the money twice.
    """
    moves = [t for t in transfers if t.amount_minor != 0]
    if any(t.amount_minor < 0 for t in moves):
        raise ValueError(f"ledger transfers must be positive: {moves}")
    if not moves:
        return
    ltx = LedgerTransaction(id=new_id("ltx"), transaction_key=key, merchant_id=merchant_id)
    session.add(ltx)
    await session.flush()  # a repeated key fails here (UNIQUE constraint)
    session.add_all(
        LedgerTransfer(
            transaction_id=ltx.id,
            from_account=t.from_account,
            to_account=t.to_account,
            amount_minor=t.amount_minor,
        )
        for t in moves
    )
    await session.flush()


def payment_transfers(merchant_id: str, amount_minor: int, fee_minor: int) -> list[Transfer]:
    """A succeeded payment: the merchant is owed the amount minus PayForge's fee."""
    return [
        Transfer(WORLD_BANK, merchant_account(merchant_id), amount_minor - fee_minor),
        Transfer(WORLD_BANK, PLATFORM_FEES, fee_minor),
    ]


def refund_transfers(merchant_id: str, amount_minor: int) -> list[Transfer]:
    """A succeeded refund: money goes back out of the merchant's balance (fee not returned)."""
    return [Transfer(merchant_account(merchant_id), WORLD_BANK, amount_minor)]


async def get_balance(account: str) -> int:
    """Money in - money out. Each half is served by its own index."""
    money_in = (
        select(func.coalesce(func.sum(LedgerTransfer.amount_minor), 0))
        .where(LedgerTransfer.to_account == account)
        .scalar_subquery()
    )
    money_out = (
        select(func.coalesce(func.sum(LedgerTransfer.amount_minor), 0))
        .where(LedgerTransfer.from_account == account)
        .scalar_subquery()
    )
    async with SessionLocal() as session:
        balance = await session.scalar(select(money_in - money_out))
    return int(balance or 0)


def balance_cache_key(merchant_id: str) -> str:
    return f"cache:balance:{merchant_id}"


async def merchant_balance(merchant_id: str) -> int:
    """The merchant's balance, cached. get_balance() sums every transfer the merchant
    ever had, so it gets slower as the ledger grows; the cache absorbs dashboards and
    scripts polling it. Whoever posts to the ledger calls forget_balance() after COMMIT.

    Display only: anything that MOVES money (payouts, later) must read get_balance().
    """
    value = await cache.get_or_load(
        balance_cache_key(merchant_id),
        BALANCE_TTL_SECONDS,
        lambda: get_balance(merchant_account(merchant_id)),
    )
    return int(value)


async def forget_balance(merchant_id: str) -> None:
    await cache.invalidate(balance_cache_key(merchant_id))
