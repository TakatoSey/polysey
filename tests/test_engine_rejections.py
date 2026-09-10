from decimal import Decimal
from types import SimpleNamespace

from app.config import Settings
from app.engine import CopyEngine
from app.models import CopyTrade, PaperOrder
from app.paper import Fill
from app.polymarket import Book, LeaderActivity


class RecordingSession:
    def __init__(self):
        self.items = []

    def add(self, item):
        self.items.append(item)


def test_skipped_copy_is_always_visible_as_rejected_order():
    session = RecordingSession()
    trade = CopyTrade(id=7)
    event = LeaderActivity(
        event_key="event",
        timestamp=1,
        condition_id="condition",
        token_id="token",
        side="BUY",
        size=Decimal("10"),
        price=Decimal("0.5"),
        title="Market",
        outcome="Yes",
        slug="market",
    )

    CopyEngine.record_rejection(session, trade, event, "below_min_copy_notional")

    assert len(session.items) == 1
    order = session.items[0]
    assert isinstance(order, PaperOrder)
    assert order.copy_trade_id == 7
    assert order.status == "rejected"
    assert order.reason == "below_min_copy_notional"


def test_small_leader_buy_is_raised_to_executable_minimum():
    account = SimpleNamespace(
        paper_balance=Decimal("100"),
        trade_size=Decimal("5"),
        max_trade_size=Decimal("30"),
    )
    settings = Settings(
        _env_file=None,
        COPY_BALANCE_PCT="0.05",
        LEADER_ORDER_SCALE="0.10",
        MIN_COPY_NOTIONAL="1.10",
    )

    budget = CopyEngine.calculate_buy_budget(
        account,
        settings,
        leader_notional=Decimal("7.041745"),
        fee_rate=Decimal("0.03"),
    )

    assert budget == Decimal("1.10")


def test_sizing_never_forces_minimum_when_our_cash_budget_is_too_small():
    account = SimpleNamespace(
        paper_balance=Decimal("1.76"),
        trade_size=Decimal("5"),
        max_trade_size=Decimal("30"),
    )
    settings = Settings(_env_file=None, COPY_BALANCE_PCT="0.05", MIN_COPY_NOTIONAL="1.10")

    budget = CopyEngine.calculate_buy_budget(
        account,
        settings,
        leader_notional=Decimal("100"),
        fee_rate=Decimal("0.03"),
    )

    assert budget < settings.min_copy_notional


def test_small_copy_is_raised_to_exchange_share_minimum_when_affordable():
    book = Book(
        bids=[],
        asks=[(Decimal("0.37"), Decimal("100"))],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        neg_risk=False,
    )

    budget = CopyEngine.ensure_book_minimum_budget(
        budget=Decimal("1.10"),
        own_capacity=Decimal("5"),
        book=book,
        reference_price=Decimal("0.37"),
        slippage_bps=500,
    )

    assert budget == Decimal("1.85")


def test_buy_notification_contains_outcome_link_amount_and_shares():
    leader = SimpleNamespace(address="0x09b045baad1fbe115c70785635a261411774a3b6", label=None)
    event = LeaderActivity(
        event_key="event",
        timestamp=1,
        condition_id="condition",
        token_id="token",
        side="BUY",
        size=Decimal("10"),
        price=Decimal("0.5"),
        title="Will A & B win?",
        outcome="Yes",
        slug="market",
        trader_name="blackewolf83",
    )
    fill = Fill(
        shares=Decimal("5"),
        average_price=Decimal("0.37"),
        notional=Decimal("1.85"),
        fee=Decimal("0.01"),
        status="filled",
    )

    message = CopyEngine.build_buy_notification(leader, event, fill)

    assert "Will A &amp; B win?" in message
    assert "<b>Position:</b> Yes" in message
    assert ">@blackewolf83</a>" in message
    assert "https://polymarket.com/profile/0x09b045" in message
    assert "<b>Leader bought:</b> $5.00 (10.00 shares)" in message
    assert "<b>You bought:</b> $1.85 (5.00 shares)" in message
    assert "<b>Entry Price:</b> 37.0¢" in message
    assert "Fee" not in message


def test_market_title_carries_the_polymarket_link_in_buy_and_settlement():
    event = LeaderActivity(
        event_key="event",
        timestamp=1,
        condition_id="condition",
        token_id="token",
        side="BUY",
        size=Decimal(10),
        price=Decimal("0.5"),
        title="Bitcoin Up or Down",
        outcome="Up",
        slug="btc-updown-15m",
        event_slug="btc-series",
    )
    fill = Fill(
        shares=Decimal(5),
        average_price=Decimal("0.5"),
        notional=Decimal("2.5"),
        fee=Decimal(0),
        status="filled",
    )
    leader = SimpleNamespace(address="0x" + "a" * 40, label=None)

    buy = CopyEngine.build_buy_notification(leader, event, fill)
    assert '<a href="https://polymarket.com/event/btc-series/btc-updown-15m">Bitcoin' in buy
    assert "View on Polymarket" not in buy

    settled = CopyEngine.build_settlement_notification(
        event.title, "Up", Decimal(5), Decimal(1), Decimal("2.5"), event.slug, event.event_slug
    )
    assert '<a href="https://polymarket.com/event/btc-series/btc-updown-15m">Bitcoin' in settled
    # The link lives in the title now, so PnL closes the block.
    assert "View on Polymarket" not in settled
    assert "  └ PnL:" in settled


def test_a_market_without_a_stored_slug_stays_plain_text():
    plain = CopyEngine.build_settlement_notification(
        "Unknown market", "Yes", Decimal(5), Decimal(1), Decimal(1)
    )
    assert "<a href" not in plain
    assert "Unknown market" in plain
