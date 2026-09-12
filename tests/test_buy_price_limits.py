import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_copy_latency import activity, drain
from test_copy_latency import rig as _rig
from test_sizing import event
from test_sizing import sizing_rig as _sizing_rig

from app.bot import SIZING_REASONS
from app.models import (
    Account,
    CopyTrade,
    Leader,
    LeaderPosition,
    PaperOrder,
    SizingEntry,
    SourceReceipt,
)
from app.paper import execute_buy_fak_by_budget, execute_fak
from app.polymarket import Book
from app.price_limits import (
    MAX_BUY_PRICE,
    MIN_BUY_PRICE,
    PriceRange,
    allowed_buy_price,
    leader_price_range,
)

rig = _rig
sizing_rig = _sizing_rig


def book(price):
    return Book(
        asks=[(D(price), D(1000))],
        bids=[(D(price), D(1000))],
        tick_size=D("0.01"),
        min_order_size=D(1),
        neg_risk=False,
    )


@pytest.mark.parametrize(
    "price,allowed",
    [
        ("0.01", False),
        ("0.019999", False),
        ("0.02", True),
        ("0.5", True),
        ("0.98", True),
        ("0.980001", False),
        ("0.99", False),
        ("NaN", False),
    ],
)
def test_inclusive_price_range(price, allowed):
    assert allowed_buy_price(D(price)) == allowed


@pytest.mark.parametrize("price", ["0.02", "0.98"])
def test_boundary_fills_remain_allowed(price):
    b = book(price)
    assert execute_buy_fak_by_budget(b, D(2), D(0), D(price), slippage_price=D("0.05")).shares > 0
    assert execute_fak(b, "BUY", D(2), D(0), D(price), slippage_price=D("0.05")).shares == 2


@pytest.mark.parametrize("price", ["0.01", "0.99"])
def test_leader_cannot_bypass_band_even_with_acceptable_ask(price):
    b = book("0.02" if price == "0.01" else "0.98")
    assert (
        execute_buy_fak_by_budget(b, D(2), D(0), D(price), slippage_price=D("0.50")).reason
        == "leader_price_out_of_range"
    )
    assert (
        execute_fak(b, "BUY", D(2), D(0), D(price), slippage_price=D("0.50")).reason
        == "leader_price_out_of_range"
    )


@pytest.mark.parametrize("ask,reference", [("0.01", "0.02"), ("0.99", "0.98")])
def test_valid_leader_price_does_not_allow_out_of_range_ask(ask, reference):
    b = book(ask)
    assert (
        execute_buy_fak_by_budget(b, D(2), D(0), D(reference), slippage_price=D("0.05")).reason
        == "buy_price_out_of_range"
    )
    assert execute_fak(b, "BUY", D(2), D(0)).reason == "buy_price_out_of_range"


def test_depth_is_capped_even_when_average_price_would_be_in_band():
    b = book("0.97")
    b.asks = [(D("0.97"), D(2)), (D("0.98"), D(1)), (D("0.99"), D(100))]
    for result in [
        execute_buy_fak_by_budget(b, D(5), D(0), D("0.97"), slippage_price=D("0.5")),
        execute_fak(b, "BUY", D(5), D(0)),
    ]:
        assert result.status == "partial"
        assert result.shares == 3
        assert result.notional == D("2.92")


@pytest.mark.parametrize("price", ["0.01", "0.99"])
def test_sell_is_never_blocked_by_buy_band(price):
    assert (
        execute_fak(book(price), "SELL", D(5), D(0), D(price), slippage_price=D("0.05")).shares == 5
    )


@pytest.mark.parametrize("price", ["0.01", "0.99"])
async def test_rejected_source_has_receipt_and_shadow_but_no_network_or_spend(rig, price):
    bad = replace(activity("blocked"), price=D(price))
    rig.engine._schedule_copy(1, bad)
    await drain(rig.engine)
    rig.engine._schedule_copy(1, bad)
    await drain(rig.engine)
    rig.client.get_book.assert_not_awaited()
    rig.client.get_market.assert_not_awaited()
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert (await session.scalar(select(LeaderPosition))).shares == 10
        assert len(list(await session.scalars(select(SourceReceipt)))) == 1
        assert (await session.scalar(select(CopyTrade))).skip_reason == "leader_price_out_of_range"
        assert (await session.scalar(select(PaperOrder))).filled_shares == 0
        assert await session.scalar(select(SizingEntry)) is None


