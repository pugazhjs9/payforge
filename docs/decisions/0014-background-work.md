# 0014. One async worker: RabbitMQ for webhooks, the database as queue for reports

**Status:** accepted

## Context
Background work: relaying events, delivering webhooks, reconciling stuck payments, cleaning expired
idempotency keys, and building CSV reports.

## Decision
One process, `python -m payforge.worker`, running asyncio tasks: the relay, the RabbitMQ consumers,
and a few `every(n seconds)` loops. Run as many copies as needed; **every job is safe to run twice at
the same moment**: the relay uses `SKIP LOCKED`, finalizing payments is compare-and-set, deliveries are
claimed by attempt number, and expired-key cleanup is a plain idempotent `DELETE … LIMIT`. So there's
no leader election and no single cron machine to lose.

- **Webhooks use RabbitMQ:** high volume, fan-out, delayed retries and dead-lettering are exactly
  what a broker is for.
- **Reports use the database as the queue:** the `reports` row *is* the job. Its status is what the API
  shows, and the worker claims the oldest pending one with `SKIP LOCKED`. A second copy of the job in
  a broker would have to be kept in sync, for a job that runs a few times a day.
- **Shutdown:** on SIGTERM the worker stops consuming, lets each loop finish its round, and exits. An
  unacknowledged message goes back to the queue, and handling it twice is safe.

## Alternatives considered
- **Celery.** The standard choice, but its workers are synchronous, while this code base is async
  end to end (async SQLAlchemy, httpx). Every task would need `asyncio.run` and its own engine.
  Celery beat would also be one more scheduler process to keep single.
- **arq.** Async-native and Redis-based, but in maintenance mode, and Redis isn't a durable queue
  unless it's configured carefully.
- **Separate processes per job.** Easier to scale independently, but more to deploy. One process with
  clear task boundaries can be split later without code changes.
