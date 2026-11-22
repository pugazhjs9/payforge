# 0003. A bank timeout means "unknown" (202), never "failed"

**Status:** accepted

## Context
When the bank doesn't answer within our timeout it may still have charged the customer.
Marking the payment `failed` could make the merchant ask the customer to pay again.

## Decision
- Every bank call runs inside `asyncio.timeout(SIMBANK_TIMEOUT_SECONDS)`.
- Timeouts, connection errors, 5xx and unreadable answers are all **Unknown**: the payment stays
  `processing` and the API answers **202 Accepted** with the payment ID.
- A clear answer is final and returns **201 Created**, including a **decline**
  (`status: failed`, `failure_code: card_declined`). A decline is a business result, not an HTTP error.
- PayForge sends its **payment ID as the bank reference**. The bank de-duplicates by reference and
  offers `GET /charges/{reference}`, so the outcome can be looked up later and a retry can never
  create a second charge.
- State changes are **compare-and-set** (`UPDATE … WHERE status = 'processing'`, then check the
  row count), so two processes finishing the same payment can't both win.

## Alternatives considered
- **504 Gateway Timeout** — tells the client nothing happened, which may be false.
- **402 Payment Required for declines** — the payment resource *was* created and has an ID.

## Consequences
Something must resolve `processing` payments later (a reconciliation job that asks the bank).
