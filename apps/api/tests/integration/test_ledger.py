import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from payforge.db import SessionLocal
from payforge.services.ledger import (
    PLATFORM_FEES,
    WORLD_BANK,
    Transfer,
    get_balance,
    merchant_account,
    payment_transfers,
    post_transaction,
    refund_transfers,
)

from .conftest import MERCHANT_A

MERCHANT = merchant_account(MERCHANT_A)


async def post(key: str, transfers: list[Transfer]) -> None:
    async with SessionLocal.begin() as session:
        await post_transaction(session, key=key, merchant_id=MERCHANT_A, transfers=transfers)


async def test_worked_example_payment_then_refund() -> None:
    await post("payment:pay_A", payment_transfers(MERCHANT_A, 10_000, 200))
    await post("refund:re_B", refund_transfers(MERCHANT_A, 5_000))

    assert await get_balance(MERCHANT) == 4_800
    assert await get_balance(PLATFORM_FEES) == 200
    assert await get_balance(WORLD_BANK) == -5_000
    # every transfer balances by itself, so all balances together are exactly zero
    assert 4_800 + 200 - 5_000 == 0


async def test_posting_the_same_key_twice_is_rejected() -> None:
    await post("payment:pay_A", payment_transfers(MERCHANT_A, 10_000, 200))

    with pytest.raises(IntegrityError):
        await post("payment:pay_A", payment_transfers(MERCHANT_A, 10_000, 200))
    assert await get_balance(MERCHANT) == 9_800


async def test_database_rejects_zero_or_negative_transfers() -> None:
    async with SessionLocal.begin() as session:
        await session.execute(
            text(
                "INSERT INTO ledger_transactions (id, transaction_key, merchant_id) "
                "VALUES ('ltx_x', 'k', 'mer_a')"
            )
        )
    with pytest.raises(DBAPIError, match="ledger_transfers_amount_positive"):
        async with SessionLocal.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO ledger_transfers "
                    "(transaction_id, from_account, to_account, amount_minor) "
                    "VALUES ('ltx_x', 'a', 'b', -100)"
                )
            )


async def test_ledger_transfers_cannot_be_edited_or_deleted() -> None:
    await post("payment:pay_A", payment_transfers(MERCHANT_A, 10_000, 200))

    for statement in (
        "UPDATE ledger_transfers SET amount_minor = 1",
        "DELETE FROM ledger_transfers",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with SessionLocal.begin() as session:
                await session.execute(text(statement))
    assert await get_balance(MERCHANT) == 9_800
