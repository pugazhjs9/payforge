import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from payforge import ratelimit
from payforge.auth.tokens import issue_access_token
from payforge.config import settings

from .conftest import ALL_SCOPES, MERCHANT_A, MERCHANT_B, merchant

A = merchant(MERCHANT_A)


@pytest.fixture
def burst_of_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_burst", 3)
    monkeypatch.setattr(settings, "rate_limit_per_second", 0.001)  # no refill during the test


async def test_responses_carry_rate_limit_headers(client: httpx.AsyncClient) -> None:
    res = await client.get("/v1/balance", headers=A)

    assert res.status_code == 200
    assert res.headers["ratelimit-limit"] == str(settings.rate_limit_burst)
    assert res.headers["ratelimit-remaining"] == str(settings.rate_limit_burst - 1)
    assert int(res.headers["ratelimit-reset"]) >= 1


@pytest.mark.usefixtures("burst_of_3")
async def test_over_the_limit_is_429_with_retry_after(client: httpx.AsyncClient) -> None:
    statuses = [(await client.get("/v1/balance", headers=A)).status_code for _ in range(3)]
    res = await client.get("/v1/balance", headers=A)

    assert statuses == [200, 200, 200]
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "rate_limited"
    assert res.json()["error"]["request_id"] == res.headers["x-request-id"]
    assert int(res.headers["retry-after"]) >= 1
    assert res.headers["ratelimit-remaining"] == "0"


@pytest.mark.usefixtures("burst_of_3")
async def test_each_merchant_has_its_own_bucket(client: httpx.AsyncClient) -> None:
    for _ in range(4):
        await client.get("/v1/balance", headers=A)

    assert (await client.get("/v1/balance", headers=merchant(MERCHANT_B))).status_code == 200


@pytest.mark.usefixtures("burst_of_3")
async def test_a_second_api_key_does_not_double_the_quota(client: httpx.AsyncClient) -> None:
    other_key = issue_access_token(merchant_id=MERCHANT_A, key_id="key_two", scopes=ALL_SCOPES)
    for _ in range(3):
        await client.get("/v1/balance", headers=A)

    res = await client.get("/v1/balance", headers={"Authorization": f"Bearer {other_key}"})
    assert res.status_code == 429


async def test_limiter_fails_open_when_redis_is_down(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(**_kwargs: object) -> None:
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(ratelimit, "_token_bucket", down)
    res = await client.get("/v1/balance", headers=A)

    assert res.status_code == 200
    assert "ratelimit-limit" not in res.headers
