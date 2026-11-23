from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from payforge import storage
from payforge.api.deps import PaymentsRead
from payforge.models import Report, ReportStatus
from payforge.services import reports

router = APIRouter(tags=["Reports"])
DOWNLOAD_SECONDS = 300


class CreateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["payments"] = "payments"
    start_date: date = Field(examples=["2026-11-01"])
    end_date: date = Field(examples=["2026-11-30"], description="Included.")


class ReportOut(BaseModel):
    id: str
    object: Literal["report"] = "report"
    type: str
    status: ReportStatus
    start_date: date
    end_date: date
    row_count: int | None
    download_url: str | None = Field(
        description="When ready: a link to the CSV, valid for 5 minutes. GET again for a new one."
    )
    error: str | None
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, r: Report) -> "ReportOut":
        ready = r.status == ReportStatus.READY and r.file_key is not None
        return cls(
            id=r.id,
            type=r.type,
            status=r.status,
            start_date=date.fromisoformat(r.params["start_date"]),
            end_date=date.fromisoformat(r.params["end_date"]),
            row_count=r.row_count,
            download_url=storage.presigned_get(
                r.file_key, DOWNLOAD_SECONDS, reports.download_name(r)
            )
            if ready and r.file_key
            else None,
            error=r.error if r.status == ReportStatus.FAILED else None,
            created_at=r.created_at,
            finished_at=r.finished_at,
        )


@router.post("/reports", status_code=202)
async def create_report(body: CreateReport, merchant: PaymentsRead) -> ReportOut:
    """Starts building a CSV of your payments. **202**: poll GET /v1/reports/{id} until
    `status` is `ready`, then download from `download_url`."""
    return ReportOut.of(await reports.request_report(merchant.id, body.start_date, body.end_date))


@router.get("/reports/{report_id}")
async def get_report(report_id: str, merchant: PaymentsRead) -> ReportOut:
    return ReportOut.of(await reports.get_report(merchant.id, report_id))
