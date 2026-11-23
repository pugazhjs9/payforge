import io

import httpx
import pytest

from payforge import storage
from payforge.config import settings
from payforge.ids import new_id

pytestmark = pytest.mark.usefixtures("s3")


async def test_upload_head_read_and_presigned_download() -> None:
    key = f"test/{new_id('f')}.csv"
    await storage.upload_file(key, io.BytesIO(b"id,amount\n1,10.00\n"), "text/csv")

    info = await storage.head(key)
    assert info is not None
    assert (info.size, info.content_type) == (18, "text/csv")
    assert await storage.read_start(key, 2) == b"id"
    async with httpx.AsyncClient() as http:
        url = storage.presigned_get(key, filename="report.csv")
        res = await http.get(url)
        assert res.content == b"id,amount\n1,10.00\n"
        assert "report.csv" in res.headers["content-disposition"]
        assert (await http.get(url.replace(key, "test/other.csv"))).status_code == 403
        bare = f"{settings.s3_endpoint_url}/{settings.s3_bucket}/{key}"
        assert (await http.get(bare)).status_code == 403  # the bucket is private


async def test_missing_object_is_none() -> None:
    assert await storage.head("test/does-not-exist") is None


async def test_presigned_post_enforces_size_and_type() -> None:
    post = storage.presigned_post(f"test/{new_id('f')}.png", "image/png", max_bytes=100)
    async with httpx.AsyncClient() as http:

        async def upload(data: bytes, content_type: str = "image/png") -> int:
            fields = {**post["fields"], "Content-Type": content_type}
            files = {"file": ("logo.png", data, content_type)}
            return (await http.post(post["url"], data=fields, files=files)).status_code

        assert await upload(b"x" * 50) == 204
        assert await upload(b"x" * 101) == 400  # bigger than the signed limit
        assert await upload(b"x" * 50, "text/html") == 403  # not the signed type


async def test_browser_uploads_from_the_dashboard_pass_cors() -> None:
    async with httpx.AsyncClient() as http:
        res = await http.options(
            f"{settings.s3_endpoint_url}/{settings.s3_bucket}",
            headers={
                "Origin": settings.dashboard_origin,
                "Access-Control-Request-Method": "POST",
            },
        )
    assert res.headers.get("access-control-allow-origin") in (settings.dashboard_origin, "*")
