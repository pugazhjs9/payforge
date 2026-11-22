"""Public API, version 1. Everything here is part of the contract with merchants."""

from fastapi import APIRouter, Depends

from payforge.api.deps import require_json
from payforge.api.v1 import balance, payments

router = APIRouter(prefix="/v1", dependencies=[Depends(require_json)])
router.include_router(payments.router)
router.include_router(balance.router)
