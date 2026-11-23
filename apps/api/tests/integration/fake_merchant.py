"""A merchant's webhook receiver, for tests. Records what it receives; each path can be
told to answer with given status codes, to stall, or to redirect."""

import asyncio
import threading
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeMerchant:
    received: list[Received] = field(default_factory=list)
    statuses: dict[str, list[int]] = field(default_factory=dict)  # path -> answers to give
    lock: threading.Lock = field(default_factory=threading.Lock)

    def answer(self, path: str, *statuses: int) -> None:
        self.statuses[path] = list(statuses)

    def to(self, path: str) -> list[Received]:
        with self.lock:
            return [r for r in self.received if r.path == path]

    def reset(self) -> None:
        with self.lock:
            self.received.clear()
            self.statuses.clear()


def create_fake_merchant() -> tuple[FastAPI, FakeMerchant]:
    app = FastAPI()
    state = FakeMerchant()

    @app.post("/hooks/{name}")
    async def hook(name: str, request: Request) -> Response:
        path = f"/hooks/{name}"
        with state.lock:
            state.received.append(Received(path, dict(request.headers), await request.body()))
            planned = state.statuses.get(path)
            status = planned.pop(0) if planned else 200
        if name == "slow":
            await asyncio.sleep(2)
        if name == "redirect":
            return Response(status_code=302, headers={"Location": "http://169.254.169.254/"})
        return Response(content=f"answer {status}", status_code=status)

    return app, state