@pytest.mark.parametrize("bad_first", [True, False])
async def test_bad_fragment_cannot_hide_in_vwap_or_increase_smart_budget(sizing_rig, bad_first):
    r = sizing_rig
    good = event("good", "20", timestamp=r.timestamp, price="0.5")
    bad = event("bad", "200", timestamp=r.timestamp, price="0.99")
    for e in [bad, good] if bad_first else [good, bad]:
        r.engine._schedule_copy(1, e)
    await asyncio.wait_for(asyncio.gather(*set(r.engine._pending.values())), 5)
    async with r.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 95
        assert (await session.scalar(select(SizingEntry))).leader_notional == 20
        assert (await session.scalar(select(SizingEntry))).spent == 5
        assert len(list(await session.scalars(select(CopyTrade)))) == 2
        assert (
            await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "bad"))
        ).skip_reason == "leader_price_out_of_range"


def test_rejection_descriptions_name_the_price_range_without_fixing_its_bounds():
    # A leader may run a range of their own, so a label naming 2-98c would be
    # wrong for them; it still has to say the price was the reason.
    for reason in ("leader_price_out_of_range", "buy_price_out_of_range"):
        assert "диапазон" in SIZING_REASONS[reason]
        assert "цена" in SIZING_REASONS[reason]
        assert "98" not in SIZING_REASONS[reason]


def test_history_export_retains_matching_ids_without_private_configuration():
    from types import SimpleNamespace

    from app.ledger_audit import export_history

    leader = SimpleNamespace(id=1, address="0x1", label="gningd", telegram_token="secret")
    result = export_history([], [], [], [leader])
    assert result["leaders"] == [
        {
            "id": 1,
            "address": "0x1",
            "label": "gningd",
            "fixed_trade_size": None,
            "fixed_trade_percent": None,
            "min_buy_price": None,
            "max_buy_price": None,
        }
    ]
    assert "telegram_token" not in str(result)


async def test_full_history_command_exports_real_model_fields(rig, monkeypatch, capsys):
    import json

    from app import ledger_audit

    rig.engine._schedule_copy(1, activity("exported"))
    await drain(rig.engine)
    capsys.readouterr()
    monkeypatch.setattr(ledger_audit, "SessionLocal", rig.sessions)
    await ledger_audit.main(include_history=True)
    report = json.loads(capsys.readouterr().out)
    history = report["history"]
    trade = history["copy_trades"][0]
    order = history["paper_orders"][0]
    assert trade["event_key"] == "exported"
    assert order["copy_trade_id"] == trade["id"]
    assert order["token_id"] == trade["token_id"]
    assert order["status"] == "filled"
    assert history["positions"][0]["token_id"] == trade["token_id"]
    assert history["leaders"][0]["id"] == trade["leader_id"]


WIDE = PriceRange(D(0), D(1))
NARROW = PriceRange(D("0.40"), D("0.60"))


def leader_bounds(minimum, maximum):
    price_range = leader_price_range(SimpleNamespace(min_buy_price=minimum, max_buy_price=maximum))
    return price_range.minimum, price_range.maximum


def test_leader_range_falls_back_bound_by_bound():
    default = (MIN_BUY_PRICE, MAX_BUY_PRICE)
    assert leader_bounds(None, None) == default
    assert leader_bounds(D(0), D(1)) == (D(0), D(1))
    assert leader_bounds(D("0.10"), None) == (D("0.10"), MAX_BUY_PRICE)
    assert leader_bounds(None, D("0.50")) == (MIN_BUY_PRICE, D("0.50"))
    # Stored nonsense must not widen anything: an inverted pair and values
    # outside 0-1 fall back instead of buying at prices nobody asked for.
    assert leader_bounds(D("0.90"), D("0.10")) == default
    assert leader_bounds(D("NaN"), D(2)) == default
    assert leader_bounds(D(-1), None) == default


