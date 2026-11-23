"""Reports: CSV exports of a merchant's payments, built in the background, kept in S3.

The `reports` table is also the job queue. A worker claims the oldest pending report
with FOR UPDATE SKIP LOCKED, so several workers never build the same one. Why not a
RabbitMQ queue here: the row is already the durable record of the job, and its status
is what the API shows. A second copy of the job in a broker would have to be kept in
sync with it, for a job that runs a few times a day. (Webhooks are different: high
volume, fan-out and delayed retries are exactly what a broker is good at.)

A worker that dies mid-report leaves it `running`; after STALE_RUNNING another worker
takes it again, up to MAX_ATTEMPTS times.
"""

import csv
import io
import tempfile
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import and_, or_, select, update

from payforge import storage
from payforge.db import SessionLocal
from payforge.domain.money import format_minor
from payforge.errors import NotFound, ValidationFailed
from payforge.ids import new_id
from payforge.logging import log
from payforge.models import Payment, Report, ReportStatus, utcnow

MAX_DAYS = 366
STALE_RUNNING = timedelta(minutes=10)
MAX_ATTEMPTS = 3
PAGE = 1000
COLUMNS = [
    "id",
    "created_at",
    "status",
    "method",
    "amount",
    "fee",
    "amount_refunded",
    "currency",
    "failure_code",
    "description",
    "customer_email",
]


async def request_report(merchant_id: str, start: date, end: date) -> Report:
    if end < start:
        raise ValidationFailed("end_date", "must not be before start_date")
    if (end - start).days >= MAX_DAYS:
        raise ValidationFailed("end_date", f"a report covers at most {MAX_DAYS} days")
    report = Report(
        id=new_id("rpt"),
        merchant_id=merchant_id,
        type="payments",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    async with SessionLocal.begin() as session:
        session.add(report)
    return report


async def get_report(merchant_id: str, report_id: str) -> Report:
    async with SessionLocal() as session:
        report = await session.scalar(
            select(Report).where(Report.id == report_id, Report.merchant_id == merchant_id)
        )
    if report is None:
        raise NotFound("Report")
    return report


def download_name(report: Report) -> str:
    return f"payforge-payments-{report.params['start_date']}-to-{report.params['end_date']}.csv"


# ── the worker side ───────────────────────────────────────────────────────────


async def claim_next() -> Report | None:
    """Takes the oldest report waiting to be built, if any.

    Two queries, each matching the (status, created_at) index exactly. With one query
    and an OR, MySQL scans the table, and FOR UPDATE then locks EVERY row it reads,
    so parallel workers find everything "locked" and skip it all. (The same lesson as
    the outbox relay: SKIP LOCKED only works well on an index-driven query.)
    """
    now = utcnow()
    async with SessionLocal.begin() as session:
        report_id = await session.scalar(
            select(Report.id)
            .where(Report.status == ReportStatus.PENDING)
            .order_by(Report.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if report_id is None:  # a worker died while building one?
            report_id = await session.scalar(
                select(Report.id)
                .where(
                    Report.status == ReportStatus.RUNNING,
                    Report.started_at < now - STALE_RUNNING,
                )
                .order_by(Report.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        if report_id is None:
            return None
        await session.execute(
            update(Report)
            .where(Report.id == report_id)
            .values(status=ReportStatus.RUNNING, started_at=now, attempts=Report.attempts + 1)
        )
        return await session.get_one(Report, report_id, populate_existing=True)


def _cell(value: str | None) -> str:
    """Text a customer typed could be a spreadsheet formula ("=HYPERLINK(…)"); a leading
    quote makes Excel / Sheets show it as text instead of running it (CSV injection)."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value or ""


async def build(report: Report) -> None:
    start = datetime.combine(date.fromisoformat(report.params["start_date"]), time(), UTC)
    end = datetime.combine(date.fromisoformat(report.params["end_date"]), time(), UTC)
    end += timedelta(days=1)  # the end date is included

    rows = 0
    # up to 5 MB in memory, then a temporary file on disk: memory stays flat
    with tempfile.SpooledTemporaryFile(max_size=5 * 1024 * 1024) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.writer(text)
        writer.writerow(COLUMNS)
        after: tuple[datetime, str] | None = None
        while True:  # page through with a keyset, like the API's cursor pagination
            conditions = [
                Payment.merchant_id == report.merchant_id,
                Payment.created_at >= start,
                Payment.created_at < end,
            ]
            if after is not None:
                conditions.append(
                    or_(
                        Payment.created_at > after[0],
                        and_(Payment.created_at == after[0], Payment.id > after[1]),
                    )
                )
            async with SessionLocal() as session:
                page = list(
                    await session.scalars(
                        select(Payment)
                        .where(*conditions)
                        .order_by(Payment.created_at, Payment.id)
                        .limit(PAGE)
                    )
                )
            for p in page:
                writer.writerow(
                    [
                        p.id,
                        p.created_at.isoformat(),
                        p.status,
                        p.method,
                        format_minor(p.amount_minor),
                        format_minor(p.fee_minor),
                        format_minor(p.amount_refunded_minor),
                        p.currency,
                        _cell(p.failure_code),
                        _cell(p.description),
                        _cell(p.customer_email),
                    ]
                )
            rows += len(page)
            if len(page) < PAGE:
                break
            after = (page[-1].created_at, page[-1].id)
        text.flush()
        text.detach()  # hand the bytes back to `raw` without closing it
        raw.seek(0)
        key = f"reports/{report.merchant_id}/{report.id}.csv"
        await storage.upload_file(key, raw, "text/csv")

    async with SessionLocal.begin() as session:
        await session.execute(
            update(Report)
            .where(Report.id == report.id, Report.attempts == report.attempts)  # still ours
            .values(
                status=ReportStatus.READY,
                file_key=key,
                row_count=rows,
                error=None,
                finished_at=utcnow(),
            )
        )


async def run_pending(limit: int = 5) -> int:
    """Builds up to `limit` reports. Returns how many were finished."""
    done = 0
    for _ in range(limit):
        report = await claim_next()
        if report is None:
            break
        try:
            await build(report)
            done += 1
        except Exception as exc:
            log.exception("report_failed", report_id=report.id, attempt=report.attempts)
            give_up = report.attempts >= MAX_ATTEMPTS
            async with SessionLocal.begin() as session:
                await session.execute(
                    update(Report)
                    .where(Report.id == report.id, Report.attempts == report.attempts)
                    .values(
                        status=ReportStatus.FAILED if give_up else ReportStatus.PENDING,
                        error=f"{type(exc).__name__}: {exc}"[:300],
                        finished_at=utcnow() if give_up else None,
                    )
                )
    return done
