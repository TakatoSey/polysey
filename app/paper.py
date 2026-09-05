from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .polymarket import Book

FEE_RATES = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "geopolitics": Decimal("0"),
}


def fee_rate_for_title(title: str) -> Decimal:
    text = title.lower()
    for category, rate in FEE_RATES.items():
        if category in text:
            return rate
    return Decimal("0.05")


@dataclass(slots=True)
class Fill:
    shares: Decimal
    average_price: Decimal
    notional: Decimal
    fee: Decimal
    status: str
    reason: str | None = None


def _level_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    return shares * fee_rate * price * (Decimal(1) - price)


def execute_buy_fak_by_budget(
    book: Book,
    budget: Decimal,
    fee_rate: Decimal,
    reference_price: Decimal,
    slippage_bps: int,
) -> Fill:
    if budget <= 0 or reference_price <= 0:
        return Fill(
            Decimal(0), Decimal(0), Decimal(0), Decimal(0), "rejected", "non_positive_budget"
        )
    drift = Decimal(slippage_bps) / Decimal(10000)
    max_price = reference_price * (Decimal(1) + drift)
    eligible_asks = [(price, size) for price, size in book.asks if price <= max_price]
    if not eligible_asks:
        return Fill(
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(0),
            "rejected",
            "no_liquidity_within_slippage",
        )
    # The minimum constrains the submitted order, not the eventual partial
    # fill. A valid FAK order may match fewer shares than requested.
    if budget < book.min_order_size * eligible_asks[0][0]:
        return Fill(
            Decimal(0), Decimal(0), Decimal(0), Decimal(0), "rejected", "below_min_order_size"
        )
    remaining_budget = budget
    filled = Decimal(0)
    notional = Decimal(0)
    fee = Decimal(0)
    for price, size in eligible_asks:
        affordable = remaining_budget / price
        take = min(size, affordable)
        if take <= 0:
            continue
        level_notional = take * price
        filled += take
        notional += level_notional
        fee += _level_fee(take, price, fee_rate)
        remaining_budget -= level_notional
        if remaining_budget <= Decimal("0.00000001"):
            break
    if filled <= 0:
        return Fill(
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(0),
            "rejected",
            "no_liquidity_within_slippage",
        )
    average = (notional / filled).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    fee = fee.quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
    status = "filled" if remaining_budget <= Decimal("0.00000001") else "partial"
    return Fill(filled, average, notional, fee, status)


def execute_fak(
    book: Book,
    side: str,
    requested_shares: Decimal,
    fee_rate: Decimal,
    reference_price: Decimal | None = None,
    slippage_bps: int | None = None,
) -> Fill:
    levels = book.asks if side == "BUY" else book.bids
    if requested_shares <= 0:
        return Fill(Decimal(0), Decimal(0), Decimal(0), Decimal(0), "rejected", "non_positive_size")
    if requested_shares < book.min_order_size:
        return Fill(
            Decimal(0), Decimal(0), Decimal(0), Decimal(0), "rejected", "below_min_order_size"
        )
    max_price = min_price = None
    if reference_price is not None and slippage_bps is not None:
        drift = Decimal(slippage_bps) / Decimal(10000)
        max_price = reference_price * (Decimal(1) + drift)
        min_price = reference_price * (Decimal(1) - drift)
    remaining = requested_shares
    filled = Decimal(0)
    notional = Decimal(0)
    fee = Decimal(0)
    for price, size in levels:
        if side == "BUY" and max_price is not None and price > max_price:
            break
        if side == "SELL" and min_price is not None and price < min_price:
            break
        take = min(remaining, size)
        if take <= 0:
            continue
        filled += take
        notional += take * price
        fee += _level_fee(take, price, fee_rate)
        remaining -= take
        if remaining <= 0:
            break
    if filled <= 0:
        return Fill(Decimal(0), Decimal(0), Decimal(0), Decimal(0), "rejected", "no_liquidity")
    avg = (notional / filled).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    fee = fee.quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
    return Fill(filled, avg, notional, fee, "filled" if remaining == 0 else "partial")
