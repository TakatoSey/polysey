"""User-requested inclusive entry range. Exits and payouts are unrestricted."""

from decimal import Decimal

MIN_BUY_PRICE = Decimal("0.02")
MAX_BUY_PRICE = Decimal("0.98")


def allowed_buy_price(price: Decimal) -> bool:
    return price.is_finite() and MIN_BUY_PRICE <= price <= MAX_BUY_PRICE
