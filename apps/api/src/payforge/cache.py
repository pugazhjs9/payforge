"""Cache-aside on Redis.

Read: try Redis; on a miss, load from MySQL and store the result with a TTL.
Write: change MySQL first, COMMIT, then delete the cache key (never update it in place:
two writers updating the cache could leave the older value there).

The cache is only ever an optimisation. If Redis is slow or down, every call here
falls back to the database and logs a warning, so a cache outage costs speed, not uptime.
"""

import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import RedisError

from payforge.logging import log
from payforge.redis import redis


def jittered(ttl_seconds: int) -> int:
    """TTL ± 20%, so keys written at the same moment don't all expire at the same moment
    (which would send a burst of misses to the database together)."""
    return max(1, round(ttl_seconds * random.uniform(0.8, 1.2)))  # noqa: S311 (not security)


async def get_or_load(key: str, ttl_seconds: int, load: Callable[[], Awaitable[Any]]) -> Any:
    """The JSON value cached under `key`, or `await load()` (then cached)."""
    try:
        cached = await redis.get(key)
    except RedisError as exc:
        log.warning("cache_unavailable", key=key, error=str(exc))
        return await load()
    if cached is not None:
        return json.loads(cached)

    value = await load()
    try:
        await redis.set(key, json.dumps(value), ex=jittered(ttl_seconds))
    except RedisError as exc:
        log.warning("cache_unavailable", key=key, error=str(exc))
    return value


async def invalidate(*keys: str) -> None:
    """Call AFTER the database commit. If this fails, the stale value lives until its TTL."""
    try:
        await redis.delete(*keys)
    except RedisError as exc:
        log.warning("cache_invalidate_failed", keys=keys, error=str(exc))
