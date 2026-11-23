# 0011. Idempotency keys stored in MySQL, required on every create

**Status:** accepted

## Context
A merchant's server sends `POST /v1/payments`, and the connection drops before the response
arrives. It cannot know whether the card was charged. Retrying blindly may charge twice; not
retrying may lose the sale. Payment-ID-as-bank-reference (ADR 0003) protects the bank call, but
not the creation of a second payment row.

## Decision
`POST /v1/payments` and `POST /v1/payments/{id}/refunds` **require** an `Idempotency-Key`
header. Keys are stored in an `idempotency_keys` table with primary key `(merchant_id, key)`:

1. **Claim:** `INSERT` the key as `in_progress` with a random `lock_token`. The primary key makes
   this atomic: of 50 identical parallel requests, exactly one wins. The rest get a replay, a
   `409` with `Retry-After: 1` (still running), or a `422` (same key, different request, detected
   by a SHA-256 fingerprint of method, path and canonical JSON body).
2. **Link:** create the payment **and** save its id on the key in **one transaction**, so there
   is never a payment without its key, or a key pointing at nothing.
3. **Complete:** save the status code and body. Retries get exactly that response back, with
   `Idempotent-Replayed: true`.

**Crashes.** If a worker dies mid-request, its lock goes stale. After 60 s the next retry takes
the lock over with a conditional `UPDATE` and **resumes** from the saved `resource_id`. Calling
the bank again is safe because the bank deduplicates by payment ID. Every write after the claim
checks `lock_token` (a **fencing token**), so a slow worker that lost its lock cannot overwrite
the new owner's result.
**Errors.** If nothing was created (validation error, refund larger than the remaining amount),
the key is deleted, so the corrected request can reuse it. If something was created, the key is
unlocked, so a retry resumes immediately.

## Alternatives considered
- **Redis `SET NX` for the key.** Fast, but the key and the payment would live in different
  stores: a crash between them leaves a payment without a key, or a key without a payment, and
  Redis eviction or failover can lose keys. The record that prevents a double charge must be
  as durable as the charge, so it lives in the same database and transaction.
- **`INSERT IGNORE`.** In MySQL it also silently ignores other errors (it turns some into
  warnings). Catching the duplicate-key error (1062) is explicit.
- **Optional keys** (as some gateways do). Safer to require them now, before anyone integrates.
  Requiring them later would be a breaking change and need `/v2`.
- **Natural deduplication on the merchant's order ID.** It only works for merchants that have one
  and want it to be unique, so it is not a general retry mechanism.

## Consequences
- The key column uses `ascii_bin`: MySQL's default collation ignores case, which would make
  `Order-1` and `order-1` the same key.
- Keys expire after 24 hours. An expired key is reclaimed in place, and a cleanup job (Phase 4)
  will delete old rows using the `expires_at` index.
- A replay returns the **original** response (for example `202 processing`). To see the latest
  state, merchants `GET` the payment.
