import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from payforge import cache
from payforge.redis import redis


class Loader:
    """Counts how often the "database" is asked."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    async def __call__(self) -> object:
        self.calls += 1
        return self.value


async def test_second_read_comes_from_redis() -> None:
    load = Loader({"available": 4_800})

    assert await cache.get_or_load("cache:test", 30, load) == {"available": 4_800}
    assert await cache.get_or_load("cache:test", 30, load) == {"available": 4_800}
    assert load.calls == 1
    assert 24 <= await redis.ttl("cache:test") <= 36


async def test_invalidate_forces_a_fresh_load() -> None:
    load = Loader(1)
    await cache.get_or_load("cache:test", 30, load)
    await cache.invalidate("cache:test")
    await cache.get_or_load("cache:test", 30, load)

    assert load.calls == 2


def test_ttl_jitter_stays_within_20_percent() -> None:
    ttls = {cache.jittered(100) for _ in range(500)}
    assert min(ttls) >= 80
    assert max(ttls) <= 120
    assert len(ttls) > 10  # actually spread out


async def test_redis_down_falls_back_to_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(*_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(redis, "get", down)
    monkeypatch.setattr(redis, "set", down)
    monkeypatch.setattr(redis, "delete", down)
    load = Loader(7)

    assert await cache.get_or_load("cache:test", 30, load) == 7
    await cache.invalidate("cache:test")  # logs, does not raise
    assert load.calls == 1
