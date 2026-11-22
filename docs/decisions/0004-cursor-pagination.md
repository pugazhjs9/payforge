# 0004. Cursor pagination on (created_at, id) instead of offset

**Status:** accepted

## Context
`GET /v1/payments` lists a merchant's payments newest first while new payments keep arriving.

## Decision
- Clients get an opaque `next_cursor`: base64url of the last item's `created_at` and `id`.
- The next page is `created_at < :t OR (created_at = :t AND id < :id)`, ordered by
  `created_at DESC, id DESC`, served by the descending index `(merchant_id, created_at DESC, id DESC)`.
- `id` breaks ties between rows with the same timestamp (tested with 5 rows per timestamp).
- Timestamps are trimmed to milliseconds in Python before saving, because `DATETIME(3)` rounds
  finer values — otherwise the in-memory value used for a cursor wouldn't equal the stored one.
- One extra row is fetched to decide `has_more` without a `COUNT(*)`.

## Alternatives considered
- **Offset (`LIMIT 20 OFFSET 40`)** — a payment arriving between page requests shifts every row,
  so the next page repeats an item; deep offsets make MySQL read and discard every skipped row.

## Consequences
No "jump to page 37". A tampered cursor returns `400 invalid_cursor`.
