import asyncio
import os
import signal
import subprocess
import sys

import httpx
import pytest

from payforge.services import webhooks
from payforge.worker.main import run

from .conftest import MERCHANT_A, MERCHANT_SITE, merchant, pay, with_key
from .fake_merchant import FakeMerchant

A = merchant(MERCHANT_A)


@pytest.mark.usefixtures("bank", "channel")
async def test_the_worker_turns_a_payment_into_a_delivered_webhook(
    client: httpx.AsyncClient, merchant_site: FakeMerchant
) -> None:
    await webhooks.create_endpoint(MERCHANT_A, MERCHANT_SITE + "/hooks/a", ["*"], None)
    stop = asyncio.Event()
    worker = asyncio.create_task(run(stop))

    await client.post("/v1/payments", json=pay("100.00"), headers=with_key(A))
    for _ in range(100):  # relay → fan-out → delivery, all inside the worker
        if merchant_site.to("/hooks/a"):
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=10)  # and it shuts down cleanly
    assert len(merchant_site.to("/hooks/a")) == 1


@pytest.mark.usefixtures("channel")
def test_the_worker_process_starts_and_stops_on_sigterm() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "payforge.worker"],
        env={**os.environ, "LOG_LEVEL": "info"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if "worker_started" in line:
                break
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
        assert "worker_stopped" in process.stdout.read()
    finally:
        process.kill()
