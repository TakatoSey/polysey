"""How an order is executed: simulated locally, or sent to the exchange.

Both executors answer with the same `Fill`, so everything downstream — the
ledger, exits, risk rules, notifications — is identical in either mode.

The live executor deliberately reuses the paper simulation as its pre-flight
check against the same book snapshot. Every guard the paper mode applies (price
range, slippage, the market's minimum, liquidity) therefore applies before a
real order is signed, and the reasons a copy is skipped stay the same in both
modes. What the simulation says would have filled is then discarded: the fill
recorded is the one the exchange reports.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import structlog

from .paper import Fill, execute_buy_fak_by_budget, execute_fak
from .polymarket import Book
from .price_limits import DEFAULT_RANGE, PriceRange

log = structlog.get_logger(__name__)

ZERO = Decimal(0)
ONE = Decimal(1)


def rejected(reason: str) -> Fill:
    return Fill(ZERO, ZERO, ZERO, ZERO, "rejected", reason)


def snap_limit(price: Decimal, tick: Decimal, side: str) -> Decimal | None:
    """Put a price limit onto the market's tick grid, never past our promise.

    The exchange only quotes multiples of the tick, and the signing client
    rounds a price to the tick's precision in whichever direction is nearest —
    which for a buy can land above the limit our slippage rule allows. Snapping
    it down ourselves (up, for a sell floor) keeps the promise exact and cannot
    exclude a level that was within the limit, because levels sit on the grid.

    None when no valid price exists on that side: the exchange accepts only
    prices between one tick and one tick below a dollar.
    """
    if tick <= ZERO:
        return price
    steps = price / tick
    rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
    snapped = steps.to_integral_value(rounding=rounding) * tick
    if side == "BUY":
        snapped = min(snapped, ONE - tick)
        return snapped if snapped >= tick else None
    snapped = max(snapped, tick)
    return snapped if snapped <= ONE - tick else None


class PaperExecutor:
    """Simulated fills against a fresh REST book snapshot."""

    mode = "paper"
    live = False

    async def cancel_open_orders(self) -> str:
        """Nothing rests on an exchange in paper mode."""
        return "нет ордеров на бирже"

    async def buy(
        self,
        *,
        book: Book,
        token_id: str,
        budget: Decimal,
        fee_rate: Decimal,
        reference_price: Decimal,
        slippage_price: Decimal,
        price_range: PriceRange = DEFAULT_RANGE,
    ) -> Fill:
        return execute_buy_fak_by_budget(
            book,
            budget,
            fee_rate,
            reference_price=reference_price,
            slippage_price=slippage_price,
            price_range=price_range,
        )

    async def sell(
        self,
        *,
        book: Book,
        token_id: str,
        shares: Decimal,
        fee_rate: Decimal,
        reference_price: Decimal,
        slippage_price: Decimal,
        price_range: PriceRange = DEFAULT_RANGE,
    ) -> Fill:
        return execute_fak(
            book,
            "SELL",
            shares,
            fee_rate,
            reference_price=reference_price,
            slippage_price=slippage_price,
            price_range=price_range,
        )


class LiveExecutor:
    """Real orders. The exchange decides what filled, and at what price."""

    mode = "live"
    live = True

    def __init__(self, trader, settings, state=None):
        self.trader = trader
        self.settings = settings
        self.state = state

    async def prewarm(self, token_id: str) -> None:
        """Load what order creation would otherwise fetch mid-submit."""
        await self.trader.prewarm(token_id)

    async def cancel_open_orders(self) -> str:
        """Pull every order of ours off the exchange."""
        try:
            open_orders = await self.trader.open_orders()
            await self.trader.cancel_all()
        except Exception as exc:
            log.error("live_cancel_all_failed", error=str(exc)[:240])
            return f"не удалось снять ордера: {type(exc).__name__}"
        if self.state is not None:
            self.state.invalidate()
        return f"снято ордеров: {len(open_orders)}"

    async def buy(
        self,
        *,
        book: Book,
        token_id: str,
        budget: Decimal,
        fee_rate: Decimal,
        reference_price: Decimal,
        slippage_price: Decimal,
        price_range: PriceRange = DEFAULT_RANGE,
    ) -> Fill:
        preflight = execute_buy_fak_by_budget(
            book,
            budget,
            fee_rate,
            reference_price=reference_price,
            slippage_price=slippage_price,
            price_range=price_range,
        )
        if preflight.shares <= 0:
            return preflight
        if budget > self.settings.live_max_order_usdc:
            # Sizing already applies this cap; an order past it means a bug,
            # and a bug must not reach the exchange.
            log.error(
                "live_order_cap_exceeded",
                token_id=token_id,
                budget=str(budget),
                cap=str(self.settings.live_max_order_usdc),
            )
            return rejected("live_order_cap_exceeded")
        limit = snap_limit(
            min(price_range.maximum, reference_price + slippage_price), book.tick_size, "BUY"
        )
        if limit is None:
            return rejected("live_price_outside_tick_grid")
        order = await self.trader.buy(token_id, budget, limit, neg_risk=book.neg_risk or None)
        return self._fill(order, "BUY", token_id, budget)

    async def sell(
        self,
        *,
        book: Book,
        token_id: str,
        shares: Decimal,
        fee_rate: Decimal,
        reference_price: Decimal,
        slippage_price: Decimal,
        price_range: PriceRange = DEFAULT_RANGE,
    ) -> Fill:
        preflight = execute_fak(
            book,
            "SELL",
            shares,
            fee_rate,
            reference_price=reference_price,
            slippage_price=slippage_price,
            price_range=price_range,
        )
        if preflight.shares <= 0:
            return preflight
        limit = snap_limit(max(ZERO, reference_price - slippage_price), book.tick_size, "SELL")
        if limit is None:
            return rejected("live_price_outside_tick_grid")
        order = await self.trader.sell(token_id, shares, limit, neg_risk=book.neg_risk or None)
        return self._fill(order, "SELL", token_id, shares)

    def _fill(self, order, side: str, token_id: str, requested: Decimal) -> Fill:
        """Translate one exchange answer into the ledger's fill, or a skip."""
        if not order.submitted:
            return rejected(order.error or "live_order_not_submitted")
        if not order.confirmed:
            # Shares may or may not exist. Recording either guess would be a
            # lie; reconciliation against the exchange reports the truth.
            log.error(
                "live_order_result_unknown",
                side=side,
                token_id=token_id,
                order_id=order.order_id,
                status=order.status,
            )
            if self.state is not None:
                self.state.invalidate()
            return rejected(f"live_unconfirmed:{order.order_id}"[:240])
        if order.shares <= 0:
            return rejected(f"live_unfilled:{order.status or 'no_match'}"[:240])
        if self.state is not None:
            if side == "BUY":
                self.state.note_buy(token_id, order.notional + order.fee, order.shares)
            else:
                self.state.note_sell(token_id, order.notional - order.fee, order.shares)
        complete = (
            order.notional >= requested - Decimal("0.01")
            if side == "BUY"
            else order.shares >= requested - Decimal("0.000001")
        )
        return Fill(
            shares=order.shares,
            average_price=order.average_price,
            notional=order.notional,
            fee=order.fee,
            status="filled" if complete else "partial",
            reason=None,
        )
