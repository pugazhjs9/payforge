import asyncio

from payforge import ratelimit
from payforge.redis import redis

SLOW = 0.001  # tokens per second: effectively no refill during a test


async def test_bucket_allows_the_burst_then_refuses() -> None:
    decisions = [
        await ratelimit.take_token("rl:t", rate_per_second=SLOW, burst=3) for _ in range(4)
    ]

    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert [d.remaining for d in decisions] == [2, 1, 0, 0]
    assert decisions[-1].retry_after_seconds >= 1
    assert decisions[-1].headers()["Retry-After"] == str(decisions[-1].retry_after_seconds)


async def test_bucket_refills_over_time() -> None:
    assert (await ratelimit.take_token("rl:t", rate_per_second=20, burst=1)).allowed
    assert not (await ratelimit.take_token("rl:t", rate_per_second=20, burst=1)).allowed
    await asyncio.sleep(0.06)  # 20/s = one token every 50 ms
    assert (await ratelimit.take_token("rl:t", rate_per_second=20, burst=1)).allowed


async def test_parallel_requests_never_get_more_than_the_burst() -> None:
    """The Lua script is atomic: no two requests can both take the last token."""
    decisions = await asyncio.gather(
        *(ratelimit.take_token("rl:t", rate_per_second=SLOW, burst=10) for _ in range(50))
    )
    assert sum(d.allowed for d in decisions) == 10


async def test_buckets_are_independent_and_expire_when_full() -> None:
    await ratelimit.take_token("rl:a", rate_per_second=SLOW, burst=1)

    assert (await ratelimit.take_token("rl:b", rate_per_second=SLOW, burst=1)).allowed
    full = await ratelimit.take_token("rl:c", rate_per_second=100, burst=5)
    assert 0 < await redis.pttl("rl:c") <= 10  # 1 token at 100/s = full again in 10 ms
    assert full.reset_seconds == 1


async def test_fixed_window_counts_attempts_and_keeps_its_expiry() -> None:
    first = await ratelimit.count_attempt("rl:w", limit=2, window_seconds=900)
    await ratelimit.count_attempt("rl:w", limit=2, window_seconds=900)
    third = await ratelimit.count_attempt("rl:w", limit=2, window_seconds=900)

    assert first.allowed
    assert first.remaining == 1
    assert not third.allowed
    assert 890 <= third.retry_after_seconds <= 900
    assert 890 <= await redis.ttl("rl:w") <= 900  # not pushed back by later attempts
