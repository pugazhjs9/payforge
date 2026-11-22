# 0002. Store money as integer paise, never floats

**Status:** accepted

## Context
Binary floating point cannot represent most decimals: in Python `19.99 * 100` is
`1998.9999999999998`, so `int(19.99 * 100)` is `1998`. One lost paisa per payment is an
accounting bug.

## Decision
- Every amount inside PayForge is an **integer number of paise** (`*_minor` columns).
- API v1 accepts and returns **decimal strings** (`"499.00"`), converted with string parsing
  only (`to_minor` / `format_minor` in `payforge/domain/money.py`).
- The pattern is compiled with `re.ASCII`: without it, Python's `\d` also matches digits such
  as `"١٢"`, and `int("١٢") == 12` — a test caught this.
- Fees use integer maths and basis points: `(amount * bps + 5000) // 10000`.
- Limits (₹1.00 – ₹5,00,000.00) are enforced in code **and** by a `CHECK` constraint.

## Alternatives considered
- **JSON numbers (`499.5`)** — most clients parse them as floats.
- **`Decimal` + `DECIMAL(12,2)` columns** — correct, but integers are simpler, faster and what
  most payment APIs expose; a future API version can expose integer paise with no storage change.
