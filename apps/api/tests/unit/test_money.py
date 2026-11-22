import pytest

from payforge.domain.money import fee_for, format_minor, to_minor
from payforge.errors import ValidationFailed


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("1", 100),
        ("0.5", 50),
        ("0.05", 5),
        ("499", 49900),
        ("499.5", 49950),
        ("499.05", 49905),
        ("19.99", 1999),  # the float trap: int(19.99 * 100) is 1998
        ("1.15", 115),  # another one: 1.15 * 100 = 114.99999999999999
        ("500000", 50000000),
    ],
)
def test_to_minor(amount: str, expected: int) -> None:
    assert to_minor(amount) == expected


@pytest.mark.parametrize(
    "amount",
    [
        "",
        " 1",
        "1 ",
        "-1",
        "+1",
        "1.234",
        "1e3",
        "1,000",
        "1.",
        ".5",
        "abc",
        "1234567",
        "NaN",
        "١٢",
    ],
)
def test_to_minor_rejects(amount: str) -> None:
    with pytest.raises(ValidationFailed):
        to_minor(amount)


@pytest.mark.parametrize(
    ("minor", "expected"),
    [
        (0, "0.00"),
        (5, "0.05"),
        (50, "0.50"),
        (100, "1.00"),
        (49905, "499.05"),
        (50000000, "500000.00"),
        (-250, "-2.50"),
        (-5, "-0.05"),
    ],
)
def test_format_minor(minor: int, expected: str) -> None:
    assert format_minor(minor) == expected


def test_round_trip() -> None:
    for s in ["0.01", "1.10", "499.99", "123456.78"]:
        assert format_minor(to_minor(s)) == s


@pytest.mark.parametrize(
    ("amount", "bps", "expected"),
    [(10000, 200, 200), (49900, 200, 998), (125, 200, 3), (100, 200, 2), (12345, 0, 0)],
)
def test_fee_for(amount: int, bps: int, expected: int) -> None:
    assert fee_for(amount, bps) == expected
