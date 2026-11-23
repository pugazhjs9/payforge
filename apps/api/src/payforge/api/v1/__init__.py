"""Public API, version 1. Everything here is part of the contract with merchants."""

from fastapi import APIRouter, Depends

from payforge.api.deps import rate_limit_merchant, require_json
from payforge.api.v1 import balance, payments

# Rate limiting needs the merchant, so it runs after authentication; FastAPI caches
# require_access_token per request, so the token is verified only once.
router = APIRouter(prefix="/v1", dependencies=[Depends(require_json), Depends(rate_limit_merchant)])
router.include_router(payments.router)
router.include_router(balance.router)
