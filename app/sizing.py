"""Capital allocation heuristics, not leader equity or win-probability estimates."""

from dataclasses import dataclass
from decimal import Decimal

from .price_limits import allowed_buy_price

ZERO = Decimal(0)
ONE = Decimal(1)


def entry_bucket(timestamp: int, seconds: int) -> int:
    return timestamp // seconds * seconds


@dataclass(frozen=True)
class EntrySample:
    reference_notional: Decimal
    sample_count: int
    sample_start: int
    sample_end: int


def sample_entries(
    activities, *, before: int, seconds: int, min_samples: int
) -> EntrySample | None:
    """Same fixed buckets as execution; exclude open buckets and BUY/SELL mixtures.

    Activity is already fetched for detection. No extra HTTP request or future
    fills are needed. Last seven days of the available API window, not a claim
    that we have every trade. Trim 10% of both tails when >=10 entries exist.
    """
    groups = {}
    blocked = set()
    seen = set()
    # A capped API response may begin halfway through a fragmented entry.
    oldest = min((event.timestamp for event in activities), default=0)
    truncated_bucket = entry_bucket(oldest, seconds) if len(activities) >= 500 else None
    for event in activities:
        if event.event_key in seen:
            continue
        seen.add(event.event_key)
        start = entry_bucket(event.timestamp, seconds)
        if start == truncated_bucket:
            continue
        if start + seconds > before or start < before - 7 * 86400:
            continue
        key = (event.token_id, start)
        if event.side == "SELL":
            blocked.add(key)
        elif (
            event.side == "BUY"
            and event.size.is_finite()
            and event.price.is_finite()
            and event.size > 0
            and ZERO < event.price < ONE
        ):
            groups[key] = groups.get(key, ZERO) + event.size * event.price
    groups = {key: value for key, value in groups.items() if key not in blocked}
    if len(groups) < min_samples:
        return None
    ordered = sorted(groups.values())
    trim = len(ordered) // 10
    middle = ordered[trim : len(ordered) - trim] if trim else ordered
    return EntrySample(
        sum(middle, ZERO) / len(middle),
        len(ordered),
        min(key[1] for key in groups),
        max(key[1] for key in groups) + seconds,
    )


@dataclass(frozen=True)
class BudgetDecision:
    leader_vwap: Decimal
    price_factor: Decimal
    odds_factor: Decimal
    target_budget: Decimal
    order_budget: Decimal  # notional, fee reserve already removed
    reference_price: Decimal
    reason: str | None = None


def entry_budget(
    entry,
    *,
    ask: Decimal,
    event_price: Decimal,
    cash: Decimal,
    exposure_room: Decimal,
    current_max: Decimal,
    fee_rate: Decimal,
    slippage_price: Decimal | None = None,
    slippage_bps: int | None = None,
    min_notional: Decimal,
    min_shares: Decimal,
) -> BudgetDecision:
    """Cumulative all-in target using size, odds and actual prior cash debits."""
    vwap = entry.leader_notional / entry.leader_shares
    reference = min(vwap, event_price)
    price_factor = min(ONE, vwap / ask) if ask > 0 else ZERO
    # Contract price is not a verified probability, so it only nudges size:
    # 10c -> 0.68x, 50c -> 1.00x, 70c -> 1.16x, 98c -> 1.38x.
    odds_factor = min(
        Decimal("1.40"), max(Decimal("0.60"), Decimal("0.60") + vwap * Decimal("0.80"))
    )
    reserve = ONE + max(ZERO, fee_rate) * (ONE - ask)
    cap = min(entry.max_budget, current_max)
    fixed = entry.max_multiplier == ZERO
    if fixed:
        target = cap
        odds_factor = ONE
    else:
        intensity = min(entry.max_multiplier, entry.leader_notional / entry.reference_notional)
        raw_target = entry.base_budget * intensity * price_factor * odds_factor
        # A meaningful small leader entry receives our executable minimum; dust
        # does not get inflated. Further fragments keep one cumulative target.
        meaningful = entry.leader_notional >= max(
            min_notional, entry.reference_notional * Decimal("0.20")
        )
        minimum_all_in = max(min_notional, min_shares * ask) * reserve if meaningful else ZERO
        target = min(cap, max(raw_target, minimum_all_in))
    remaining = max(ZERO, target - entry.spent)
    # _level_fee / notional = rate * (1-price). At >= ask this is an upper bound.
    budget = max(ZERO, min(remaining, cash, exposure_room)) / reserve
    reason = None
    distance = (
        slippage_price
        if slippage_price is not None
        else reference * Decimal(slippage_bps or 0) / 10000
    )
    if entry.closed:
        reason = "sizing_entry_closed"
    elif not allowed_buy_price(event_price) or not allowed_buy_price(vwap):
        reason = "leader_price_out_of_range"
    elif ask <= 0:
        reason = "no_liquidity"
    elif not allowed_buy_price(ask):
        reason = "buy_price_out_of_range"
    elif ask > reference + distance:
        reason = "no_liquidity_within_slippage"
    elif slippage_price is not None and ask < max(vwap, event_price) - distance:
        reason = "entry_price_drop"
    elif remaining <= 0:
        reason = "sizing_entry_budget_used"
    elif exposure_room <= 0:
        reason = "sizing_exposure_limit"
    elif budget < max(min_notional, min_shares * ask):
        reason = "sizing_below_minimum"
    return BudgetDecision(vwap, price_factor, odds_factor, target, budget, reference, reason)
