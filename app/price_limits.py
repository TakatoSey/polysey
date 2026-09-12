"""Inclusive entry range for BUY. Exits, stops and payouts are unrestricted.

2-98c is the default for every leader. A leader may override it (some trade
edges the default deliberately excludes), and the override applies only to
that leader's own copies.
"""

from dataclasses import dataclass
from decimal import Decimal

from .formatting import cents

ZERO = Decimal(0)
ONE = Decimal(1)
MIN_BUY_PRICE = Decimal("0.02")
MAX_BUY_PRICE = Decimal("0.98")


@dataclass(frozen=True)
class PriceRange:
    minimum: Decimal = MIN_BUY_PRICE
    maximum: Decimal = MAX_BUY_PRICE

    def allows(self, price: Decimal) -> bool:
        return price.is_finite() and self.minimum <= price <= self.maximum

    @property
    def overridden(self) -> bool:
        return (self.minimum, self.maximum) != (MIN_BUY_PRICE, MAX_BUY_PRICE)

    @property
    def label(self) -> str:
        return f"{cents(self.minimum)}–{cents(self.maximum)}¢"


DEFAULT_RANGE = PriceRange()


def allowed_buy_price(price: Decimal) -> bool:
    """The default range. Per-leader execution uses that leader's own range."""
    return DEFAULT_RANGE.allows(price)


def _bound(value: Decimal | None, fallback: Decimal) -> Decimal:
    if value is None or not value.is_finite() or not ZERO <= value <= ONE:
        return fallback
    return value


def leader_price_range(leader) -> PriceRange:
    """This leader's stored range, falling back to the default bound by bound.

    A stored pair that is not a range at all is ignored rather than widened
    into one: an unusable setting must not buy at prices nobody asked for.
    """
    minimum = _bound(getattr(leader, "min_buy_price", None), MIN_BUY_PRICE)
    maximum = _bound(getattr(leader, "max_buy_price", None), MAX_BUY_PRICE)
    if minimum > maximum:
        return DEFAULT_RANGE
    return PriceRange(minimum, maximum)
