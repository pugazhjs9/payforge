from fastapi import APIRouter

from payforge.api.deps import PaymentsRead
from payforge.api.v1.schemas import BalanceOut
from payforge.domain.money import format_minor
from payforge.services.ledger import get_balance, merchant_account

router = APIRouter()


@router.get("/balance", tags=["Balance"])
async def balance(merchant: PaymentsRead) -> BalanceOut:
    """What PayForge owes the merchant, computed from the double-entry ledger."""
    return BalanceOut(available=format_minor(await get_balance(merchant_account(merchant.id))))
