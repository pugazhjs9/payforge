"""Pure ASGI middleware: request IDs + access logs, and a request-body size limit."""

import json
import re
import time
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from payforge.errors import AppError, error_payload
from payforge.ids import new_id
from payforge.logging import log

# Only accept short, plain IDs from clients. Anything else could inject fake lines
# into our logs, so we replace it with our own.
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


async def send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    data = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


class RequestContextMiddleware:
    """Gives every request one ID (reusing the client's if safe), sends it back in
    X-Request-Id, writes one access-log line, and turns unexpected exceptions into
    a 500 that reveals nothing but the request ID."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if SAFE_REQUEST_ID.fullmatch(incoming) else new_id("req")
        scope.setdefault("state", {})["request_id"] = request_id

        status = 500
        started = False
        start = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                log.exception("unhandled error")  # full details in the log only
                if started:
                    raise
                await send_json(
                    send_with_id,
                    500,
                    error_payload(
                        request_id, "internal_error", "Something went wrong on our side."
                    ),
                )
            finally:
                if scope["path"] != "/healthz":  # health checks would flood the logs
                    level = "error" if status >= 500 else "warning" if status >= 400 else "info"
                    log.log(
                        {"error": 40, "warning": 30, "info": 20}[level],
                        "request completed",
                        method=scope["method"],
                        path=scope["path"],
                        status=status,
                        duration_ms=round((time.perf_counter() - start) * 1000, 1),
                    )


class BodyTooLarge(AppError):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            413, "body_too_large", f"Request body is larger than {max_bytes // 1000} KB."
        )


class BodySizeLimitMiddleware:
    """Refuses request bodies over `max_bytes`: early from Content-Length, or while
    streaming for chunked uploads."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            error = BodyTooLarge(self.max_bytes)
            request_id = scope.get("state", {}).get("request_id", "unknown")
            await send_json(
                send, error.status, error_payload(request_id, error.code, error.message)
            )
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge(self.max_bytes)
            return message

        await self.app(scope, limited_receive, send)
