# PayForge

[![CI](https://github.com/pugazhjs9/payforge/actions/workflows/ci.yml/badge.svg)](https://github.com/pugazhjs9/payforge/actions/workflows/ci.yml)

A multi-merchant **payment gateway API** built with **FastAPI and MySQL**: merchants create,
track and refund payments that are processed by a simulated bank. Built around the problems
real payment systems have: retries that could double-charge, banks that answer late, and money
that must always balance.

> No real money or card data: **SimBank**, a small fake bank in this repo, uses test tokens.

## Highlights

- **Correct money** — integer paise everywhere; decimal strings at the API edge, never floats.
- **Timeouts are "unknown", not "failed"** — a slow bank returns `202 processing`; the payment ID is the bank reference, so a retry can never charge twice.
- **Double-entry ledger enforced by MySQL** — stored as transfers that balance row by row, with `CHECK` constraints and append-only triggers.
- **Race-safe refunds** — parallel refunds can never exceed the payment (one conditional `UPDATE` on InnoDB, backed by a `CHECK` constraint).
- **Cursor pagination** that stays stable while new payments arrive.
- **Two auth models** — dashboard: Redis sessions with httpOnly cookies, CSRF tokens and "Sign in with GitHub" (OAuth 2.0 + PKCE); merchant servers: API keys exchanged for 15-minute ES256 JWTs (OAuth client credentials) with scopes, a JWKS endpoint and instant revocation.
- **Consistent API** — one error shape, meaningful status codes (201 / 202 / 409 / 413 / 415 / 422), request IDs on every response, OpenAPI docs at `/docs`.
- **Fully async** — FastAPI + async SQLAlchemy 2.0 + httpx; waiting on the bank doesn't hold a thread.
- **Tested against real dependencies** — integration tests run on real MySQL and a real SimBank server, in CI on every push; `mypy --strict` and ruff on all code.

## Architecture

```
 Merchant's server ──Bearer JWT──►┐
                                  ├─► PayForge API (FastAPI) ──HTTP, timeout──► SimBank (fake bank)
 Browser dashboard ──cookie──────►┘        │            │
                                           ▼            ▼
                                       MySQL 8.4      Redis 7
                      payments · refunds · ledger    sessions · revoked keys
```

Design decisions, with the alternatives that were rejected: [`docs/decisions`](docs/decisions).

## Run it locally

Requirements: Python 3.13 + [uv](https://docs.astral.sh/uv/), Docker.

```sh
uv sync
cp .env.example .env
docker compose up -d mysql redis
uv run alembic -c apps/api/alembic.ini upgrade head
uv run python -m payforge.seed
```

Start the fake bank and the API in two terminals:

```sh
uv run uvicorn simbank.main:app --port 4000
```

```sh
uv run uvicorn payforge.main:app --reload --port 3000
```

Open the dashboard at <http://localhost:3000/app/> (sign up, then create an API key) and the
interactive API docs at <http://localhost:3000/docs>.

### Try it

Exchange the API key for an access token, then create a payment:

```sh
curl -s -X POST localhost:3000/v1/auth/token -u "$KEY_ID:$KEY_SECRET" -d grant_type=client_credentials
```

```sh
curl -s -X POST localhost:3000/v1/payments \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"amount":"499.00","currency":"INR","payment_method":"tok_card_ok"}'
```

| Test token | Result |
|------------|--------|
| `tok_card_ok` | `201`, succeeded |
| `tok_card_declined` / `tok_card_no_funds` | `201`, failed with a `failure_code` |
| `tok_card_slow` | `202`, processing (the bank answers after our timeout) |
| `tok_card_no_refunds` | succeeds, but refunds on it fail |

## API (v1)

| Method | Path | |
|--------|------|-|
| `POST` | `/v1/payments` | Create a payment → `201` final / `202` processing |
| `GET` | `/v1/payments?limit=&cursor=&status=` | List, newest first, cursor pagination |
| `GET` | `/v1/payments/{id}` | One payment |
| `POST` | `/v1/payments/{id}/refunds` | Refund all (no body) or part |
| `GET` | `/v1/payments/{id}/refunds` | A payment's refunds |
| `GET` | `/v1/balance` | What PayForge owes the merchant, from the ledger |
| `POST` | `/v1/auth/token` | API key → 15-minute access token (OAuth client credentials) |
| `GET` | `/.well-known/jwks.json` | Public keys that verify access tokens |

Dashboard (browser, session cookie): `/auth/signup`, `/auth/login`, `/auth/logout`, `/auth/me`,
`/auth/github` (+ callback), and `/dashboard/api-keys` (create / list / revoke).

## Tests and checks

```sh
uv run ruff check . && uv run mypy apps/api/src apps/simbank/src && uv run pytest
```

Unit tests, plus integration tests against MySQL and SimBank, including parallel-refund and
double-finalisation race tests.

## Project layout

```
apps/api/src/payforge/
  domain/      pure logic: money, cursors
  services/    business rules: payments, refunds, ledger, users, API keys (no HTTP code)
  auth/        passwords, sessions, access tokens, GitHub login
  clients/     the SimBank client (timeouts, "unknown" outcomes)
  api/         FastAPI routers, request/response schemas, dependencies
apps/api/migrations/   Alembic migrations (constraints and ledger triggers)
apps/simbank/          the fake bank
docs/decisions/        design decision records
```

## Roadmap

- [x] Core REST API: payments, refunds, ledger, pagination, validation
- [x] Authentication: sessions + GitHub OAuth for the dashboard, API keys + JWT for servers
- [ ] Redis: caching, rate limiting, idempotency keys
- [ ] Async: transactional outbox, RabbitMQ, background jobs, signed webhooks, UPI, S3 uploads
