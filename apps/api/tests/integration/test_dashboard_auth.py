import httpx

from payforge.redis import redis

SIGNUP = {
    "email": "Kiran@Example.com",
    "password": "a long enough password",
    "merchant_name": "Kiran's Books",
}


def session_cookie(res: httpx.Response) -> str | None:
    for header in res.headers.get_list("set-cookie"):
        if header.startswith("pf_sid="):
            return header
    return None


async def test_signup_creates_merchant_logs_in_and_sets_a_safe_cookie(
    client: httpx.AsyncClient,
) -> None:
    res = await client.post("/auth/signup", json=SIGNUP)

    assert res.status_code == 201
    assert res.json()["user"]["email"] == "kiran@example.com"
    assert res.json()["user"]["role"] == "owner"
    cookie = session_cookie(res)
    assert cookie is not None
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie


async def test_me_then_logout_removes_the_session_from_redis(client: httpx.AsyncClient) -> None:
    await client.post("/auth/signup", json=SIGNUP)

    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["merchant"]["name"] == "Kiran's Books"
    assert len(me.json()["csrf_token"]) == 43
    assert len(await redis.keys("sess:*")) == 1

    res = await client.post("/auth/logout", headers={"X-CSRF-Token": me.json()["csrf_token"]})
    assert res.status_code == 204
    assert (await client.get("/auth/me")).status_code == 401
    assert await redis.keys("sess:*") == []


async def test_login_issues_a_new_session_id_each_time(client: httpx.AsyncClient) -> None:
    first = await client.post("/auth/signup", json=SIGNUP)
    second = await client.post(
        "/auth/login", json={"email": "kiran@example.com", "password": SIGNUP["password"]}
    )

    assert second.status_code == 204
    assert session_cookie(second) is not None
    assert session_cookie(second) != session_cookie(first)


async def test_wrong_password_and_unknown_email_get_the_same_answer(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    wrong = await client.post(
        "/auth/login", json={"email": "kiran@example.com", "password": "nope nope nope"}
    )
    unknown = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "nope nope nope"}
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_emails_are_case_insensitive(client: httpx.AsyncClient) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    again = await client.post("/auth/signup", json={**SIGNUP, "email": "KIRAN@example.com"})

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "email_taken"


async def test_short_password_is_422(client: httpx.AsyncClient) -> None:
    assert (
        await client.post("/auth/signup", json={**SIGNUP, "password": "short"})
    ).status_code == 422


async def test_made_up_session_cookie_is_401(client: httpx.AsyncClient) -> None:
    client.cookies.set("pf_sid", "x" * 43)
    assert (await client.get("/auth/me")).status_code == 401


# ── CSRF ──────────────────────────────────────────────────────────────────────


async def test_cookie_without_csrf_token_cannot_change_state(client: httpx.AsyncClient) -> None:
    await client.post("/auth/signup", json=SIGNUP)

    res = await client.post("/auth/logout")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "csrf_token_invalid"
    assert (await client.get("/auth/me")).status_code == 200  # still logged in


async def test_wrong_csrf_token_is_403(client: httpx.AsyncClient) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    assert (
        await client.post("/auth/logout", headers={"X-CSRF-Token": "x" * 43})
    ).status_code == 403


async def test_request_from_another_sites_origin_is_403_even_for_login(
    client: httpx.AsyncClient,
) -> None:
    res = await client.post("/auth/signup", json=SIGNUP, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "bad_origin"


async def test_our_own_dashboard_origin_is_accepted(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/auth/signup", json=SIGNUP, headers={"Origin": "http://localhost:3000"}
    )
    assert res.status_code == 201
