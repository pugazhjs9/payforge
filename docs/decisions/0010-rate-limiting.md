# 0010. Rate limiting: a token bucket per merchant, attempt counters for login

**Status:** accepted

## Context
One merchant's runaway script must not slow down every other merchant. Login must resist
password guessing. The API runs as several processes, so limits have to live in one shared
place: Redis.

## Decision
- **`/v1`: token bucket per merchant** (default 100 burst, 25 requests/s refill). The whole
  read, refill, take and write runs as **one Lua script**, so it is atomic across API instances,
  and it uses Redis's clock, so app servers with skewed clocks agree. The bucket is keyed by
  merchant, not API key, so creating more keys does not raise the quota.
  Responses carry `RateLimit-Limit/Remaining/Reset`; a refusal is `429` with `Retry-After`.
- **Login: fixed-window counters** per *email + IP* (10 per 15 minutes) and per *IP* (100 per
  15 minutes); signups per IP (20 per hour). A successful login clears that account's counter.
- **Failure modes differ on purpose:** `/v1` **fails open** (if Redis is down, traffic is
  unlimited rather than every checkout failing). Login **fails closed** (`503`): without the
  limit, attackers get unlimited guesses, and login needs Redis for sessions anyway.

## Alternatives considered
- **Fixed window for the API.** Allows double the rate around a window boundary and gives no
  burst control. Fine for "10 login attempts per 15 minutes", too coarse for API traffic.
- **Sliding log (a sorted set of timestamps).** Exact, but stores one entry per request.
- **In-process counters.** Each API instance would allow the full limit by itself.
- **Limiting login per email only.** Anyone could then lock a real user out by typing their
  email with wrong passwords. Combining email with IP avoids that.
- **Per-IP limits on every route in the app.** This belongs at the load balancer or CDN. The app
  limits what only it understands: merchants and accounts.

## Consequences
Behind a proxy, uvicorn must run with `--proxy-headers --forwarded-allow-ips=<proxy>` so the
client IP comes from `X-Forwarded-For`, and only when the proxy sent that header.
