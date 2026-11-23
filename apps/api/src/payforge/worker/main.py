"""The worker process: `python -m payforge.worker`.

One asyncio process runs:
- the outbox relay (MySQL events → RabbitMQ), continuously;
- the webhook consumers (RabbitMQ → merchants' servers);
- scheduled jobs: reconciliation, the webhook sweeper, idempotency-key cleanup;
- report building (the reports table is its own job queue).

Run as many copies as you like: the relay uses SKIP LOCKED, deliveries are claimed
with compare-and-set, and every scheduled job tolerates a twin running at the same
moment. So there's no leader election and no single "cron" machine to lose.

Shutdown (SIGTERM, e.g. from Docker or Kubernetes): stop taking new messages, let the
loops finish their current round, close connections. A message being handled at that
moment is not acknowledged, so RabbitMQ gives it to another worker; handling it twice
is safe.
"""

import asyncio
import signal
from collections.abc import Awaitable, Callable

from payforge import messaging
from payforge.clients import simbank
from payforge.config import settings
from payforge.db import engine
from payforge.logging import configure_logging, log
from payforge.messaging import Topology
from payforge.redis import redis
from payforge.services import reconcile, reports, webhooks
from payforge.worker import webhooks as webhook_consumers
from payforge.worker.relay import run_relay, sleep_unless_stopped

Job = Callable[[], Awaitable[object]]


async def every(seconds: float, name: str, job: Job, stop: asyncio.Event) -> None:
    """Runs `job` every `seconds` until stopped. A failing round is logged, not fatal."""
    while not stop.is_set():
        try:
            result = await job()
            if result:
                log.info("job_done", job=name, result=result)
        except Exception:
            log.exception("job_failed", job=name)
        await sleep_unless_stopped(stop, seconds)


def sweep_webhooks(topology: Topology) -> Job:
    async def run() -> int:
        due = await webhooks.sweep_overdue()
        for delivery_id, attempt in due:
            await webhook_consumers.publish_attempt(topology, delivery_id, attempt)
        return len(due)

    return run


async def reconcile_all() -> int:
    return await reconcile.reconcile_payments() + await reconcile.reconcile_refunds()


async def run(stop: asyncio.Event) -> None:
    connection = await messaging.connect()
    try:
        # publishing and consuming on separate channels: a slow consumer can't hold up
        # the relay's publisher confirms
        publisher = await connection.channel(on_return_raises=True)
        topology = await messaging.declare(publisher)
        consumer = await connection.channel()
        await webhook_consumers.start(consumer, topology)
        log.info("worker_started")

        loops = [
            run_relay(topology.events, stop),
            every(15, "reconcile", reconcile_all, stop),
            every(10, "sweep_webhooks", sweep_webhooks(topology), stop),
            every(2, "reports", reports.run_pending, stop),
            every(
                600,
                "delete_expired_idempotency_keys",
                reconcile.delete_expired_idempotency_keys,
                stop,
            ),
        ]
        tasks = [asyncio.create_task(loop) for loop in loops]
        await stop.wait()
        log.info("worker_stopping")
        await consumer.close()  # no new messages; unacknowledged ones go back to the queue
        await asyncio.gather(*tasks)
    finally:
        await connection.close()
        await webhooks.close()
        await simbank.close()
        await redis.aclose()
        await engine.dispose()
        log.info("worker_stopped")


def main() -> None:
    configure_logging(settings.log_level, json=settings.environment != "development")

    async def start() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(stop)

    asyncio.run(start())
