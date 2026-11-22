# 0008. Sign in with GitHub: authorization code flow with PKCE

**Status:** accepted

## Decision
- OAuth 2.0 **authorization code** flow. `state` (random, stored 10 minutes in Redis, single-use
  via `GETDEL`) ties the callback to the login this browser started. **PKCE** (S256) makes a stolen
  authorization code useless without the verifier.
- The code is exchanged **server to server** with the client secret; the access token never
  reaches the browser. PayForge only asks for `read:user user:email`.
- GitHub OAuth apps don't issue an OpenID Connect `id_token`, so the user is identified by calling
  `/user` and `/user/emails`.
- **Account linking only through a verified primary email.** Linking through an unverified email
  would let anyone add your address to their GitHub account and take over your PayForge account
  (tested: the attacker gets a separate account instead).
- GitHub URLs are configurable, so tests run the whole flow against a local fake GitHub that
  really checks the client secret and the PKCE verifier.

## Alternatives considered
- **Implicit flow** — deprecated; tokens in the URL fragment.
- **An auth library (Authlib, fastapi-users)** — less code, but hides exactly the steps worth
  understanding; the flow is ~100 lines.
