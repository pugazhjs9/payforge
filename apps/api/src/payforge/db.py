from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from payforge.config import settings

engine = create_async_engine(
    settings.database_url,
    # READ COMMITTED: each statement sees the latest committed data, and InnoDB takes
    # fewer gap locks than with the default REPEATABLE READ (see docs/decisions/0001).
    isolation_level="READ COMMITTED",
    pool_size=10,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={"init_command": "SET time_zone = '+00:00'"},  # NOW() is always UTC
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

__all__ = ["AsyncSession", "SessionLocal", "engine"]
