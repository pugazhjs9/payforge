from fastapi import APIRouter

router = APIRouter(tags=["Operations"])


@router.get("/healthz", summary="Liveness check")
async def healthz() -> dict[str, str]:
    """ "The process is running." It deliberately checks nothing else, or a short
    database blip would make every server look dead."""
    return {"status": "ok"}
