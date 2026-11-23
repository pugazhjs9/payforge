# PayForge

[![CI](https://github.com/pugazhjs9/payforge/actions/workflows/ci.yml/badge.svg)](https://github.com/pugazhjs9/payforge/actions/workflows/ci.yml)

A multi-merchant **payment gateway** built with **FastAPI, MySQL, Redis and RabbitMQ**:
merchants take card and UPI payments, refund them, and get signed webhooks, all processed by
a simulated bank. It's built around the problems real payment systems have: retries that
could double-charge, banks that answer late or by webhook, events that must not be lost, and
money that must always balance.

> No real money or card data: **SimBank**, a small fake bank in this repo, uses test tokens.

## Highlights

- **Correct money** — integer paise everywhere; decimal strings at the API edge, never floats.
- **Safe retries with idempotency keys** — every payment and refund needs an `Idempotency-Key`; a retry replays the first response. 50 identical parallel requests create exactly one payment, and a crashed request is resumed, not repeated (fencing tokens, stored in MySQL next to the payment).
- **Timeouts are "unknown", not "failed"** — a slow bank returns `202 processing`; a reconciliation job later asks the bank what happened. The payment ID is the bank reference, so nothing is charged twice.
- **Double-entry ledger enforced by MySQL** — stored as transfers that balance row by row, with `CHECK` constraints and append-only triggers.
- **Transactional outbox → RabbitMQ** — every event is written in the same transaction as the change it describes, then relayed with publisher confirms. Events are never lost and never announced for a rolled-back change.
- **Signed webhooks with retries** — HMAC-SHA256 with a signed timestamp; retried over ~7 hours through per-delay TTL queues; a dead-letter queue for poison messages; a delivery log and "resend" in the dashboard; an SSRF guard on merchant URLs.
- **UPI, asynchronous like the real thing** — the bank confirms later by webhook, which is signature-verified and deduplicated, with polling as the safety net.
- **Files on S3** — browser uploads go straight to storage with presigned POST policies (size and type enforced by S3, then magic bytes checked); CSV reports are built by the worker and downloaded by presigned URL.
- **Redis where it fits** — a per-merchant token-bucket rate limiter (one atomic Lua script, `429` + `Retry-After`), login attempt limits, and a cache-aside balance; the API keeps working if Redis goes down.
- **Two auth models** — dashboard: Redis sessions with httpOnly cookies, CSRF tokens and "Sign in with GitHub" (OAuth 2.0 + PKCE); merchant servers: API keys exchanged for 15-minute ES256 JWTs (OAuth client credentials) with scopes, a JWKS endpoint and instant revocation.
- **Consistent API** — one error shape, meaningful status codes (201 / 202 / 400 / 409 / 413 / 415 / 422 / 429), request IDs on every response, OpenAPI docs at `/docs`.
- **Fully async** — FastAPI, async SQLAlchemy, httpx and aio-pika; one worker process runs every background job, and any number of copies can run side by side.
- **Tested against real dependencies** — about 290 tests against real MySQL, Redis, RabbitMQ, an S3 server and SimBank, run in CI on every push; `mypy --strict` and ruff on all code.

## Architecture

```
 Merchant's server ──Bearer JWT──►┐                          ┌──► SimBank (fake bank)
                                  ├─► PayForge API (FastAPI) ┤      │ UPI result, signed webhook
 Browser dashboard ──cookie──────►┘    │       │       │     └◄─────┘
        │ presigned upload             │       │       │
        ▼                              ▼       ▼       ▼
   S3 (SeaweedFS) ◄── reports ──  MySQL 8.4  Redis 7  (no broker in the request path)
        ▲                          │ events (outbox)
        │                          ▼
        └──────────────────  Worker ──► RabbitMQ ──► Worker ──signed POST──► merchant's webhook URL
                    relay · reconciliation · sweeper · reports
```

- **MySQL**: payments, refunds, ledger, idempotency keys, events (the outbox), webhook deliveries.
- **Redis**: sessions, revoked API keys, rate limits, balance cache.
- **RabbitMQ**: event fan-out and webhook delivery, with retry queues and a dead-letter queue.
- **S3**: merchant logos and CSV reports.

Design decisions, with the alternatives that were rejected: [`docs/decisions`](docs/decisions).

## Run it locally

Requirements: Python 3.13 + [uv](https://docs.astral.sh/uv/), Docker.

```sh
uv sync
cp .env.example .env
docker compose up -d
uv run alembic -c apps/api/alembic.ini upgrade head
```

Start the fake bank, the API and the worker, each in its own terminal:

```sh
uv run uvicorn simbank.main:app --port 4000
```

```sh
uv run uvicorn payforge.main:app --reload --port 3000
```

```sh
uv run python -m payforge.worker
```

Open the dashboard at <http://localhost:3000/app/>, sign up, and create an API key. The
interactive API docs are at <http://localhost:3000/docs>, and RabbitMQ's management UI is at
<http://localhost:15673> (guest / guest).

### Try it with the demo shop

`apps/demo-shop` is a tiny merchant site that takes payments through PayForge and receives
its webhooks. In the dashboard, add the webhook endpoint `http://127.0.0.1:5050/webhooks/payforge`,
then start the shop with your key and the endpoint's signing secret:

```sh
PAYFORGE_KEY_ID=key_… PAYFORGE_KEY_SECRET=pf_sk_test_… PAYFORGE_WEBHOOK_SECRET=whsec_… uv run uvicorn demo_shop.app:app --port 5050
```

Open <http://localhost:5050> and pay by card or UPI. UPI orders show `processing`, and a few
seconds later they flip to `succeeded` or `failed`. That's the whole chain: SimBank's webhook →
PayForge → outbox → RabbitMQ → worker → the shop's webhook.

### Or with curl

```sh
curl -s -X POST localhost:3000/v1/auth/token -u "$KEY_ID:$KEY_SECRET" -d grant_type=client_credentials
```

```sh
curl -s -X POST localhost:3000/v1/payments \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -H "idempotency-key: $(uuidgen)" \
  -d '{"amount":"499.00","currency":"INR","payment_method":"tok_card_ok"}'
```

Send the same request with the same key again and you get the same payment back, with
`Idempotent-Replayed: true`; the card is not charged twice.

| Payment method | Result |
|----------------|--------|
| `tok_card_ok` | `201`, succeeded |
| `tok_card_declined` / `tok_card_no_funds` | `201`, failed with a `failure_code` |
| `tok_card_slow` | `202`, processing (the bank answers after our timeout; reconciliation finishes it) |
| `tok_card_no_refunds` | succeeds, but refunds on it fail |
| `upi:kiran@okbank` | `202`, processing → succeeded a few seconds later (bank webhook) |
| `upi:fail@okbank` / `upi:expire@okbank` | `202`, processing → failed |

## API (v1)

| Method | Path | |
|--------|------|-|
| `POST` | `/v1/payments` | Create a payment → `201` final / `202` processing (needs `Idempotency-Key`) |
| `GET` | `/v1/payments?limit=&cursor=&status=` | List, newest first, cursor pagination |
| `GET` | `/v1/payments/{id}` | One payment |
| `POST` | `/v1/payments/{id}/refunds` | Refund all (no body) or part (needs `Idempotency-Key`) |
| `GET` | `/v1/payments/{id}/refunds` | A payment's refunds |
| `GET` | `/v1/balance` | What PayForge owes the merchant, from the ledger |
| `GET` | `/v1/events?type=&limit=&cursor=` | Everything that happened (the same events are sent as webhooks) |
| `GET` | `/v1/events/{id}` | One event |
| `POST` | `/v1/reports` | Start a CSV report of payments → `202` |
| `GET` | `/v1/reports/{id}` | Report status, and a 5-minute download link when ready |
| `POST` | `/v1/auth/token` | API key → 15-minute access token (OAuth client credentials) |
| `GET` | `/.well-known/jwks.json` | Public keys that verify access tokens |

Webhooks carry the event as JSON, with `PayForge-Signature: t=<time>,v1=<HMAC-SHA256>`. They are
delivered **at least once** and **not necessarily in order**: dedupe by event id.

Dashboard (browser, session cookie): `/auth/*` (sign-up, login, logout, GitHub), and under
`/dashboard`: API keys, webhook endpoints with their delivery log and resend, and the logo upload.

## Tests and checks

```sh
uv run ruff check . && uv run mypy apps/api/src apps/simbank/src apps/demo-shop/src && uv run pytest
```

The integration tests run against real MySQL, Redis, RabbitMQ (its own vhost), SeaweedFS and
SimBank. They include race tests (parallel refunds, double finalisation, 50 identical payment
requests, parallel outbox relays and report workers), crash recovery (an interrupted
request, a stale webhook attempt, a report left running), the full webhook path through
RabbitMQ's retry and dead-letter queues, and the worker process starting and stopping on
SIGTERM.

## Project layout

```
apps/api/src/payforge/
  domain/        pure logic: money, cursors
  services/      business rules: payments, refunds, ledger, idempotency, events, webhooks,
                 bank webhooks, reconciliation, reports, logos, users, API keys (no HTTP code)
  api/           FastAPI routers, request/response schemas, dependencies
  worker/        the background worker: outbox relay, webhook consumers, scheduled jobs
  auth/          passwords, sessions, access tokens, GitHub login
  clients/       the SimBank client (timeouts, "unknown" outcomes, webhook parsing)
  messaging.py   RabbitMQ exchanges and queues
  storage.py     S3: presigned uploads and downloads
  cache.py, ratelimit.py, net.py (SSRF guard), webhook_signing.py
apps/api/migrations/   Alembic migrations (constraints and ledger triggers)
apps/simbank/          the fake bank (cards, UPI, signed webhooks)
apps/demo-shop/        a merchant shop that uses PayForge
docs/decisions/        design decision records
```

## Roadmap

- [x] Core REST API: payments, refunds, ledger, pagination, validation
- [x] Authentication: sessions + GitHub OAuth for the dashboard, API keys + JWT for servers
- [x] Reliability: idempotency keys, rate limiting, Redis caching
- [x] Async: transactional outbox, RabbitMQ, background worker, signed webhooks, UPI, S3
- [ ] Real-time dashboard (WebSockets), GraphQL and gRPC APIs, metrics and tracing
