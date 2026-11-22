# 0006. Double-entry ledger stored as transfers, enforced by MySQL

**Status:** accepted

## Context
PayForge must always know exactly what it owes each merchant, and every rupee must be
traceable. Bugs in application code must not be able to create or destroy money.

A classic double-entry design writes separate debit and credit entries and checks that each
transaction sums to zero **at commit**. PostgreSQL can do that with a deferrable constraint
trigger; **MySQL has no deferrable constraints**, and its row triggers run before all of a
transaction's entries exist.

## Decision
Store the ledger as **transfers**: one row = money moving `from_account` → `to_account`.
- Every row balances by itself, so the books can never be unbalanced — no commit-time check needed.
  (The same model is used by purpose-built ledgers such as TigerBeetle.)
- Accounts: `world:bank`, `merchant:<id>:balance`, `platform:fees`.
- `CHECK (amount_minor > 0)` and `CHECK (from_account <> to_account)`.
- **Append-only**: `BEFORE UPDATE` / `BEFORE DELETE` triggers `SIGNAL` an error; mistakes are
  fixed with a reversing transfer.
- Each posting has a unique key (`payment:<id>`, `refund:<id>`), so posting twice fails instead
  of counting twice.
- Balance = `SUM(to = account) - SUM(from = account)`, each half served by its own index.
- Postings happen in the **same transaction** as the payment/refund status change.

## Alternatives considered
- **Debit/credit entry rows + application-level sum check** — the check lives only in code.
- **A stored procedure as the only way to write entries (app user has no INSERT privilege)** —
  strong, but moves business logic into SQL procedures that are harder to test and review.
- **A `balance` column on merchants** — fast reads, but one missed update silently corrupts it.

## Consequences
Balance reads cost two indexed `SUM`s — the first candidate for caching. The platform fee is not
returned on refunds, so a merchant's balance can go negative (tested).
