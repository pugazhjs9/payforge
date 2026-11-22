from payforge.services.ledger import (
    PLATFORM_FEES,
    WORLD_BANK,
    Transfer,
    payment_transfers,
    refund_transfers,
)


def test_payment_splits_the_amount_between_merchant_and_fees() -> None:
    transfers = payment_transfers("mer_1", 10_000, 200)

    assert transfers == [
        Transfer(WORLD_BANK, "merchant:mer_1:balance", 9_800),
        Transfer(WORLD_BANK, PLATFORM_FEES, 200),
    ]
    assert sum(t.amount_minor for t in transfers) == 10_000


def test_refund_moves_money_from_the_merchant_back_to_the_world() -> None:
    assert refund_transfers("mer_1", 5_000) == [
        Transfer("merchant:mer_1:balance", WORLD_BANK, 5_000)
    ]
