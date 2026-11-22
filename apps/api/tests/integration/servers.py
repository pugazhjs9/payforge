"""Run a real ASGI app on a local port in a background thread (for SimBank, fake GitHub…)."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI


@contextmanager
def serve_in_thread(app: FastAPI, port: int) -> Iterator[None]:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"server on port {port} did not start")
        time.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5)
