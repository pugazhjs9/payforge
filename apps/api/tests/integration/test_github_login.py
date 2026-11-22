import re
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import func, select

from payforge.db import SessionLocal
from payforge.models import User

from .fake_github import FakeGithub

SIGNUP = {
    "email": "kiran@example.com",
    "password": "a long enough password",
    "merchant_name": "Kiran's Books",
}


async def login_with_github(client: httpx.AsyncClient, github: FakeGithub) -> httpx.Response:
    """The whole browser dance; returns the callback response."""
    start = await client.get("/auth/github")
    assert start.status_code == 302
    code, state = github.approve(start.headers["location"])
    return await client.get("/auth/github/callback", params={"code": code, "state": state})


async def users() -> list[User]:
    async with SessionLocal() as session:
        return list(await session.scalars(select(User)))


async def test_redirects_to_github_with_state_and_a_pkce_s256_challenge(
    client: httpx.AsyncClient,
) -> None:
    res = await client.get("/auth/github")
    url = urlparse(res.headers["location"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}

    assert f"{url.scheme}://{url.netloc}{url.path}" == "http://127.0.0.1:4998/login/oauth/authorize"
    assert query["client_id"] == "test-client"
    assert re.fullmatch(r"[\w-]{22}", query["state"])
    assert re.fullmatch(r"[\w-]{43}", query["code_challenge"])
    assert query["code_challenge_method"] == "S256"


async def test_first_login_creates_a_merchant_and_an_owner(
    client: httpx.AsyncClient, github: FakeGithub
) -> None:
    callback = await login_with_github(client, github)

    assert callback.status_code == 302
    assert callback.headers["location"] == "/app/"
    me = (await client.get("/auth/me")).json()
    assert me["user"]["email"] == "kiran@example.com"
    assert me["user"]["role"] == "owner"
    assert me["merchant"]["name"] == "kiran-dev's shop"


async def test_logging_in_again_finds_the_same_user(
    client: httpx.AsyncClient, github: FakeGithub
) -> None:
    await login_with_github(client, github)
    await login_with_github(client, github)
    assert len(await users()) == 1


async def test_links_an_existing_account_through_a_verified_email(
    client: httpx.AsyncClient, github: FakeGithub
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    await login_with_github(client, github)

    [user] = await users()
    assert user.github_id == "4242"


async def test_does_not_link_through_an_unverified_email(
    client: httpx.AsyncClient, github: FakeGithub
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    github.profile = {"id": 777, "login": "attacker", "name": None}
    github.emails = [{"email": "kiran@example.com", "primary": True, "verified": False}]
    await login_with_github(client, github)

    async with SessionLocal() as session:
        victim = await session.scalar(select(User).where(User.email == "kiran@example.com"))
        count = await session.scalar(select(func.count()).select_from(User))
    assert victim is not None and victim.github_id is None
    assert count == 2  # the attacker got their own, separate account


async def test_made_up_state_is_400_and_each_state_works_once(
    client: httpx.AsyncClient, github: FakeGithub
) -> None:
    bogus = await client.get("/auth/github/callback", params={"code": "abc", "state": "made-up"})
    assert bogus.status_code == 400
    assert bogus.json()["error"]["code"] == "invalid_oauth_state"

    start = await client.get("/auth/github")
    code, state = github.approve(start.headers["location"])
    params = {"code": code, "state": state}
    assert (await client.get("/auth/github/callback", params=params)).status_code == 302
    assert (await client.get("/auth/github/callback", params=params)).status_code == 400


async def test_cancel_on_github_goes_back_to_the_login_page(client: httpx.AsyncClient) -> None:
    res = await client.get("/auth/github/callback", params={"error": "access_denied", "state": "x"})
    assert res.status_code == 302
    assert res.headers["location"] == "/app/login.html?error=github_cancelled"
