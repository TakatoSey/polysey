"""Numbers as a person reads them: no trailing zeros, no exponents.

A Decimal keeps the scale it was stored with, and "g" formatting keeps that
scale too, so a NUMERIC(20, 10) column renders as 1.0000000000 and a scaled
zero as 0e-10. These helpers drop the noise without going through float.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def plain(value: Decimal | None, places: int = 4) -> str:
    """At most `places` decimals, trailing zeros and exponents removed."""
    if value is None:
        return "—"
    try:
        if not value.is_finite():
            return "—"
        rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP).normalize()
    except (InvalidOperation, AttributeError, ValueError):
        return str(value)
    # normalize() renders 100 as 1E+2; "f" prints it as a plain number again.
    return f"{rounded:f}"


def cents(price: Decimal | None, places: int = 2) -> str:
    """A price as cents: 0.02 -> "2", 0.005 -> "0.5", 1 -> "100"."""
    if price is None:
        return "—"
    return plain(price * 100, places)


def percent(value: Decimal | None) -> str:
    """A stored percentage: Decimal("5.0000") -> "5"."""
    return plain(value, 2)
