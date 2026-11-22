"""One shared Redis client (sessions, cache, rate limits).

Short socket timeouts: while Redis is down, commands fail fast instead of hanging,
so each feature can decide how to degrade.
"""

from redis.asyncio import Redis

from payforge.config import settings

redis: "Redis" = Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_timeout=1.0,
    socket_connect_timeout=1.0,
)
