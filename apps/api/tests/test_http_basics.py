import re

import httpx


async def test_healthz_returns_ok_and_a_request_id(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz")

    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    assert re.fullmatch(r"req_[0-9A-Z]{26}", res.headers["x-request-id"])


async def test_reuses_a_safe_client_request_id(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz", headers={"X-Request-Id": "client-abc_123"})
    assert res.headers["x-request-id"] == "client-abc_123"


async def test_replaces_an_unsafe_client_request_id(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz", headers={"X-Request-Id": "bad id {with} spaces"})
    assert res.headers["x-request-id"].startswith("req_")


async def test_unknown_route_uses_the_standard_error_shape(client: httpx.AsyncClient) -> None:
    res = await client.get("/nothing-here")

    assert res.status_code == 404
    assert res.json() == {
        "error": {
            "code": "route_not_found",
            "message": "No route for GET /nothing-here.",
            "request_id": res.headers["x-request-id"],
        }
    }


async def test_body_over_100kb_is_refused_with_413(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/anything", content=b"x" * 200_000, headers={"content-type": "application/json"}
    )

    assert res.status_code == 413
    assert res.json()["error"]["code"] == "body_too_large"
