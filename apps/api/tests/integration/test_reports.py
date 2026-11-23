import asyncio
import csv
import io
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from payforge import storage
from payforge.db import SessionLocal
from payforge.models import Payment, PaymentStatus, Report, ReportStatus, utcnow
from payforge.services import reports

from .conftest import MERCHANT_A, MERCHANT_B, merchant

A = merchant(MERCHANT_A)
pytestmark = pytest.mark.usefixtures("s3")
NOV_10 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


async def seed(merchant_id: str, when: datetime, n: int = 1, description: str = "") -> None:
    async with SessionLocal.begin() as session:
        for i in range(n):
            session.add(
                Payment(
                    id=f"pay_{merchant_id}_{when:%m%d}_{i:04d}",
                    merchant_id=merchant_id,
                    amount_minor=10_000 + i,
                    currency="INR",
                    status=PaymentStatus.SUCCEEDED,
                    method="card",
                    method_token="tok_card_ok",
                    description=description or None,
                    created_at=when,
                )
            )


async def request(client: httpx.AsyncClient, start: str, end: str) -> httpx.Response:
    return await client.post("/v1/reports", json={"start_date": start, "end_date": end}, headers=A)


async def download(client: httpx.AsyncClient, report_id: str) -> list[dict[str, str]]:
    report = (await client.get(f"/v1/reports/{report_id}", headers=A)).json()
    assert report["status"] == "ready"
    async with httpx.AsyncClient() as http:
        res = await http.get(report["download_url"])
    return list(csv.DictReader(io.StringIO(res.text)))


async def test_report_is_built_in_the_background_and_downloaded_from_s3(
    client: httpx.AsyncClient,
) -> None:
    await seed(MERCHANT_A, NOV_10, n=3)
    await seed(MERCHANT_A, NOV_10 + timedelta(days=30))  # outside the range
    await seed(MERCHANT_B, NOV_10)  # someone else's

    created = await request(client, "2026-11-01", "2026-11-30")
    assert created.status_code == 202
    assert created.json()["status"] == "pending"
    assert created.json()["download_url"] is None

    assert await reports.run_pending() == 1  # what the worker does

    rows = await download(client, created.json()["id"])
    assert [r["id"] for r in rows] == [f"pay_mer_a_1110_{i:04d}" for i in range(3)]
    assert rows[0]["amount"] == "100.00"
    report = (await client.get(f"/v1/reports/{created.json()['id']}", headers=A)).json()
    assert report["row_count"] == 3


async def test_end_date_is_included(client: httpx.AsyncClient) -> None:
    await seed(MERCHANT_A, datetime(2026, 11, 30, 23, 59, tzinfo=UTC))
    created = await request(client, "2026-11-30", "2026-11-30")
    await reports.run_pending()

    assert len(await download(client, created.json()["id"])) == 1


async def test_formulas_typed_by_customers_are_escaped(client: httpx.AsyncClient) -> None:
    await seed(MERCHANT_A, NOV_10, description='=HYPERLINK("http://evil.example","Refund")')
    created = await request(client, "2026-11-01", "2026-11-30")
    await reports.run_pending()

    [row] = await download(client, created.json()["id"])
    assert row["description"].startswith("'=HYPERLINK")


async def test_large_report_pages_through_the_table(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reports, "PAGE", 7)
    await seed(MERCHANT_A, NOV_10, n=20)
    created = await request(client, "2026-11-01", "2026-11-30")
    await reports.run_pending()

    rows = await download(client, created.json()["id"])
    assert len({r["id"] for r in rows}) == 20


@pytest.mark.parametrize(
    ("start", "end", "field"),
    [("2026-11-30", "2026-11-01", "end_date"), ("2025-01-01", "2026-11-30", "end_date")],
)
async def test_invalid_ranges_are_422(
    client: httpx.AsyncClient, start: str, end: str, field: str
) -> None:
    res = await request(client, start, end)
    assert res.status_code == 422
    assert res.json()["error"]["details"][0]["field"] == field


async def test_another_merchants_report_is_404(client: httpx.AsyncClient) -> None:
    created = await request(client, "2026-11-01", "2026-11-30")
    res = await client.get(f"/v1/reports/{created.json()['id']}", headers=merchant(MERCHANT_B))
    assert res.status_code == 404


async def test_parallel_workers_build_each_report_once(client: httpx.AsyncClient) -> None:
    for _ in range(4):
        await request(client, "2026-11-01", "2026-11-30")

    done = await asyncio.gather(*(reports.run_pending(limit=1) for _ in range(6)))

    assert sum(done) == 4


async def test_a_failing_build_is_retried_then_marked_failed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def s3_down(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("S3 unreachable")

    monkeypatch.setattr(storage, "upload_file", s3_down)
    report_id = (await request(client, "2026-11-01", "2026-11-30")).json()["id"]

    statuses = []
    for _ in range(reports.MAX_ATTEMPTS):
        await reports.run_pending(limit=1)
        statuses.append((await client.get(f"/v1/reports/{report_id}", headers=A)).json())
    assert [s["status"] for s in statuses] == ["pending", "pending", "failed"]
    assert statuses[-1]["error"] == "ConnectionError: S3 unreachable"


async def test_a_report_left_running_by_a_dead_worker_is_taken_again() -> None:
    report = await reports.request_report(MERCHANT_A, NOV_10.date(), NOV_10.date())
    async with SessionLocal.begin() as session:
        await session.execute(
            update(Report).values(
                status=ReportStatus.RUNNING, started_at=utcnow() - timedelta(hours=1), attempts=1
            )
        )

    assert await reports.run_pending() == 1
    done = await reports.get_report(MERCHANT_A, report.id)
    assert (done.status, done.attempts) == (ReportStatus.READY, 2)
