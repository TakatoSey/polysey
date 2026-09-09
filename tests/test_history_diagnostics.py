import json
from dataclasses import replace
from decimal import Decimal as D

import pytest
from sqlalchemy import select
from test_copy_latency import activity, drain
from test_copy_latency import rig as _rig
from test_execution_safety import book, seed_buy, sell
from test_sizing import event
from test_sizing import sizing_rig as _sizing_rig

from app.bot import TelegramApp
from app.engine import CopyEngine
from app.history_analysis import analyze_history
from app.ledger_audit import export_history
from app.models import (
    Account,
    CopyTrade,
    Leader,
    LeaderPosition,
    PaperOrder,
    Position,
    SourceObservation,
    SourceReceipt,
)
from app.polymarket import copy_event_key
from app.repository import initialize_execution

rig = _rig
sizing_rig = _sizing_rig


def network_event(base, tx="a"):
    address = "0x" + "1" * 40
    transaction = "0x" + tx * 64
    raw = (
        f"{transaction}:{base.timestamp}:{base.condition_id}:{base.token_id}:"
        f"{base.side}:{base.size}:{base.price}"
    )
    return replace(
        base,
        event_key=copy_event_key(raw, address),
        transaction_hash=transaction,
        trader_address=address,
    )


def test_identity_ignores_timestamp_but_not_wallet_transaction_or_economic_leg():
    first = network_event(activity("first"))
    assert (
        first.event_key == network_event(replace(first, timestamp=first.timestamp + 100)).event_key
    )
    for other in [
        network_event(first, "b"),
        network_event(replace(first, side="SELL")),
        network_event(replace(first, size=D(11))),
        network_event(replace(first, price=D("0.51"))),
        network_event(replace(first, token_id="other")),
    ]:
        assert other.event_key != first.event_key
    raw = f"{first.transaction_hash}:100:token:token:BUY:10:0.5"
    assert copy_event_key(raw, "0x" + "2" * 40) != first.event_key
    # No transaction hash: don't collapse unrelated identical trades over time.
    assert copy_event_key(":100:token:token:BUY:10:0.5", first.trader_address) != copy_event_key(
        ":101:token:token:BUY:10:0.5", first.trader_address
    )


@pytest.mark.parametrize("queued_together", [True, False])
async def test_timestamp_shift_cannot_double_buy_budget_or_leader_inventory(
    sizing_rig, queued_together
):
    r = sizing_rig
    r.clock.time = lambda: r.timestamp + 0.1
    first = network_event(event("initial", "20", timestamp=r.timestamp))
    second = network_event(replace(first, timestamp=first.timestamp + 1, source="rtds"))
    r.engine._schedule_copy(1, first)
    if not queued_together:
        await drain(r.engine)
        r.engine = CopyEngine(r.engine.settings, r.client)
    r.engine._schedule_copy(1, second)
    await drain(r.engine)
    async with r.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 95
        assert len(list(await session.scalars(select(CopyTrade)))) == 1
        assert (await session.scalar(select(LeaderPosition))).shares == 40


async def test_old_v2_timestamp_receipt_is_bridged_without_recopying(rig):
    import hashlib

    new = network_event(activity("new", timestamp=101))
    old_payload = "|".join(
        (new.trader_address, new.transaction_hash, "100", "token", "token", "BUY", "10", "0.5")
    )
    old_key = "v2:" + hashlib.sha256(old_payload.encode()).hexdigest()
    await seed_buy(rig, old_key)
    rig.engine._schedule_copy(1, new)
    await drain(rig.engine)
    rig.engine._schedule_copy(1, new)
    await drain(rig.engine)
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 95
        assert len(list(await session.scalars(select(CopyTrade)))) == 1
        assert await session.get(SourceReceipt, new.event_key)


async def test_timestamp_shift_cannot_sell_twice(rig):
    await seed_buy(rig)
    first = network_event(replace(activity("exit"), side="SELL", size=D(2)))
    second = network_event(replace(first, timestamp=first.timestamp + 1))
    for e in (first, second):
        rig.engine._schedule_copy(1, e)
        await drain(rig.engine)
    async with rig.sessions() as session:
        assert (await session.scalar(select(Position))).shares == 8
        assert (await session.scalar(select(LeaderPosition))).shares == 8
        assert (await session.get(Account, 1)).paper_balance == 96


