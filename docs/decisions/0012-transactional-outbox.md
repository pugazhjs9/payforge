# 0012. Events through a transactional outbox

**Status:** accepted

## Context
When a payment succeeds, the merchant must hear about it (a webhook). The naive version, "commit the
payment, then publish to RabbitMQ", is a **dual write**: if the process dies between the two, or the
broker is down, the event is lost forever. Publishing *before* the commit is worse: a rolled-back
payment would be announced as succeeded.

## Decision
The event is a row in an `events` table, inserted **in the same transaction** as the status change,
the ledger entry and everything else. Either all of it commits or none of it does.

A **relay** in the worker then moves events to RabbitMQ:
1. lock a batch of unpublished events with `SELECT id … FOR UPDATE SKIP LOCKED`;
2. publish them and wait for RabbitMQ's **publisher confirms**;
3. set `published_at` in the same transaction.

If the relay dies between 2 and 3, those events are published again: **at least once**, never lost.
Consumers dedupe by event id. `GET /v1/events` serves the same table, so a merchant can also catch up
on anything they missed.

## Alternatives considered
- **Publish directly after commit.** Loses events on crashes and broker outages, as described above.
- **Change data capture (Debezium reading the MySQL binlog).** No polling and no extra writes, but it
  needs Kafka Connect, binlog access and another system to run. Worth it at much larger scale.
- **Distributed transactions (XA / 2PC) across MySQL and RabbitMQ.** RabbitMQ doesn't support them,
  and they couple the availability of both systems.

## A lesson from the tests
The first relay selected whole rows `FOR UPDATE SKIP LOCKED … LIMIT 100`. On a small table MySQL
chose a full scan and sort, and **`FOR UPDATE` locked every row it scanned**, not just the 100 it
returned. A second relay then found everything locked, so parallel relays did nothing. Locking only
`id`, read in order from the `(published_at, created_at, id)` index, fixed it. The same thing
happened again in the report queue (ADR 0014). `SKIP LOCKED` only works well when an index matches
the query's `WHERE` and `ORDER BY`.
