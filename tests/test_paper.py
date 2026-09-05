from decimal import Decimal

from app.paper import execute_buy_fak_by_budget, execute_fak
from app.polymarket import Book


def test_buy_consumes_asks_and_reports_partial_fill() -> None:
    book = Book(
        bids=[(Decimal("0.40"), Decimal("10"))],
        asks=[(Decimal("0.50"), Decimal("2")), (Decimal("0.51"), Decimal("1"))],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        neg_risk=False,
    )
    fill = execute_fak(book, "BUY", Decimal("5"), Decimal("0.05"))
    assert fill.status == "partial"
    assert fill.shares == Decimal("3")
    assert fill.average_price == Decimal("0.50333333")
    assert fill.fee > 0


def test_sell_without_liquidity_is_rejected() -> None:
    book = Book(
        bids=[], asks=[], tick_size=Decimal("0.01"), min_order_size=Decimal("1"), neg_risk=False
    )
    fill = execute_fak(book, "SELL", Decimal("1"), Decimal("0.05"))
    assert fill.status == "rejected"
    assert fill.reason == "no_liquidity"


def test_buy_respects_slippage_ceiling() -> None:
    book = Book(
        bids=[],
        asks=[(Decimal("0.52"), Decimal("10")), (Decimal("0.60"), Decimal("10"))],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        neg_risk=False,
    )
    fill = execute_fak(
        book,
        "BUY",
        Decimal("15"),
        Decimal("0.05"),
        reference_price=Decimal("0.50"),
        slippage_bps=500,
    )
    assert fill.status == "partial"
    assert fill.shares == Decimal("10")
    assert fill.average_price == Decimal("0.52000000")


def test_minimum_order_size_is_enforced() -> None:
    book = Book(
        bids=[],
        asks=[(Decimal("0.50"), Decimal("100"))],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        neg_risk=False,
    )
    fill = execute_fak(book, "BUY", Decimal("4"), Decimal("0.05"))
    assert fill.status == "rejected"
    assert fill.reason == "below_min_order_size"


def test_fixed_buy_spends_budget_at_actual_book_prices() -> None:
    book = Book(
        bids=[],
        asks=[(Decimal("0.50"), Decimal("4")), (Decimal("0.60"), Decimal("10"))],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        neg_risk=False,
    )
    fill = execute_buy_fak_by_budget(
        book,
        budget=Decimal("5"),
        fee_rate=Decimal("0.05"),
        reference_price=Decimal("0.55"),
        slippage_bps=1000,
    )
    assert fill.status == "filled"
    assert fill.notional.quantize(Decimal("0.00001")) == Decimal("5.00000")
    assert fill.shares > Decimal("8")


def test_valid_fak_order_may_partially_fill_below_exchange_order_minimum() -> None:
    book = Book(
        bids=[],
        asks=[(Decimal("0.37"), Decimal("2"))],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        neg_risk=False,
    )

    fill = execute_buy_fak_by_budget(
        book,
        budget=Decimal("1.85"),
        fee_rate=Decimal("0.03"),
        reference_price=Decimal("0.37"),
        slippage_bps=500,
    )

    assert fill.status == "partial"
    assert fill.shares == Decimal("2")
