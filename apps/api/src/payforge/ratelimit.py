"""Rate limiting with Redis, shared by every API instance.

Two algorithms, each where it fits:

- Token bucket (API traffic): a bucket holds up to `burst` tokens and refills at
  `rate` tokens per second; each request takes one. Short bursts are fine, the
  long-run average is capped. It runs as ONE Lua script, so read-refill-take-write
  is atomic even with many API instances, and it uses Redis's clock, not ours.
- Fixed window counter (login attempts): INCR a counter that expires after the
  window. Simple and exact enough for "10 attempts per 15 minutes".
"""

import math
from dataclasses import dataclass

from payforge.redis import redis

BUCKET_SCRIPT = """
-- KEYS[1] = bucket; ARGV[1] = refill rate (tokens/second); ARGV[2] = capacity (burst)
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])
if tokens == nil then
  tokens = capacity  -- a new (or expired, i.e. refilled) bucket starts full
  ts = now
end

-- refill for the time that passed, never above capacity
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate / 1000)

local allowed = 0
local retry_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_ms = math.ceil((1 - tokens) * 1000 / rate)
end

-- the key expires when the bucket would be full again: then "missing" == "full"
local full_ms = math.max(1, math.ceil((capacity - tokens) * 1000 / rate))
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], full_ms)
return {allowed, math.floor(tokens), retry_ms, full_ms}
"""

_token_bucket = redis.register_script(BUCKET_SCRIPT)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int  # until the limit is fully available again
    retry_after_seconds: int  # 0 when allowed

    def headers(self) -> dict[str, str]:
        """RateLimit-* (IETF httpapi draft) on every response; Retry-After (RFC 9110) on 429."""
        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.reset_seconds),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


async def take_token(key: str, *, rate_per_second: float, burst: int) -> Decision:
    allowed, remaining, retry_ms, full_ms = await _token_bucket(
        keys=[key], args=[rate_per_second, burst]
    )
    return Decision(
        allowed=bool(allowed),
        limit=burst,
        remaining=int(remaining),
        reset_seconds=math.ceil(int(full_ms) / 1000),
        retry_after_seconds=math.ceil(int(retry_ms) / 1000),
    )


async def count_attempt(key: str, *, limit: int, window_seconds: int) -> Decision:
    """Counts one attempt in the current window. MULTI/EXEC makes INCR + EXPIRE atomic;
    NX sets the expiry only on the first attempt, so the window doesn't keep sliding."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        pipe.ttl(key)
        count, _, ttl = await pipe.execute()
    left = max(int(ttl), 1)
    allowed = int(count) <= limit
    return Decision(
        allowed=allowed,
        limit=limit,
        remaining=max(limit - int(count), 0),
        reset_seconds=left,
        retry_after_seconds=0 if allowed else left,
    )
