import httpx
from sqlalchemy import update

from payforge.db import SessionLocal
from payforge.models import User, UserRole

SIGNUP = {
    "email": "kiran@example.com",
    "password": "a long enough password",
    "merchant_name": "Kiran's Books",
}


async def logged_in(client: httpx.AsyncClient, signup: dict[str, str] = SIGNUP) -> dict[str, str]:
    """Sign up and return the headers a dashboard page sends on writes."""
    await client.post("/auth/signup", json=signup)
    csrf = (await client.get("/auth/me")).json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


async def test_create_shows_the_secret_once_and_lists_never_show_it(
    client: httpx.AsyncClient,
) -> None:
    headers = await logged_in(client)
    created = await client.post("/dashboard/api-keys", json={"name": "web shop"}, headers=headers)

    assert created.status_code == 201
    body = created.json()
    assert body["id"].startswith("key_")
    assert body["secret"].startswith("pf_sk_test_")
    assert body["secret_last4"] == body["secret"][-4:]
    assert body["scopes"] == ["payments:read", "payments:write", "refunds:write"]

    listed = (await client.get("/dashboard/api-keys")).json()["data"]
    assert [k["id"] for k in listed] == [body["id"]]
    assert "secret" not in listed[0]


async def test_creating_a_key_needs_the_csrf_token(client: httpx.AsyncClient) -> None:
    await logged_in(client)
    res = await client.post("/dashboard/api-keys", json={"name": "web shop"})
    assert res.status_code == 403


async def test_revoke_and_other_merchants_keys_are_404(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    key = (await client.post("/dashboard/api-keys", json={"name": "a"}, headers=headers)).json()

    assert (
        await client.delete(f"/dashboard/api-keys/{key['id']}", headers=headers)
    ).status_code == 204
    assert (await client.get("/dashboard/api-keys")).json()["data"][0]["revoked_at"] is not None

    other = httpx.AsyncClient(
        transport=client._transport, base_url="http://test"
    )  # a second browser
    async with other:
        other_headers = await logged_in(other, {**SIGNUP, "email": "chai@example.com"})
        assert (
            await other.delete(f"/dashboard/api-keys/{key['id']}", headers=other_headers)
        ).status_code == 404


async def test_a_viewer_cannot_create_keys(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    async with SessionLocal.begin() as session:
        await session.execute(update(User).values(role=UserRole.VIEWER))

    res = await client.post("/dashboard/api-keys", json={"name": "a"}, headers=headers)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "insufficient_role"
