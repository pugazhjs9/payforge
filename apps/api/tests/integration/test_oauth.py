import httpx
import jwt
import pytest

from payforge.services.api_keys import create_api_key

from .conftest import MERCHANT_A, pay, with_key

pytestmark = pytest.mark.usefixtures("bank")


async def new_key(scopes: list[str] | None = None) -> tuple[str, str]:
    created = await create_api_key(
        MERCHANT_A, "server", scopes or ["payments:read", "payments:write"]
    )
    return created.key.id, created.secret


async def get_token(client: httpx.AsyncClient, key_id: str, secret: str) -> httpx.Response:
    return await client.post(
        "/v1/auth/token", data={"grant_type": "client_credentials"}, auth=(key_id, secret)
    )


async def test_client_credentials_gives_a_working_15_minute_token(
    client: httpx.AsyncClient,
) -> None:
    res = await get_token(client, *await new_key())

    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    body = res.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    assert body["scope"] == "payments:read payments:write"

    balance = await client.get(
        "/v1/balance", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert balance.status_code == 200


async def test_wrong_secret_uses_the_oauth_error_format(client: httpx.AsyncClient) -> None:
    key_id, _ = await new_key()
    res = await get_token(client, key_id, "pf_sk_test_wrong")

    assert res.status_code == 401
    assert res.json()["error"] == "invalid_client"
    assert res.headers["www-authenticate"] == "Basic"


async def test_grant_type_errors(client: httpx.AsyncClient) -> None:
    key_id, secret = await new_key()
    wrong = await client.post(
        "/v1/auth/token", data={"grant_type": "password"}, auth=(key_id, secret)
    )
    missing = await client.post("/v1/auth/token", auth=(key_id, secret))

    assert wrong.status_code == 400
    assert wrong.json()["error"] == "unsupported_grant_type"
    assert missing.json()["error"] == "invalid_request"


async def test_revoking_a_key_kills_its_tokens_immediately(client: httpx.AsyncClient) -> None:
    from payforge.services.api_keys import revoke_api_key

    key_id, secret = await new_key()
    token = (await get_token(client, key_id, secret)).json()["access_token"]
    await revoke_api_key(MERCHANT_A, key_id)

    res = await client.get("/v1/balance", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "key_revoked"
    assert (await get_token(client, key_id, secret)).status_code == 401


async def test_a_read_only_key_cannot_create_payments(client: httpx.AsyncClient) -> None:
    token = (await get_token(client, *await new_key(["payments:read"]))).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = await client.post("/v1/payments", json=pay("10.00"), headers=with_key(headers))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "insufficient_scope"
    assert (await client.get("/v1/payments", headers=headers)).status_code == 200


async def test_jwks_publishes_the_key_that_verifies_tokens(client: httpx.AsyncClient) -> None:
    token = (await get_token(client, *await new_key())).json()["access_token"]
    keys = (await client.get("/.well-known/jwks.json")).json()["keys"]

    assert len(keys) == 1
    assert keys[0]["kty"] == "EC" and keys[0]["crv"] == "P-256" and keys[0]["alg"] == "ES256"
    public_key = jwt.PyJWK(keys[0]).key
    claims = jwt.decode(token, public_key, algorithms=["ES256"], audience="payforge-api")
    assert claims["sub"] == MERCHANT_A
