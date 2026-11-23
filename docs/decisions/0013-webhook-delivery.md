# 0013. Webhook delivery: signed, retried by RabbitMQ, tracked in MySQL

**Status:** accepted

## Context
Merchants' servers are slow, down for deploys, or broken. Webhooks must be retried for hours without
flooding anyone, must be verifiable (anyone can POST to a merchant's URL), and must never become a
way to attack PayForge's own network.

## Decision
- **Signature:** `PayForge-Signature: t=<unix time>,v1=<HMAC-SHA256(secret, "t." + raw body)>`. The
  timestamp is signed, and receivers reject anything older than 5 minutes, so a captured webhook
  can't be replayed. Each endpoint has its own secret.
- **State in MySQL, transport in RabbitMQ.** Every (event, endpoint) pair is one `webhook_deliveries`
  row (unique, so a redelivered event creates nothing new). Every HTTP attempt is logged in
  `webhook_attempts`, which is the delivery log shown in the dashboard.
- **Claiming:** an attempt message carries its number. `UPDATE … SET attempts = n WHERE attempts =
  n - 1` claims it, so a duplicate or stale message changes nothing (compare-and-set again).
- **Retries:** after attempts 1–5 fail, wait 10 s, 1 min, 10 min, 1 h, then 6 h (6 attempts over about
  7 hours). Each delay has **its own queue** with a message TTL that dead-letters back to the delivery
  queue. One shared queue can't do this, because RabbitMQ only expires messages at the head of a
  queue: a 6-hour message would block a 10-second one behind it.
- **Sweeper:** if a message is lost, or a worker dies mid-attempt (its 5-minute lease runs out), the
  overdue row in MySQL is found and a fresh message is sent. The database is the source of truth.
- **Poison messages:** work queues are quorum queues with a delivery limit of 5. A message that keeps
  crashing its consumer goes to `webhooks.dead` instead of looping forever.
- **SSRF guard:** `https` only, no credentials in the URL, and every address the host name resolves
  to must be public (no 10/8, 127/8, 169.254/16 cloud metadata, IPv4-mapped IPv6…). This is checked
  when the endpoint is saved and again before each attempt (DNS can change). Redirects are not
  followed.

## Alternatives considered
- **The delayed-message exchange plugin.** Simpler routing, but it's a plugin to install, and its
  delayed messages live on a single node and aren't replicated.
- **Retries by polling MySQL only (`next_attempt_at`).** Also valid, and it is our fallback (the
  sweeper). RabbitMQ gives instant hand-off and spreads work across workers without hammering the
  table.
- **Asymmetric signatures (Ed25519) or mTLS.** Stronger, but harder for merchants to set up. HMAC with
  a per-endpoint secret is what most payment providers use.

## Consequences
Merchants must handle **duplicates** (dedupe by event id) and **out-of-order** delivery (an event is
a snapshot; GET the payment for the latest state). The demo shop in `apps/demo-shop` shows how. The
remaining SSRF gap is the race between the DNS check and connecting; in production an egress proxy
that applies the same rules at connect time closes it.
