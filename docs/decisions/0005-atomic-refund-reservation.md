# 0005. Reserve refund amounts with one conditional UPDATE

**Status:** accepted

## Context
Two ₹60 refunds on a ₹100 payment can arrive at the same moment. If each reads "₹0 refunded",
checks `60 <= 100` in Python and then writes, both pass.

## Decision
The check and the write are **one SQL statement**:

```sql
UPDATE payments
SET amount_refunded_minor = amount_refunded_minor + :amount
WHERE id = :id AND merchant_id = :merchant
  AND status = 'succeeded'
  AND amount_refunded_minor + :amount <= amount_minor
```

InnoDB's `UPDATE` is a locking read: the second request waits for the first one's row lock,
then re-evaluates the `WHERE` clause against the latest committed row, so only reservations that
still fit succeed (0 rows → `409`). The refund row is inserted `pending` in the same transaction,
the bank is called afterwards, and a failed bank refund gives the reservation back.
A `CHECK (amount_refunded_minor BETWEEN 0 AND amount_minor)` constraint is the last line of defence.

## Alternatives considered
- **`SELECT … FOR UPDATE`, then check in Python** — also correct, but holds an explicit lock
  longer and is easy to break by forgetting the `FOR UPDATE`.
- **SERIALIZABLE isolation** — correct but needs retry logic for lock conflicts everywhere.

## Consequences
`amount_refunded` includes pending refunds on purpose: money that may be on its way back must
not be refunded again. An integration test fires two refunds in parallel.
