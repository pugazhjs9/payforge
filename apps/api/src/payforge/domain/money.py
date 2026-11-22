"""Money is always an integer number of paise inside PayForge.

Binary floating point can't represent most decimals exactly (19.99 * 100 is
1998.9999999999998), so amounts are converted with string parsing only.
"""

import re

from payforge.errors import ValidationFailed

# re.ASCII: in Python, \d also matches digits like "١٢", and int("١٢") == 12
AMOUNT_PATTERN = re.compile(r"(\d{1,6})(?:\.(\d{1,2}))?", re.ASCII)

MIN_AMOUNT_MINOR = 100  # ₹1.00
MAX_AMOUNT_MINOR = 50_000_000  # ₹5,00,000.00


def to_minor(amount: str) -> int:
    """'499' -> 49900, '499.5' -> 49950, '499.05' -> 49905."""
    match = AMOUNT_PATTERN.fullmatch(amount)
    if not match:
        raise ValidationFailed("amount", 'must be a string like "499" or "499.50"')
    rupees = int(match.group(1))
    paise = int((match.group(2) or "").ljust(2, "0"))  # "5" -> 50, "" -> 0
    return rupees * 100 + paise


def format_minor(minor: int) -> str:
    """49905 -> '499.05', -250 -> '-2.50' (balances can be negative)."""
    sign = "-" if minor < 0 else ""
    rupees, paise = divmod(abs(minor), 100)
    return f"{sign}{rupees}.{paise:02d}"


def fee_for(amount_minor: int, fee_bps: int) -> int:
    """Platform fee in paise, rounded to the nearest paisa with integers only.

    fee_bps is in basis points: 200 = 2%.
    """
    return (amount_minor * fee_bps + 5_000) // 10_000
