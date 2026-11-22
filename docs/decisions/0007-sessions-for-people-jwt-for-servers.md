# 0007. Sessions for people, JWTs for servers

**Status:** accepted

## Context
Two kinds of callers: people using the dashboard in a browser, and merchants' servers
calling `/v1`. They have different needs and different threats.

## Decision
- **Dashboard (browser): server-side sessions in Redis.** The cookie holds only a random
  256-bit ID (`HttpOnly`, `SameSite=Lax`, `Secure` in production); the data lives under
  `sess:<id>` with a sliding idle timeout (1 day) and an absolute limit (7 days). A new ID is
  issued at every login (no session fixation). Logout deletes the key: instant revocation.
- **CSRF**: `SameSite=Lax`, an `Origin` check on every state-changing dashboard request, and a
  per-session CSRF token sent back in `X-CSRF-Token`.
- **Roles are read from the database on each check**, not cached in the session, so a demoted
  user loses access immediately (a test caught the stale-role bug).
- **Merchant servers: API key → short-lived JWT** via the OAuth 2.0 client-credentials grant
  (`POST /v1/auth/token`, HTTP Basic). Tokens are **ES256**, live **15 minutes**, carry the
  merchant ID and **scopes** (`payments:read`, `payments:write`, `refunds:write`). The merchant
  ID always comes from the token, never from the URL or body (tenant isolation).
- **Revocation**: revoking a key adds its ID to a Redis set checked on every request, so its
  tokens die immediately instead of after 15 minutes.
- **API secrets are stored as SHA-256**, passwords as **argon2id** (19 MiB, 2 passes), hashed in
  a worker thread so the event loop isn't blocked.

## Alternatives considered
- **JWTs in the browser (localStorage)** — any XSS bug could steal them, and they can't be revoked.
- **One mechanism for both** — cookies don't fit servers; long-lived tokens don't fit browsers.
- **HS256 (shared secret)** — every verifier could also forge tokens. ES256 lets other services
  verify with the public key from `/.well-known/jwks.json` (`kid` allows key rotation).
- **Sending the API key on every request** — the long-lived secret would travel constantly; a
  leaked token expires in 15 minutes, a leaked key doesn't.
- **Refresh tokens** — useful for mobile apps where a user logs in once; a server holding a client
  secret can simply ask for a new token, and RFC 6749 says client-credentials should not issue them.
- **argon2 for API secrets** — a 32-byte random secret can't be guessed, so a fast hash is safe
  and doesn't add ~20 ms to every token request.
