import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from payforge import ratelimit
from payforge.config import settings
from payforge.main import create_app

SIGNUP = {
    "email": "kiran@example.com",
    "password": "a long enough password",
    "merchant_name": "Kiran's Books",
}
WRONG = {"email": "kiran@example.com", "password": "not the password"}
RIGHT = {"email": "kiran@example.com", "password": SIGNUP["password"]}


async def statuses(client: httpx.AsyncClient, body: dict[str, str], times: int) -> list[int]:
    return [(await client.post("/auth/login", json=body)).status_code for _ in range(times)]


async def test_eleventh_attempt_is_429_even_with_the_right_password(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/auth/signup", json=SIGNUP)

    assert await statuses(client, WRONG, 10) == [401] * 10
    res = await client.post("/auth/login", json=RIGHT)
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "too_many_attempts"
    assert 890 <= int(res.headers["retry-after"]) <= 900


async def test_a_successful_login_clears_the_failed_attempts(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_attempts_per_account", 3)
    await client.post("/auth/signup", json=SIGNUP)

    assert await statuses(client, WRONG, 2) == [401, 401]
    assert await statuses(client, RIGHT, 1) == [204]
    assert await statuses(client, WRONG, 2) == [401, 401]
    assert await statuses(client, RIGHT, 1) == [204]  # the 6th attempt, but counted from 0


async def test_one_ip_trying_many_accounts_is_stopped(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_attempts_per_ip", 3)
    results = [
        (
            await client.post("/auth/login", json={"email": f"u{i}@example.com", "password": "x"})
        ).status_code
        for i in range(4)
    ]

    assert results == [401, 401, 401, 429]


async def test_another_ip_is_not_affected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "login_attempts_per_ip", 1)
    app = create_app()

    async def attempt(ip: str) -> int:
        transport = httpx.ASGITransport(app=app, client=(ip, 5000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return (await c.post("/auth/login", json=WRONG)).status_code

    assert [await attempt("10.0.0.1") for _ in range(2)] == [401, 429]
    assert await attempt("10.0.0.2") == 401


async def test_signups_are_limited_per_ip(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "signups_per_ip", 2)
    results = [
        (
            await client.post("/auth/signup", json={**SIGNUP, "email": f"s{i}@example.com"})
        ).status_code
        for i in range(3)
    ]

    assert results == [201, 201, 429]


async def test_login_fails_closed_when_redis_is_down(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(ratelimit, "count_attempt", down)
    res = await client.post("/auth/login", json=RIGHT)

    assert res.status_code == 503
    assert res.json()["error"]["code"] == "temporarily_unavailable"
    assert res.headers["retry-after"] == "30"
