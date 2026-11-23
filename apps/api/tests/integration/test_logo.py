import httpx
import pytest
from sqlalchemy import update

from payforge import storage
from payforge.db import SessionLocal
from payforge.models import User, UserRole

pytestmark = pytest.mark.usefixtures("s3")

SIGNUP = {
    "email": "kiran@example.com",
    "password": "a long enough password",
    "merchant_name": "Kiran's Books",
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # starts like every PNG file
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100


async def logged_in(client: httpx.AsyncClient, signup: dict[str, str] = SIGNUP) -> dict[str, str]:
    await client.post("/auth/signup", json=signup)
    return {"X-CSRF-Token": (await client.get("/auth/me")).json()["csrf_token"]}


async def upload(
    client: httpx.AsyncClient, headers: dict[str, str], data: bytes, content_type: str
) -> str:
    """What the dashboard page does: get a form, then POST the file straight to S3."""
    form = await client.post(
        "/dashboard/logo/upload", json={"content_type": content_type}, headers=headers
    )
    assert form.status_code == 200
    async with httpx.AsyncClient() as browser:
        res = await browser.post(
            form.json()["url"],
            data=form.json()["fields"],
            files={"file": ("logo", data, content_type)},
        )
    assert res.status_code == 204
    return str(form.json()["key"])


@pytest.mark.parametrize(("data", "content_type"), [(PNG, "image/png"), (JPEG, "image/jpeg")])
async def test_upload_then_set_shows_the_logo(
    client: httpx.AsyncClient, data: bytes, content_type: str
) -> None:
    headers = await logged_in(client)
    key = await upload(client, headers, data, content_type)

    res = await client.put("/dashboard/logo", json={"key": key}, headers=headers)

    assert res.status_code == 200
    me = (await client.get("/auth/me")).json()
    assert me["merchant"]["logo_url"] is not None
    async with httpx.AsyncClient() as browser:
        assert (await browser.get(me["merchant"]["logo_url"])).content == data


async def test_a_file_that_only_claims_to_be_a_png_is_refused_and_deleted(
    client: httpx.AsyncClient,
) -> None:
    headers = await logged_in(client)
    key = await upload(client, headers, b"<script>alert(1)</script>", "image/png")

    res = await client.put("/dashboard/logo", json={"key": key}, headers=headers)

    assert res.status_code == 422
    assert await storage.head(key) is None


async def test_a_new_logo_replaces_and_deletes_the_old_one(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    first = await upload(client, headers, PNG, "image/png")
    await client.put("/dashboard/logo", json={"key": first}, headers=headers)
    second = await upload(client, headers, JPEG, "image/jpeg")
    await client.put("/dashboard/logo", json={"key": second}, headers=headers)

    assert await storage.head(first) is None
    assert await storage.head(second) is not None


async def test_cannot_use_another_merchants_upload(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    key = await upload(client, headers, PNG, "image/png")
    await client.post("/auth/logout", headers=headers)
    other = await logged_in(client, {**SIGNUP, "email": "other@example.com"})

    res = await client.put("/dashboard/logo", json={"key": key}, headers=other)
    assert res.status_code == 422


async def test_only_owners_change_the_logo(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    async with SessionLocal.begin() as session:
        await session.execute(update(User).values(role=UserRole.DEVELOPER))

    res = await client.post(
        "/dashboard/logo/upload", json={"content_type": "image/png"}, headers=headers
    )
    assert res.status_code == 403


async def test_only_png_and_jpeg_forms_are_handed_out(client: httpx.AsyncClient) -> None:
    headers = await logged_in(client)
    res = await client.post(
        "/dashboard/logo/upload", json={"content_type": "image/svg+xml"}, headers=headers
    )
    assert res.status_code == 422  # SVG can carry scripts
