# 0016. Files in S3 with presigned URLs; SeaweedFS locally

**Status:** accepted

## Context
Two kinds of files: merchant logos (uploaded from the browser) and CSV reports (made by the worker,
downloaded by merchants).

## Decision
- **The API process never streams file bodies.** The browser uploads with a **presigned POST**: a
  signed policy that S3 itself enforces. It pins one key and one content type and caps the size
  (512 KB). Downloads are **presigned GET** URLs valid for 5 minutes. The bucket is private.
- **Don't trust the declared type.** After the upload, the API reads the first bytes and checks the
  PNG / JPEG magic number; anything else is deleted. SVG is not accepted: it can contain scripts.
- **Reports** are written to a spooled temporary file (in memory up to 5 MB, then disk), paging
  through payments with a keyset cursor, and uploaded with `upload_fileobj`. That switches to a
  multipart upload for big files, so parts upload in parallel and are retried one by one. Cells a
  customer could type (description, email) are escaped against **CSV injection** (`=HYPERLINK(…)`).
- **boto3 calls run in a worker thread** (`asyncio.to_thread`); signing URLs is local and runs inline.
- **Locally and in CI: SeaweedFS** (`chrislusf/seaweedfs:4.54`), an S3-compatible server. The tests
  check that it really enforces signatures, policy size and type limits, and CORS.

## Alternatives considered
- **Uploading through the API.** Simple, but every byte goes through our process, memory and
  bandwidth, and slow clients tie up workers.
- **MinIO.** The usual local S3. Its community Docker images are no longer published, so the image
  can't be pulled. Code written against the S3 API doesn't care which server answers.
- **moto (an AWS mock).** Great for unit tests, but it's a mock: we want to see a real server reject
  an oversized upload.
- **aioboto3.** Native async, but a heavier dependency for a handful of calls.
