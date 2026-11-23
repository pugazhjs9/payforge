# 0009. Cache the balance in Redis, but not single payments

**Status:** accepted

## Context
`GET /v1/balance` sums every ledger transfer a merchant ever had. That gets slower as the ledger
grows, and dashboards and merchant scripts poll it. `GET /v1/payments/{id}` is a primary-key
lookup that InnoDB answers from the clustered index in well under a millisecond.

## Decision
**Cache-aside** for the balance only: read Redis; on a miss, compute from MySQL and store it with
a TTL of 30 s ± 20% (jitter, so keys written together don't expire together).
Whoever posts to the ledger deletes the key **after the transaction commits**. Deleting before
the commit would let a concurrent reader put the old balance straight back.

Redis errors never fail a request: the cache falls back to MySQL and logs a warning.

## Alternatives considered
- **Cache single payments too.** Saves almost nothing on a primary-key read and adds a staleness
  risk to the resource merchants poll to see `processing` turn into `succeeded`. Rejected.
- **Update the cache on write instead of deleting.** Two writers can update in the wrong order
  and leave the older value cached. Deleting is always safe.
- **A stored, incrementally updated balance column.** Fast and exact, but a second source of
  truth next to the ledger that must never drift. Worth it at higher scale, together with
  periodic balance snapshots.

## Consequences
A cached balance can be at most one TTL old if a delete fails or races, so it is **for display
only**. Anything that moves money (payouts, in a later phase) must compute the balance from
the ledger inside its own transaction.
