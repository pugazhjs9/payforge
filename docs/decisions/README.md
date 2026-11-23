# Decision records

Short notes on the important design choices: the problem, the options, what was chosen and why.

| # | Decision |
|---|----------|
| [0001](0001-stack.md) | FastAPI + MySQL 8.4 + async SQLAlchemy 2.0 |
| [0002](0002-money-as-integer-paise.md) | Store money as integer paise, never floats |
| [0003](0003-timeout-means-unknown.md) | A bank timeout means "unknown" (202), never "failed" |
| [0004](0004-cursor-pagination.md) | Cursor pagination on `(created_at, id)` instead of offset |
| [0005](0005-atomic-refund-reservation.md) | Reserve refund amounts with one conditional UPDATE |
| [0006](0006-ledger-as-transfers.md) | Double-entry ledger stored as transfers, enforced by MySQL |
| [0007](0007-sessions-for-people-jwt-for-servers.md) | Sessions for people, JWTs for servers |
| [0008](0008-sign-in-with-github.md) | Sign in with GitHub: authorization code flow with PKCE |
| [0009](0009-cache-the-balance-not-payments.md) | Cache the balance in Redis, but not single payments |
| [0010](0010-rate-limiting.md) | Token bucket per merchant; attempt counters for login |
| [0011](0011-idempotency-keys.md) | Idempotency keys in MySQL, required on every create |
