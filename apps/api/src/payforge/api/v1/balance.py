from fastapi import APIRouter

from payforge.api.deps import PaymentsRead
from payforge.api.v1.schemas import BalanceOut
from payforge.domain.money import format_minor
from payforge.services.ledger import merchant_balance

router = APIRouter()


@router.get("/balance", tags=["Balance"])
async def balance(merchant: PaymentsRead) -> BalanceOut:
    """What PayForge owes the merchant, computed from the double-entry ledger.
    Cached for up to ~30 s, and refreshed as soon as a payment or refund succeeds."""
    return BalanceOut(available=format_minor(await merchant_balance(merchant.id)))