@pytest.mark.parametrize("price", ["0.01", "0.99"])
def test_widened_range_fills_exactly_where_the_default_refuses(price):
    b = book(price)
    assert (
        execute_buy_fak_by_budget(b, D(2), D(0), D(price), slippage_price=D("0.05")).reason
        == "leader_price_out_of_range"
    )
    assert (
        execute_buy_fak_by_budget(
            b, D(2), D(0), D(price), slippage_price=D("0.05"), price_range=WIDE
        ).shares
        > 0
    )
    assert (
        execute_fak(
            b, "BUY", D(2), D(0), D(price), slippage_price=D("0.05"), price_range=WIDE
        ).shares
        == 2
    )


def test_narrowed_range_refuses_a_price_the_default_allows():
    b = book("0.70")
    assert execute_buy_fak_by_budget(b, D(2), D(0), D("0.70"), slippage_price=D("0.05")).shares > 0
    for result in (
        execute_buy_fak_by_budget(
            b, D(2), D(0), D("0.70"), slippage_price=D("0.05"), price_range=NARROW
        ),
        execute_fak(b, "BUY", D(2), D(0), D("0.70"), slippage_price=D("0.05"), price_range=NARROW),
    ):
        assert result.shares == 0
        assert result.reason == "leader_price_out_of_range"


def test_a_custom_range_still_caps_depth_at_its_own_maximum():
    b = book("0.97")
    b.asks = [(D("0.97"), D(2)), (D("0.98"), D(1)), (D("0.99"), D(100))]
    wide = execute_buy_fak_by_budget(
        b, D(5), D(0), D("0.97"), slippage_price=D("0.5"), price_range=WIDE
    )
    # 99c is reachable only because this leader's own maximum allows it.
    assert wide.status == "filled"
    assert wide.shares > 3
    capped = execute_buy_fak_by_budget(
        b, D(5), D(0), D("0.97"), slippage_price=D("0.5"), price_range=PriceRange(D(0), D("0.98"))
    )
    assert capped.status == "partial"
    assert capped.shares == 3
    assert capped.notional == D("2.92")


@pytest.mark.parametrize("price", ["0.01", "0.99"])
def test_a_narrow_range_never_blocks_a_sell(price):
    assert (
        execute_fak(
            book(price),
            "SELL",
            D(5),
            D(0),
            D(price),
            slippage_price=D("0.05"),
            price_range=NARROW,
        ).shares
        == 5
    )


async def rtds_copy(rig, key, *, leader_id, price, notional="20"):
    """Schedule through the live RTDS entry point, which reads the leader row."""
    incoming = event(key, notional, timestamp=rig.timestamp, price=price)
    incoming.trader_address = "0x" + str(leader_id) * 40
    await rig.engine.on_rtds_trade(incoming)
    await drain(rig.engine)
    return incoming


async def test_only_the_leader_with_a_widened_range_buys_at_ninety_nine_cents(sizing_rig):
    sizing_rig.client.get_book.return_value = book("0.99")
    async with sizing_rig.sessions() as session:
        leader = await session.get(Leader, 1)
        leader.min_buy_price, leader.max_buy_price = D(0), D(1)
        await session.commit()
    await rtds_copy(sizing_rig, "wide-range", leader_id=1, price="0.99")
    await rtds_copy(sizing_rig, "default-range", leader_id=2, price="0.99")
    async with sizing_rig.sessions() as session:
        trades = {trade.leader_id: trade for trade in await session.scalars(select(CopyTrade))}
        assert trades[1].status == "executed"
        assert trades[2].status == "skipped"
        assert trades[2].skip_reason == "leader_price_out_of_range"
        filled = list(
            await session.scalars(
                select(PaperOrder).where(PaperOrder.status.in_(["filled", "partial"]))
            )
        )
        assert len(filled) == 1
        assert filled[0].average_fill_price == D("0.99")
        entries = list(await session.scalars(select(SizingEntry)))
        assert [entry.leader_id for entry in entries] == [1]
