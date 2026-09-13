"""Live trading against a stand-in exchange.

The real exchange cannot be reached from the test suite, so these tests pin the
contract this bot relies on: what is sent, what is believed, and above all what
is never invented when the exchange does not answer clearly.
"""

import json
import time
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_sizing import copy as copy_trade
from test_sizing import sizing_rig as _sizing_rig

from app.config import Settings
from app.executor import LiveExecutor, snap_limit
from app.live import LiveOrder, LivePosition, LiveTrader
from app.live_state import LiveState, Snapshot, utc_day
from app.models import Account, CopyTrade, DailyRisk, PaperOrder, Position
from app.polymarket import Book

sizing_rig = _sizing_rig

WALLET = "0x" + "1" * 40


def live_settings(**overrides):
    values = {
        "TRADING_MODE": "live",
        "LIVE_CONFIRM": "I_UNDERSTAND_REAL_MONEY",
        "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
        "POLYMARKET_FUNDER": WALLET,
        "LIVE_MAX_ORDER_USDC": "25",
        "LIVE_STATE_REFRESH_SECONDS": "5",
        "LIVE_DRIFT_TOLERANCE_USDC": "1",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeClob:
    """Only the calls this bot makes, recording what it was asked to do."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []
        self.posted = []

    def get_address(self):
        return WALLET

    def get_balance_allowance(self, params):
        self.calls.append(("balance", getattr(params, "token_id", None)))
        return self.answers.get("balance", {"balance": "40000000", "allowance": "40000000"})

    def get_tick_size(self, token_id):
        self.calls.append(("tick", token_id))
        return "0.01"

    def get_neg_risk(self, token_id):
        self.calls.append(("neg_risk", token_id))
        return False

    def get_fee_rate_bps(self, token_id):
        self.calls.append(("fee", token_id))
        return 0

    def create_market_order(self, args, options=None):
        self.calls.append(("create", args.side, args.token_id, args.amount, args.price))
        return SimpleNamespace(order="signed", args=args)

    def post_order(self, order, order_type=None):
        self.posted.append((order, order_type))
        answer = self.answers.get("post", {"success": True, "orderID": "0xorder"})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return self.answers.get("order", {})

    def get_trades(self, params=None):
        self.calls.append(("trades", getattr(params, "id", None)))
        return self.answers.get("trades", [])

    def get_orders(self):
        return self.answers.get("open_orders", [])

    def cancel_all(self):
        self.calls.append(("cancel_all", None))
        return {"canceled": []}


async def trader_for(monkeypatch, **answers):
    clob = FakeClob(**answers)
    monkeypatch.setattr("app.live.build_client", lambda settings: clob)
    trader = LiveTrader(live_settings(**answers.pop("settings", {})))
    await trader.start()
    return trader, clob


# --------------------------------------------------------------- what is sent


async def test_a_buy_sends_the_budget_in_usdc_and_a_price_limit(monkeypatch):
    trader, clob = await trader_for(
        monkeypatch,
        post={
            "success": True,
            "orderID": "0xabc",
            "status": "matched",
            "makingAmount": "5.00",
            "takingAmount": "10",
        },
    )
    order = await trader.buy("token", D("5"), D("0.55"))

    assert ("create", "BUY", "token", 5.0, 0.55) in clob.calls
    assert clob.posted and clob.posted[0][1] == "FAK"
    # Maker pays, taker receives: our buy pays USDC and receives shares.
    assert (order.shares, order.notional) == (D(10), D("5.000000"))
    assert order.average_price == D("0.5")
    assert order.confirmed is True


async def test_a_sell_sends_shares_and_a_floor_price(monkeypatch):
    trader, clob = await trader_for(
        monkeypatch,
        post={
            "success": True,
            "orderID": "0xdef",
            "status": "matched",
            "makingAmount": "10",
            "takingAmount": "4.50",
        },
    )
    order = await trader.sell("token", D(10), D("0.40"))

    assert ("create", "SELL", "token", 10.0, 0.4) in clob.calls
    assert (order.shares, order.notional) == (D(10), D("4.500000"))
    assert order.average_price == D("0.45")


@pytest.mark.parametrize(
    "amount, price",
    [(D(0), D("0.5")), (D(-1), D("0.5")), (D(5), D(0)), (D(5), D(1)), (D(5), D("1.5"))],
)
async def test_an_impossible_order_never_reaches_the_exchange(monkeypatch, amount, price):
    trader, clob = await trader_for(monkeypatch)

    order = await trader.buy("token", amount, price)

    assert order.submitted is False
    assert clob.posted == []


async def test_dry_run_signs_without_sending(monkeypatch):
    trader, clob = await trader_for(monkeypatch, settings={"LIVE_DRY_RUN": "true"})

    order = await trader.buy("token", D(5), D("0.5"))

    assert order.submitted is False
    assert order.status == "dry_run"
    assert any(call[0] == "create" for call in clob.calls)
    assert clob.posted == []


async def test_prewarm_loads_what_the_submit_path_would_fetch(monkeypatch):
    trader, clob = await trader_for(monkeypatch)

    await trader.prewarm("token")
    warmed = [call for call in clob.calls if call[0] in {"tick", "neg_risk", "fee"}]
    await trader.prewarm("token")

    assert {call[0] for call in warmed} == {"tick", "neg_risk", "fee"}
    # Warm once per token, not on every order.
    assert [call for call in clob.calls if call[0] in {"tick", "neg_risk", "fee"}] == warmed


# ------------------------------------------------------- what is never invented


async def test_a_rejected_order_is_reported_not_treated_as_filled(monkeypatch):
    trader, _clob = await trader_for(
        monkeypatch, post={"success": False, "errorMsg": "not enough balance"}
    )

    order = await trader.buy("token", D(5), D("0.5"))

    assert (order.submitted, order.shares) == (False, D(0))
    assert "not enough balance" in order.error


async def test_a_transport_failure_is_not_a_fill(monkeypatch):
    trader, _clob = await trader_for(monkeypatch, post=RuntimeError("connection reset"))

    order = await trader.buy("token", D(5), D("0.5"))

    assert order.submitted is False
    assert "connection reset" in order.error


async def test_an_accepted_order_without_amounts_is_confirmed_from_the_exchange(monkeypatch):
    trader, clob = await trader_for(
        monkeypatch,
        post={"success": True, "orderID": "0xabc", "status": "live"},
        order={"status": "MATCHED", "size_matched": "8", "price": "0.55"},
        trades=[{"size": "5", "price": "0.50", "fee": "0.01"}, {"size": "3", "price": "0.60"}],
    )

    order = await trader.buy("token", D(5), D("0.60"))

    assert ("get_order", "0xabc") in clob.calls
    assert order.confirmed is True
    assert order.shares == D(8)
    # Size-weighted across the trades behind the order, not the limit price.
    assert order.average_price == D("0.5375")
    assert order.fee == D("0.010000")


async def test_an_unmatched_order_is_confirmed_as_zero(monkeypatch):
    trader, _clob = await trader_for(
        monkeypatch,
        post={"success": True, "orderID": "0xabc", "status": "live"},
        order={"status": "UNMATCHED", "size_matched": "0"},
    )

    order = await trader.buy("token", D(5), D("0.5"))

    assert (order.confirmed, order.shares) == (True, D(0))


async def test_a_fill_is_never_recorded_at_a_price_nobody_quoted(monkeypatch):
    trader, _clob = await trader_for(
        monkeypatch,
        post={"success": True, "orderID": "0xabc", "status": "live"},
        # Matched, but neither the trades nor the order carry a price.
        order={"status": "MATCHED", "size_matched": "8"},
        trades=[],
    )

    order = await trader.buy("token", D(5), D("0.5"))

    assert order.confirmed is False
    assert order.average_price == D(0)


async def test_an_order_that_stays_unknown_is_left_unconfirmed(monkeypatch):
    trader, _clob = await trader_for(
        monkeypatch,
        post={"success": True, "orderID": "0xabc", "status": "live"},
        order={"status": "LIVE"},
    )
    trader.CONFIRM_DELAY = 0

    order = await trader.buy("token", D(5), D("0.5"))

    assert order.submitted is True
    assert order.confirmed is False
    assert order.shares == D(0)


# --------------------------------------------------------------- exchange state


async def test_usdc_is_read_in_its_six_decimal_units(monkeypatch):
    trader, _clob = await trader_for(
        monkeypatch, balance={"balance": "12500000", "allowance": "1000000000"}
    )

    assert await trader.cash() == D("12.5")
    assert trader.account.cash == D("12.5")
    assert trader.account.funder == WALLET


async def test_positions_come_from_polymarket_not_from_our_ledger(monkeypatch):
    trader, _clob = await trader_for(monkeypatch)
    payload = [
        {"asset": "token-a", "size": "12.5", "avgPrice": "0.4", "conditionId": "c1"},
        {"asset": "token-b", "size": "0", "avgPrice": "0.9"},
        {"nonsense": True},
    ]

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return payload

    asked = {}

    class Http:
        @staticmethod
        async def get(url, params=None):
            asked.update(url=url, params=params)
            return Response()

    rows = await trader.positions(Http(), "https://data-api.example")

    assert asked["params"]["user"] == WALLET
    assert [(row.token_id, row.shares) for row in rows] == [("token-a", D("12.5"))]


# ------------------------------------------------- projection over exchange truth


class FakeTrader:
    """A LiveTrader stand-in: whatever the exchange is told to have done."""

    def __init__(self, *, cash=D(40), positions=(), order=None, sell_order=None):
        self._cash = cash
        self._positions = list(positions)
        self.order = order
        self.sell_order = sell_order
        self.sent = []
        self.prewarmed = []
        self.cancelled = 0

    async def cash(self):
        return self._cash

    async def positions(self, http, data_api):
        return self._positions

    async def prewarm(self, token_id):
        self.prewarmed.append(token_id)

    async def buy(self, token_id, usdc, price_limit, neg_risk=None):
        self.sent.append(("BUY", token_id, usdc, price_limit))
        return self.order or LiveOrder(
            submitted=True,
            order_id="0x1",
            status="matched",
            shares=(usdc / price_limit),
            notional=usdc,
            average_price=price_limit,
            confirmed=True,
        )

    async def sell(self, token_id, shares, price_limit, neg_risk=None):
        self.sent.append(("SELL", token_id, shares, price_limit))
        return self.sell_order or LiveOrder(
            submitted=True,
            order_id="0x2",
            status="matched",
            shares=shares,
            notional=shares * price_limit,
            average_price=price_limit,
            confirmed=True,
        )

    async def open_orders(self):
        return [{"id": "0x9"}]

    async def cancel_all(self):
        self.cancelled += 1
        return {"canceled": ["0x9"]}


def snapshot_for(cash=D(40), positions=None, ok=True, age=0.0):
    return Snapshot(
        cash=cash,
        positions=dict(positions or {}),
        read_at=time.monotonic() - age if ok else 0.0,
        read_ok=ok,
    )


def go_live(rig, trader, snapshot=None, **settings):
    state = LiveState(trader, None, live_settings(**settings))
    state.snapshot = snapshot or snapshot_for()
    rig.engine.executor = LiveExecutor(trader, live_settings(**settings), state)
    rig.engine.live_state = state
    return state


def test_cash_and_shares_are_the_exchange_reading_minus_our_own_spend():
    snapshot = snapshot_for(cash=D(40), positions={"token": D(10)})

    snapshot.spent_since = D("5.50")
    snapshot.shares_since["token"] = D(11)

    assert snapshot.cash_now == D("34.50")
    assert snapshot.shares_now("token") == D(21)
    assert snapshot.shares_now("other") == D(0)
    # A projection never goes negative just because the reading is behind.
    snapshot.spent_since = D(100)
    assert snapshot.cash_now == D(0)


async def test_a_refresh_keeps_spend_that_happened_while_it_was_in_flight():
    trader = FakeTrader(cash=D(40), positions=[LivePosition("token", D(10), D("0.5"))])
    state = LiveState(trader, None, live_settings())
    await state.refresh()
    state.note_buy("token", D(5), D(10))

    async def spend_during_the_read():
        # The exchange answers with a balance that predates this buy.
        state.note_buy("token", D(3), D(6))
        return D(35)

    trader.cash = spend_during_the_read
    await state.refresh()

    # The $3 spent during the read survives it; the $5 before it does not,
    # because the exchange's new reading already accounts for that one.
    assert state.snapshot.spent_since == D(3)
    assert state.snapshot.cash_now == D(32)


@pytest.mark.parametrize(
    "snapshot, ledger_cash, expected",
    [
        (snapshot_for(ok=False), D(40), "live_state_unavailable"),
        (snapshot_for(age=600), D(40), "live_state_stale"),
        (snapshot_for(cash=D(40)), D(80), "live_ledger_drift"),
        (snapshot_for(cash=D(40)), D("40.50"), None),
    ],
)
def test_entries_stop_when_the_numbers_cannot_be_vouched_for(snapshot, ledger_cash, expected):
    state = LiveState(FakeTrader(), None, live_settings())
    state.snapshot = snapshot

    assert state.entry_block(ledger_cash) == expected


# ----------------------------------------------------------- the executor's rules


async def test_the_executor_sends_the_budget_and_a_slippage_bounded_limit():
    trader = FakeTrader()
    state = LiveState(trader, None, live_settings())
    state.snapshot = snapshot_for()
    executor = LiveExecutor(trader, live_settings(), state)
    book = Book(
        bids=[(D("0.49"), D(1000))],
        asks=[(D("0.50"), D(1000))],
        tick_size=D("0.01"),
        min_order_size=D(1),
        neg_risk=False,
    )

    fill = await executor.buy(
        book=book,
        token_id="token",
        budget=D(5),
        fee_rate=D(0),
        reference_price=D("0.50"),
        slippage_price=D("0.05"),
    )

    assert trader.sent == [("BUY", "token", D(5), D("0.55"))]
    assert fill.status == "filled"
    assert fill.shares == D(5) / D("0.55")
    # Our own spend is projected onto the last exchange reading straight away.
    assert state.snapshot.spent_since == D(5)


@pytest.mark.parametrize(
    "book_asks, budget, reason",
    [
        ([], D(5), "no_liquidity"),
        ([(D("0.70"), D(100))], D(5), "no_liquidity_within_slippage"),
        ([(D("0.99"), D(100))], D(5), "buy_price_out_of_range"),
        ([(D("0.50"), D(100))], D("0.10"), "below_min_order_size"),
        ([(D("0.50"), D(100))], D(100), "live_order_cap_exceeded"),
    ],
)
async def test_nothing_reaches_the_exchange_that_paper_mode_would_have_skipped(
    book_asks, budget, reason
):
    trader = FakeTrader()
    executor = LiveExecutor(trader, live_settings(), None)
    book = Book(
        bids=[(D("0.49"), D(1000))],
        asks=book_asks,
        tick_size=D("0.01"),
        min_order_size=D(5),
        neg_risk=False,
    )

    fill = await executor.buy(
        book=book,
        token_id="token",
        budget=budget,
        fee_rate=D(0),
        reference_price=D("0.50"),
        slippage_price=D("0.05"),
    )

    assert fill.shares == D(0)
    assert fill.reason == reason
    assert trader.sent == []


async def test_an_unconfirmed_order_is_not_recorded_as_a_fill():
    trader = FakeTrader(
        order=LiveOrder(submitted=True, order_id="0xabc", status="live", confirmed=False)
    )
    state = LiveState(trader, None, live_settings())
    state.snapshot = snapshot_for()
    executor = LiveExecutor(trader, live_settings(), state)
    book = Book(
        bids=[(D("0.49"), D(1000))],
        asks=[(D("0.50"), D(1000))],
        tick_size=D("0.01"),
        min_order_size=D(1),
        neg_risk=False,
    )

    fill = await executor.buy(
        book=book,
        token_id="token",
        budget=D(5),
        fee_rate=D(0),
        reference_price=D("0.50"),
        slippage_price=D("0.05"),
    )

    assert fill.shares == D(0)
    assert fill.reason == "live_unconfirmed:0xabc"
    assert state.snapshot.spent_since == D(0)


@pytest.mark.parametrize(
    "price, tick, side, expected",
    [
        ("0.487", "0.01", "BUY", D("0.48")),  # never above what slippage allows
        ("0.55", "0.01", "BUY", D("0.55")),
        ("0.4875", "0.001", "BUY", D("0.487")),
        ("0.995", "0.01", "BUY", D("0.99")),  # a dollar is not a tradeable price
        ("0.005", "0.01", "BUY", None),  # no valid price this low exists
        ("0.432", "0.01", "SELL", D("0.44")),  # never below our floor
        ("0.005", "0.01", "SELL", D("0.01")),
        ("0.999", "0.01", "SELL", None),
    ],
)
def test_a_price_limit_lands_on_the_tick_grid_without_breaking_its_promise(
    price, tick, side, expected
):
    assert snap_limit(D(price), D(tick), side) == expected


async def test_an_off_grid_slippage_limit_is_rounded_in_our_favour():
    trader = FakeTrader()
    executor = LiveExecutor(trader, live_settings(), None)
    book = Book(
        bids=[(D("0.43"), D(1000))],
        asks=[(D("0.44"), D(1000))],
        tick_size=D("0.01"),
        min_order_size=D(1),
        neg_risk=False,
    )

    await executor.buy(
        book=book,
        token_id="token",
        budget=D(5),
        fee_rate=D(0),
        reference_price=D("0.437"),
        slippage_price=D("0.05"),
    )

    # 0.437 + 0.05 = 0.487, which is not a price the exchange quotes. The
    # signing client would round it to 0.49, above what we allowed.
    assert trader.sent == [("BUY", "token", D(5), D("0.48"))]


# --------------------------------------------------------- the engine, live


async def test_sizing_uses_the_exchange_balance_not_the_ledgers(sizing_rig):
    trader = FakeTrader(cash=D(40))
    go_live(sizing_rig, trader)

    await sizing_rig.engine.sync_live_state_once()
    async with sizing_rig.sessions() as session:
        # The ledger's own $100 is replaced by what the exchange reports.
        assert (await session.get(Account, 1)).paper_balance == D(40)

    await copy_trade(sizing_rig, "live-buy", "20")

    assert trader.sent and trader.sent[0][0] == "BUY"
    # 5% of the exchange's $40, not of the ledger's old $100.
    assert trader.sent[0][2] == D(2)


async def test_a_live_fill_is_recorded_with_the_exchange_numbers(sizing_rig):
    trader = FakeTrader(
        cash=D(40),
        order=LiveOrder(
            submitted=True,
            order_id="0xfill",
            status="matched",
            shares=D(4),
            notional=D("1.80"),
            average_price=D("0.45"),
            confirmed=True,
        ),
    )
    go_live(sizing_rig, trader)
    await sizing_rig.engine.sync_live_state_once()

    await copy_trade(sizing_rig, "live-fill", "20")

    async with sizing_rig.sessions() as session:
        order = await session.scalar(select(PaperOrder))
        position = await session.scalar(select(Position))
        account = await session.get(Account, 1)
        # Shares and price are the exchange's, not the simulation's.
        assert (order.filled_shares, order.average_fill_price) == (D(4), D("0.45"))
        assert position.shares == D(4)
        assert account.paper_balance == D(40) - D("1.80")


async def test_a_live_entry_stops_while_the_ledger_and_exchange_disagree(sizing_rig):
    trader = FakeTrader(cash=D(40))
    go_live(sizing_rig, trader)  # ledger still says $100, exchange says $40

    await copy_trade(sizing_rig, "drifted", "20")

    async with sizing_rig.sessions() as session:
        trade = await session.scalar(select(CopyTrade))
        assert trade.skip_reason == "live_ledger_drift"
        # Eligible again once the two agree: nothing about the signal was wrong.
        assert trade.status == "retry_pending"
    assert trader.sent == []


async def test_a_live_entry_stops_when_the_exchange_cannot_be_read(sizing_rig):
    trader = FakeTrader()
    go_live(sizing_rig, trader, snapshot=snapshot_for(ok=False))

    await copy_trade(sizing_rig, "blind", "20")

    async with sizing_rig.sessions() as session:
        assert (await session.scalar(select(CopyTrade))).skip_reason == "live_state_unavailable"
    assert trader.sent == []


async def test_a_live_order_never_exceeds_the_configured_ceiling(sizing_rig):
    trader = FakeTrader(cash=D(1000))
    state = go_live(sizing_rig, trader, LIVE_MAX_ORDER_USDC="3")
    sizing_rig.engine.settings.live_max_order_usdc = D(3)
    await state.refresh()
    async with sizing_rig.sessions() as session:
        (await session.get(Account, 1)).max_trade_size = D(500)
        await session.commit()
    await sizing_rig.engine.sync_live_state_once()

    await copy_trade(sizing_rig, "capped", "2000")

    assert trader.sent
    assert trader.sent[0][2] <= D(3)


async def test_live_selling_never_asks_for_shares_the_exchange_does_not_see(sizing_rig):
    trader = FakeTrader(cash=D(40))
    state = go_live(sizing_rig, trader)
    await sizing_rig.engine.sync_live_state_once()
    await copy_trade(sizing_rig, "entry", "20")
    async with sizing_rig.sessions() as session:
        held = (await session.scalar(select(Position))).shares
    # The exchange only sees half of what our ledger recorded.
    state.snapshot = snapshot_for(cash=D(38), positions={"token": held / 2})
    trader.sent.clear()

    await copy_trade(sizing_rig, "exit", "20", side="SELL")

    assert trader.sent and trader.sent[0][0] == "SELL"
    assert trader.sent[0][2] <= held / 2


async def test_a_live_payout_is_not_credited_by_the_bot(sizing_rig):
    trader = FakeTrader(cash=D(40))
    go_live(sizing_rig, trader)
    await sizing_rig.engine.sync_live_state_once()
    await copy_trade(sizing_rig, "to-settle", "20")
    async with sizing_rig.sessions() as session:
        cash_before = (await session.get(Account, 1)).paper_balance
    sizing_rig.client.get_resolution = AsyncMock(return_value=D(1))

    await sizing_rig.engine.settle_once()

    async with sizing_rig.sessions() as session:
        account = await session.get(Account, 1)
        # Redemption happens on-chain; inventing the cash here would be a lie.
        assert account.paper_balance == cash_before
        assert await session.scalar(select(Position)) is None
        settled = await session.scalar(select(PaperOrder).where(PaperOrder.status == "settled"))
        assert settled is not None


async def test_the_daily_loss_cap_stops_new_entries_and_says_so(sizing_rig):
    trader = FakeTrader(cash=D(40))
    go_live(sizing_rig, trader)
    sizing_rig.engine.settings.live_max_daily_loss_usdc = D(5)
    await sizing_rig.engine.sync_live_state_once()
    async with sizing_rig.sessions() as session:
        # A day that started flat and has since realized a loss past the cap.
        session.add(DailyRisk(day=utc_day(), realized_at_start=D(0)))
        (await session.get(Account, 1)).realized_pnl = D(-6)
        await session.commit()

    await copy_trade(sizing_rig, "after-losses", "20")

    async with sizing_rig.sessions() as session:
        trade = await session.scalar(select(CopyTrade))
        assert trade.skip_reason == "live_daily_loss_stop"
        assert (await session.get(DailyRisk, utc_day())).stopped is True
    assert trader.sent == []
    assert not sizing_rig.engine.notifications.empty()


async def test_the_kill_switch_pulls_our_orders_off_the_exchange():
    from app.bot import TelegramApp

    trader = FakeTrader()
    executor = LiveExecutor(trader, live_settings(), None)

    assert await executor.cancel_open_orders() == "снято ордеров: 1"
    assert trader.cancelled == 1

    panel = object.__new__(TelegramApp)
    panel.settings = live_settings()
    # The mode is never ambiguous on screen.
    assert panel.live is True
    assert "LIVE" in panel._mode_badge()


async def test_the_preflight_reports_what_is_missing_without_signing(monkeypatch, capsys):
    from app import live_check

    monkeypatch.setattr("app.live_check.get_settings", lambda: live_settings())
    clob = FakeClob(balance={"balance": "5000000", "allowance": "0"})
    clob.get_collateral_address = lambda: "0xusdc"
    clob.get_conditional_address = lambda: "0xctf"
    clob.get_exchange_address = lambda neg_risk=False: "0xneg" if neg_risk else "0xexchange"
    clob.get_ok = lambda: "OK"
    monkeypatch.setattr("app.live.build_client", lambda settings: clob)

    code = await live_check.check("token")

    printed = capsys.readouterr().out
    # The log line the trader emits on start comes first; the report is the JSON.
    report = json.loads(printed[printed.index("{") :])
    assert code == 2  # a usable configuration would be 0
    assert report["usdc_balance"] == "5"
    assert report["ready_for_live"] is False
    # No allowance is the classic "orders accepted, nothing settles" trap.
    assert any("allowance" in problem for problem in report["blocking_problems"])
    assert report["market"]["tick_size"] == "0.01"
    assert clob.posted == []
