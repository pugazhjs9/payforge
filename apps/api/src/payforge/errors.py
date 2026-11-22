"""One error format for every error the API returns.

    {"error": {"code": "...", "message": "...", "details": [...], "request_id": "..."}}

`code` is for programs (stable), `message` is for humans (may change),
`request_id` lets a merchant point us at every log line of their request.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """An error we expect and know how to explain. Raise it anywhere."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class ValidationFailed(AppError):
    def __init__(self, field: str, issue: str) -> None:
        super().__init__(
            422, "validation_failed", "Request is invalid.", [{"field": field, "issue": issue}]
        )


class NotFound(AppError):
    def __init__(self, what: str = "Resource") -> None:
        super().__init__(404, "not_found", f"{what} not found.")


class Unauthorized(AppError):
    def __init__(
        self, code: str = "unauthorized", message: str = "Authentication required."
    ) -> None:
        super().__init__(401, code, message)


class Forbidden(AppError):
    def __init__(
        self, code: str = "forbidden", message: str = "You are not allowed to do this."
    ) -> None:
        super().__init__(403, code, message)


class Conflict(AppError):
    """The request is valid but clashes with the current state of the resource."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


def error_payload(
    request_id: str, code: str, message: str, details: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    error["request_id"] = request_id
    return {"error": error}


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):
        raise TypeError(exc)
    return JSONResponse(
        error_payload(_request_id(request), exc.code, exc.message, exc.details),
        status_code=exc.status,
    )


def _field(loc: tuple[int | str, ...]) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path", "header", "cookie")]
    return ".".join(parts) or "(body)"


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise TypeError(exc)
    errors = exc.errors()
    if any(e.get("type") == "json_invalid" for e in errors):
        return JSONResponse(
            error_payload(_request_id(request), "invalid_json", "Request body is not valid JSON."),
            status_code=400,
        )
    details = [
        {"field": _field(tuple(e.get("loc", ()))), "issue": str(e.get("msg"))} for e in errors
    ]
    return JSONResponse(
        error_payload(_request_id(request), "validation_failed", "Request is invalid.", details),
        status_code=422,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise TypeError(exc)
    if exc.status_code == 404:
        code, message = "route_not_found", f"No route for {request.method} {request.url.path}."
    elif exc.status_code == 405:
        code, message = "method_not_allowed", f"{request.method} is not allowed here."
    else:
        code, message = f"http_{exc.status_code}", str(exc.detail)
    return JSONResponse(
        error_payload(_request_id(request), code, message),
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
