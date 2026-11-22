from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from payforge.api import auth, health, v1
from payforge.clients import simbank
from payforge.config import settings
from payforge.db import engine
from payforge.errors import install_error_handlers
from payforge.logging import configure_logging
from payforge.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from payforge.redis import redis

DESCRIPTION = """
A multi-merchant payment gateway API (simulated bank, no real money).

- JSON only; field names in `snake_case`; times in ISO 8601 UTC.
- Every response has an `X-Request-Id` header. Quote it when reporting a problem.
- Every error has the same shape: `{"error": {"code", "message", "details", "request_id"}}`.
- Clients must ignore fields they don't recognise (adding fields is not a breaking change).
"""


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    # graceful shutdown: close outgoing connections and the database pool
    await simbank.close()
    await redis.aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    configure_logging(settings.log_level, json=settings.environment != "development")
    app = FastAPI(title="PayForge API", version="1.0", description=DESCRIPTION, lifespan=lifespan)
    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(v1.router)
    app.include_router(auth.router)

    # Starlette runs the LAST added middleware FIRST: request context is outermost.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=100_000)
    app.add_middleware(RequestContextMiddleware)
    return app


app = create_app()