async def test_source_fragments_survive_batch_and_settlement(sizing_rig):
    r = sizing_rig
    # Test metadata/batching, not expiry at the edge of a two-second bucket.
    r.clock.time = lambda: r.timestamp + 0.1
    events = [
        replace(
            event(f"fragment-{i}", "10", timestamp=r.timestamp),
            title="BTC <Up>",
            outcome="Up",
            transaction_hash=f"0xtx{i}",
            source="rtds" if i == 0 else "rest",
        )
        for i in range(2)
    ]
    for e in events:
        r.engine._schedule_copy(1, e)
    await drain(r.engine)
    r.client.get_resolution.return_value = D(1)
    await r.engine.settle_once()
    async with r.sessions() as session:
        assert await session.scalar(select(Position)) is None
        records = list(
            await session.scalars(select(SourceObservation).order_by(SourceObservation.event_key))
        )
        assert len(records) == 2
        assert records[0].copy_trade_id == records[1].copy_trade_id
        assert [o.transaction_hash for o in records] == ["0xtx0", "0xtx1"]
        assert [o.source for o in records] == ["rtds", "rest"]
        assert all(o.title == "BTC <Up>" and o.received_at > 0 for o in records)
        history = export_history(
            list(await session.scalars(select(CopyTrade))),
            list(await session.scalars(select(PaperOrder))),
            [],
            list(await session.scalars(select(Leader))),
            records,
        )
        report = analyze_history(json.loads(json.dumps(history, default=str)))
        assert report["buy_fills"] == 1
        assert report["markets"][0]["title"] == "BTC <Up>"
        assert report["markets"][0]["realized_pnl"] == 5
    for e in events:
        r.engine._schedule_copy(1, e)
    await drain(r.engine)
    async with r.sessions() as session:
        assert len(list(await session.scalars(select(SourceObservation)))) == 2
        assert (await session.get(Account, 1)).paper_balance == 105


async def test_completed_series_closes_earlier_pending_signals(rig):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(minimum="5")
    await sell(rig, "small", qty="2")
    await sell(rig, "rest", qty="8")
    async with rig.sessions() as session:
        first = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "small"))
        last = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "rest"))
        assert (first.status, first.skip_reason) == ("closed", "exit_completed_in_series")
        assert last.status == "executed"
        assert (await session.get(Account, 1)).paper_balance == 100
        rejected = await session.scalar(
            select(PaperOrder).where(PaperOrder.copy_trade_id == first.id)
        )
        assert rejected.status == "rejected"


async def test_startup_repairs_old_terminal_statuses_without_repaying(rig, monkeypatch):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(bid="0.4")
    await sell(rig, "pending")
    rig.client.get_resolution.return_value = D(1)
    await rig.engine.settle_once()
    async with rig.sessions() as session:
        trade = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "pending"))
        assert (trade.status, trade.skip_reason) == ("closed", "market_settled")
        await initialize_execution(session, rig.engine.settings)
        trade.status, trade.skip_reason = "exit_pending", "below_min_order_size"
        await session.commit()
    for _ in range(2):
        async with rig.sessions() as session:
            await initialize_execution(session, rig.engine.settings)
            await session.commit()
    async with rig.sessions() as session:
        trade = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "pending"))
        assert (trade.status, trade.skip_reason) == ("closed", "market_settled")
        assert (await session.get(Account, 1)).paper_balance == 105
        assert (
            len(
                list(
                    await session.scalars(select(PaperOrder).where(PaperOrder.status == "settled"))
                )
            )
            == 1
        )
    monkeypatch.setattr("app.bot.SessionLocal", rig.sessions)
    app = object.__new__(TelegramApp)
    app.settings = rig.engine.settings
    text = await app._orders_text_v2()
    assert "закрыто выплатой" in text
    assert "Незавершённые выходы" not in text


def test_offline_analysis_distinguishes_payout_cost_fees_and_open_positions():
    def trade(identity, token, price):
        return dict(
            id=identity,
            leader_id=1,
            token_id=token,
            side="BUY",
            timestamp=100,
            created_at="1970-01-01 00:01:42+00:00",
            leader_price=price,
            status="executed",
            skip_reason=None,
        )

    def order(identity, trade_id, token, side, size, price, fee="0", status="filled"):
        return dict(
            id=identity,
            copy_trade_id=trade_id,
            token_id=token,
            side=side,
            filled_shares=size,
            average_fill_price=price,
            fee=fee,
            status=status,
            created_at="1970-01-01 00:01:43+00:00",
        )

    history = dict(
        leaders=[dict(id=1, label="Test", address="0x1")],
        copy_trades=[trade(1, "won", "0.5"), trade(2, "lost", "0.01"), trade(3, "open", "0.5")],
        paper_orders=[
            order(1, 1, "won", "BUY", "10", "0.5", "0.1"),
            order(2, 2, "lost", "BUY", "100", "0.01"),
            order(3, 3, "open", "BUY", "10", "0.5"),
            order(4, None, "won", "SELL", "10", "1", status="settled"),
            order(5, None, "lost", "SELL", "100", "0", status="settled"),
        ],
    )
    result = analyze_history(history)
    assert result["realized_pnl"] == D("3.9")
    assert result["fees"] == D("0.1")
    assert result["outside_current_range_buys"][0]["debit"] == 1
    assert result["source_to_record_max_seconds"] == 2
    assert len(result["outside_current_range_buys"]) == 1
    assert next(m for m in result["markets"] if m["token_id"] == "open")["realized_pnl"] == 0
