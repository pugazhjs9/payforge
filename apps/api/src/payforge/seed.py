"""Seed two fictional merchants:  uv run python -m payforge.seed"""

import asyncio

from sqlalchemy.dialects.mysql import insert

from payforge.db import SessionLocal, engine
from payforge.models import Merchant

MERCHANTS = [("mer_kiranbooks", "Kiran's Books"), ("mer_chaicorner", "Chai Corner")]


async def main() -> None:
    async with SessionLocal.begin() as session:
        for merchant_id, name in MERCHANTS:
            stmt = insert(Merchant).values(id=merchant_id, name=name)
            await session.execute(stmt.on_duplicate_key_update(name=stmt.inserted.name))
    await engine.dispose()
    print(f"seeded {len(MERCHANTS)} merchants")


if __name__ == "__main__":
    asyncio.run(main())
