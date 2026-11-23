"""Object storage through the S3 API.

Locally and in CI this is SeaweedFS, an S3-compatible server; in production it would
be AWS S3. Only the endpoint and credentials change.

Big files don't pass through the API process:
- uploads: the browser POSTs straight to S3 with a PRESIGNED POST policy; S3 itself
  enforces the size limit and content type we signed;
- downloads: the API hands out a PRESIGNED GET URL that works for a few minutes.
The bucket stays private: without a valid signature, every request is refused.

boto3 is synchronous, so its network calls run in a worker thread (asyncio.to_thread).
Creating a presigned URL is local signing, no network, so it runs directly.
"""

import asyncio
from dataclasses import dataclass
from functools import cache
from typing import IO, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from mypy_boto3_s3 import S3Client

from payforge.config import settings
from payforge.logging import log


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str


@cache
def _s3() -> S3Client:
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,  # None = real AWS
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


async def ensure_bucket() -> None:
    """Creates the bucket if needed (local development and tests)."""

    def run() -> None:
        try:
            _s3().head_bucket(Bucket=settings.s3_bucket)
        except ClientError:
            _s3().create_bucket(Bucket=settings.s3_bucket)
        _s3().put_bucket_cors(  # the dashboard uploads logos from the browser
            Bucket=settings.s3_bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": [settings.dashboard_origin],
                        "AllowedMethods": ["POST", "GET"],
                        "AllowedHeaders": ["*"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )

    await asyncio.to_thread(run)


async def ensure_bucket_outside_production() -> None:
    """Local development convenience. In production the bucket (and its CORS rules)
    belongs to the infrastructure code, not to the app."""
    if settings.environment == "production":
        return
    try:
        await ensure_bucket()
    except Exception:  # S3 down: the app still starts; uploads fail until it's back
        log.exception("s3_bucket_setup_failed")


async def upload_file(key: str, file: IO[bytes], content_type: str) -> None:
    """Uploads a file-like object. boto3 switches to a MULTIPART upload above 8 MB:
    parts are sent in parallel, and a failed part is retried alone."""
    await asyncio.to_thread(
        _s3().upload_fileobj,
        file,
        settings.s3_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )


async def head(key: str) -> ObjectInfo | None:
    def run() -> ObjectInfo | None:
        try:
            meta = _s3().head_object(Bucket=settings.s3_bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(size=meta["ContentLength"], content_type=meta.get("ContentType", ""))

    return await asyncio.to_thread(run)


async def read_start(key: str, length: int) -> bytes:
    """The first `length` bytes (an HTTP Range request), e.g. to check a file's type."""

    def run() -> bytes:
        res = _s3().get_object(Bucket=settings.s3_bucket, Key=key, Range=f"bytes=0-{length - 1}")
        return res["Body"].read()

    return await asyncio.to_thread(run)


async def delete(key: str) -> None:
    await asyncio.to_thread(_s3().delete_object, Bucket=settings.s3_bucket, Key=key)


def presigned_get(key: str, expires_seconds: int = 300, filename: str | None = None) -> str:
    params: dict[str, Any] = {"Bucket": settings.s3_bucket, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return _s3().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_seconds)


def presigned_post(
    key: str, content_type: str, max_bytes: int, expires_seconds: int = 300
) -> dict[str, Any]:
    """{"url", "fields"}: the browser POSTs the fields plus the file to the url. The
    signed policy pins the key and content type and caps the size."""
    return dict(
        _s3().generate_presigned_post(
            settings.s3_bucket,
            key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
            ],
            ExpiresIn=expires_seconds,
        )
    )
