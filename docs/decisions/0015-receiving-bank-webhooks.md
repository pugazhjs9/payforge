# 0015. UPI: results arrive by bank webhook, with polling as the safety net

**Status:** accepted

## Context
UPI is asynchronous: the bank answers `pending` and the customer approves in their UPI app seconds or
minutes later. The bank then calls **us**. That call can be forged, sent twice, or never arrive.

## Decision
`POST /webhooks/simbank`:
1. **Verify the signature over the raw body** before parsing anything (`t=…,v1=…`, HMAC-SHA256, a
   5-minute window). A forgery gets `401`.
2. **Dedupe:** `inbound_webhooks` has the primary key `(provider, event_id)`. The insert runs in a
   savepoint, and a duplicate-key error means "already handled".
3. **Record and apply in one transaction**, with the payment change, ledger and outgoing event. If
   applying fails, the record rolls back too, so the bank's retry is processed, not dropped as a
   duplicate.
4. **Answer 2xx** for processed, duplicate and ignored (unknown payment) events alike, so the bank stops
   resending. Answer 4xx only for things the bank can fix.

**Safety net:** if the webhook never arrives, the reconciliation job asks the bank
(`GET /charges/{payment id}`) about every payment still `processing` after 30 seconds. Webhooks are
the fast path, polling is the guarantee. Both finalize through the same compare-and-set, so whichever
comes first wins and the other is a no-op.

## Alternatives considered
- **Only polling.** Simple, but slow to notice results, and it costs a bank call per pending payment.
- **Only webhooks.** One lost webhook leaves a payment `processing` forever.
- **Dedupe by compare-and-set alone.** Enough for payment status, but the inbound log is also an audit
  trail, and future event types (disputes, settlements) won't all be compare-and-set updates.
