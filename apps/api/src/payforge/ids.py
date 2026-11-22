from ulid import ULID


def new_id(prefix: str) -> str:
    """new_id("pay") -> "pay_01JD3W7K8Y8M0F2T6QH5RJZC4N".

    The prefix makes IDs readable in logs; a ULID sorts by creation time and has
    80 random bits, so IDs cannot be guessed by counting.
    """
    return f"{prefix}_{ULID()}"
