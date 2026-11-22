# 0001. FastAPI + MySQL 8.4 + async SQLAlchemy 2.0

**Status:** accepted

## Context
A payment gateway is I/O-bound: most of each request is spent waiting on the database
and on the bank. It needs strict input validation and real transactions with row locks.

## Decision
- **Python 3.13 + FastAPI**, fully `async`.
- **Pydantic v2** for request/response validation (and the OpenAPI docs come for free).
- **MySQL 8.4 (InnoDB)** as the source of truth.
- **SQLAlchemy 2.0 (async, `asyncmy` driver)** + **Alembic** migrations written by hand.
- Transaction isolation **READ COMMITTED**.

## Alternatives considered
- **Flask / Django** — Flask has no built-in validation or async-first design; Django is
  excellent for full apps with an admin, but its ORM and request cycle are heavier than needed
  for a JSON API. FastAPI gives typed validation, async I/O and OpenAPI docs out of the box.
- **Sync FastAPI endpoints (thread pool)** — simpler, but each request waiting 3 s on the bank
  would hold a thread; with async, waiting costs almost nothing.
- **PostgreSQL** — would also be a great fit (deferrable constraints, `RETURNING`). MySQL 8.4
  covers what we need: CHECK constraints (8.0.16+), `SKIP LOCKED`, descending indexes, JSON.
  The one gap (no deferrable constraints) is designed around in 0006.
- **MongoDB** — no CHECK constraints or row-level locking semantics money needs.
- **Raw SQL only** — more control; the ORM gives typed models and safe parameter binding,
  and conditional updates are still written explicitly.
- **REPEATABLE READ (MySQL's default)** — takes gap locks that cause more lock waits and
  deadlocks on inserts. Our correctness never relies on repeatable snapshots: every race is
  handled by a conditional `UPDATE` (a locking read that sees the latest committed row).

## Consequences
- MySQL DDL is not transactional: a migration that fails halfway is not rolled back, so
  migrations are small and reviewed.
- MySQL `DATETIME(3)` has no time zone: everything is stored as UTC, and a custom column type
  returns timezone-aware datetimes. The connection sets `time_zone = '+00:00'`.
