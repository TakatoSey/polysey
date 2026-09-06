import asyncio
import time
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_copy_latency import activity, drain
from test_copy_latency import rig as _latency_rig
from test_sizing import event
from test_sizing import sizing_rig as _sizing_rig

from app.accounting import inventory, replay
from app.config import Settings
from app.engine import CopyEngine
from app.models import (
    Account,
    CopyTrade,
    ExitIntent,
    Leader,
    LeaderPosition,
    PaperOrder,
    Position,
    SourceReceipt,
)
from app.paper import execute_buy_fak_by_budget, execute_fak
from app.polymarket import Book, copy_event_key
from app.priority import PriorityLock
from app.repository import get_execution_policy, initialize_execution
from app.rtds import RTDSTradeStream

rig = _latency_rig
sizing_rig = _sizing_rig


def book(ask="0.5", bid="0.5", qty="1000", minimum="1"):
    return Book(
        asks=[(D(ask), D(qty))],
        bids=[(D(bid), D(qty))],
        tick_size=D("0.01"),
        min_order_size=D(minimum),
        neg_risk=False,
    )


@pytest.mark.parametrize("price", ["0.05", "0.5", "0.90"])
def test_five_cents_is_absolute_at_every_price(price):
    p = D(price)
    b = book(ask=str(p + D("0.05")))
    assert execute_buy_fak_by_budget(b, D(2), D(0), p, slippage_price=D("0.05")).shares > 0
    b.asks[0] = (p + D("0.050001"), D(100))
    assert execute_buy_fak_by_budget(b, D(2), D(0), p, slippage_price=D("0.05")).shares == 0


def test_drop_filter_and_sell_floor_are_not_symmetric_execution_limits():
    assert (
        execute_buy_fak_by_budget(
            book(ask="0.01"), D(2), D(0), D("0.12"), slippage_price=D("0.05")
        ).reason
        == "entry_price_drop"
    )
    assert (
        execute_buy_fak_by_budget(
            book(ask="0.46"), D(2), D(0), D("0.5"), slippage_price=D("0.05")
        ).shares
        > 0
    )
    # Better SELL prices are always allowed; only BUY has an adverse-drop filter.
    assert (
        execute_fak(book(bid="0.9"), "SELL", D(2), D(0), D("0.5"), slippage_price=D("0.05")).shares
        == 2
    )
    assert (
        execute_fak(
            book(bid="0.449"), "SELL", D(2), D(0), D("0.5"), slippage_price=D("0.05")
        ).shares
        == 0
    )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "100"])
def test_bad_cents_setting_cannot_start(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, DEFAULT_SLIPPAGE_CENTS=value)


def test_rtds_retains_initially_empty_live_watchlist():
    watched = set()
    stream = RTDSTradeStream(AsyncMock(), watched)
    assert stream.tracked_addresses is watched
    watched.add("0x" + "1" * 40)
    assert "0x" + "1" * 40 in stream.tracked_addresses
    watched.clear()
    assert not stream.tracked_addresses


async def test_rtds_actually_delivers_newly_added_leader_and_stops_after_removal():
    watched, callback = set(), AsyncMock(return_value="scheduled")
    stream = RTDSTradeStream(callback, watched)
    wallet = "0x" + "1" * 40
    payload = {
        "topic": "activity",
        "type": "trades",
        "payload": {
            "asset": "123",
            "conditionId": "0x" + "a" * 64,
            "proxyWallet": wallet,
            "transactionHash": "0x" + "b" * 64,
            "timestamp": int(time.time()),
            "size": "10",
            "price": "0.5",
            "side": "BUY",
        },
    }
    import json

    raw = json.dumps(payload)
    await stream.handle_message(raw)
    callback.assert_not_awaited()
    watched.add(wallet)
    await stream.handle_message(raw)
    callback.assert_awaited_once()
    assert callback.await_args.args[0].source == "rtds"
    watched.remove(wallet)
    await stream.handle_message(raw)
    assert callback.await_count == 1


async def test_exit_priority_and_cancelled_waiters_do_not_lose_lock():
    lock, order = PriorityLock(), []
    await lock.acquire()

    async def work(name, priority):
        async with lock.hold(priority):
            order.append(name)

    buy = asyncio.create_task(work("buy", 10))
    cancelled = asyncio.create_task(work("cancelled", 0))
    sell = asyncio.create_task(work("sell", 0))
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    lock.release()
    await asyncio.gather(buy, sell)
    assert order == ["sell", "buy"]
    await lock.acquire()
    waiter = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    lock.release()  # grant without giving the waiting task a turn
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    await asyncio.wait_for(lock.acquire(), 1)
    lock.release()


@pytest.mark.parametrize("stamp,source", [(80, "rest"), (97, "rtds"), (1000, "rest")])
async def test_old_or_future_buy_does_no_network_io(rig, stamp, source):
    e = activity("old", timestamp=stamp)
    e.source = source
    rig.engine._schedule_copy(1, e)
    await drain(rig.engine)
    rig.client.get_book.assert_not_awaited()
    rig.client.get_market.assert_not_awaited()
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        trade = await session.scalar(select(CopyTrade))
        assert trade.skip_reason in {"stale_signal", "invalid_signal_timestamp"}
        shadow = await session.scalar(select(LeaderPosition))
        if stamp == 1000:
            assert shadow is None
        else:
            assert shadow.shares == 10


async def test_signal_ages_while_waiting_for_ledger(rig, monkeypatch):
    await rig.engine._ledger_lock.acquire()
    rig.engine._schedule_copy(1, activity("old-in-queue"))
    await asyncio.sleep(0.04)
    monkeypatch.setattr(
        "app.engine.time", SimpleNamespace(time=lambda: 120, monotonic=time.monotonic)
    )
    rig.engine._ledger_lock.release()
    await drain(rig.engine)
    async with rig.sessions() as session:
        assert (await session.scalar(select(CopyTrade))).skip_reason == "stale_signal"
        assert await session.scalar(select(Position)) is None


async def test_batch_executes_once_and_every_fragment_is_deduplicated(sizing_rig):
    r = sizing_rig
    items = [event(f"fragment-{i}", "10", timestamp=r.timestamp) for i in range(4)]
    for e in items:
        r.engine._schedule_copy(1, e)
    await drain(r.engine)
    async with r.sessions() as session:
        assert len(list(await session.scalars(select(PaperOrder)))) == 1
        assert len(list(await session.scalars(select(CopyTrade)))) == 1
        assert len(list(await session.scalars(select(SourceReceipt)))) == 4
        assert (await session.get(Account, 1)).paper_balance == 90
    r.engine = CopyEngine(r.engine.settings, r.client)
    for e in items:
        r.engine._schedule_copy(1, e)
    await drain(r.engine)
    async with r.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 90
        assert len(list(await session.scalars(select(PaperOrder)))) == 1


async def seed_buy(rig, key="buy", leader=1, qty="10"):
    e = replace(activity(key), size=D(qty))
    rig.engine._schedule_copy(leader, e)
    await drain(rig.engine)


async def sell(rig, key="sell", qty="10", price="0.5", timestamp=101):
    e = replace(activity(key, side="SELL", timestamp=timestamp), size=D(qty), price=D(price))
    rig.engine._schedule_copy(1, e)
    await drain(rig.engine)


async def retry_due(rig):
    async with rig.sessions() as session:
        intent = await session.get(ExitIntent, (1, "token"))
        intent.next_attempt = 0
        await session.commit()
    await rig.engine.retry_exits_once()
    await asyncio.gather(*list(rig.engine._exit_workers.values()))
    await asyncio.sleep(0)


async def test_exit_partial_then_restart_retry_is_bounded_and_idempotent(rig):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(qty="4")
    await sell(rig)
    async with rig.sessions() as session:
        assert (await session.get(ExitIntent, (1, "token"))).remaining == 6
        assert (await session.get(Account, 1)).paper_balance == 97
    rig.engine = CopyEngine(rig.engine.settings, rig.client)
    await retry_due(rig)
    async with rig.sessions() as session:
        assert (await session.get(ExitIntent, (1, "token"))).remaining == 2
    await retry_due(rig)
    await retry_due(rig)
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert await session.scalar(select(Position)) is None
        assert (await session.get(ExitIntent, (1, "token"))).remaining == 0
        sells = list(await session.scalars(select(PaperOrder).where(PaperOrder.side == "SELL")))
        assert sum(o.filled_shares for o in sells) == 10


async def test_sell_fragments_accumulate_below_exchange_minimum(rig):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(minimum="5")
    await sell(rig, "s1", qty="2")
    await sell(rig, "s2", qty="2")
    async with rig.sessions() as session:
        assert (await session.get(ExitIntent, (1, "token"))).remaining == 4
        assert (await session.scalar(select(Position))).shares == 10
    await sell(rig, "s3", qty="6")
    async with rig.sessions() as session:
        assert await session.scalar(select(Position)) is None
        assert (await session.get(Account, 1)).paper_balance == 100


async def test_exit_retry_keeps_limit_and_fee_and_other_leader_inventory(rig):
    await seed_buy(rig)
    await seed_buy(rig, "second-owner", leader=2)
    rig.client.get_book.return_value = book(bid="0.4")
    await sell(rig)
    await retry_due(rig)
    async with rig.sessions() as session:
        assert (await session.get(ExitIntent, (1, "token"))).remaining == 10
        assert (await session.scalar(select(Position))).shares == 20
    rig.client.get_book.return_value = book(bid="0.6")
    rig.client.get_fee_rate.return_value = D("0.07")
    await retry_due(rig)
    async with rig.sessions() as session:
        assert (await session.scalar(select(Position))).shares == 10
        assert (await session.get(Account, 1)).paper_balance == D("95.832")
        holdings, warnings = await inventory(session)
        assert not warnings
        assert holdings["token", 1].shares == 0
        assert holdings["token", 2].shares == 10
        assert holdings["token", 1].realized == D("0.832")


@pytest.mark.parametrize("control", ["pause", "leader"])
async def test_retry_respects_controls(rig, control):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(bid="0.4")
    await sell(rig)
    async with rig.sessions() as session:
        if control == "pause":
            (await session.get(Account, 1)).paused = True
        else:
            (await session.get(Leader, 1)).active = False
        await session.commit()
    rig.client.get_book.return_value = book()
    await retry_due(rig)
    async with rig.sessions() as session:
        assert (await session.scalar(select(Position))).shares == 10


async def test_settlement_cancels_pending_exit_and_cannot_pay_twice(rig):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(bid="0.4")
    await sell(rig)
    rig.client.get_resolution.return_value = D(1)
    await rig.engine.settle_once()
    await retry_due(rig)
    await rig.engine.settle_once()
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 105
        assert (await session.get(ExitIntent, (1, "token"))).remaining == 0


async def test_partial_risk_exit_reduces_reserved_copy_exit_proportionally(rig):
    from app.models import RiskRule

    await seed_buy(rig)
    await seed_buy(rig, "second-owner", leader=2)
    rig.client.get_book.return_value = book(bid="0.4", qty="4")
    await sell(rig)
    async with rig.sessions() as session:
        session.add(RiskRule(token_id="token", enabled=True, stop_loss_pct=D("0.1")))
        await session.commit()
    await rig.engine.monitor_risk_once()
    async with rig.sessions() as session:
        assert (await session.get(ExitIntent, (1, "token"))).remaining == 8
        holdings, warnings = await inventory(session)
        assert not warnings
        assert holdings["token", 1].shares == 8
        assert holdings["token", 2].shares == 8
    rig.client.get_book.return_value = book()
    await retry_due(rig)
    async with rig.sessions() as session:
        assert (await session.scalar(select(Position))).shares == 8


async def test_old_exit_does_not_sell_a_newer_entry(rig):
    await seed_buy(rig)
    await sell(rig, timestamp=99)
    async with rig.sessions() as session:
        assert (await session.scalar(select(Position))).shares == 10
        trade = await session.scalar(select(CopyTrade).where(CopyTrade.side == "SELL"))
        assert trade.skip_reason == "out_of_order_exit"


async def test_exit_retry_preparation_does_not_block_an_unrelated_buy(rig):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(bid="0.4")
    await sell(rig)
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.client.get_market.side_effect

    async def slow_market(token):
        if token == "token":
            entered.set()
            await release.wait()
        return original(token)

    rig.client.get_market.side_effect = slow_market
    retry = asyncio.create_task(retry_due(rig))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        rig.engine._schedule_copy(2, activity("unrelated", token="other"))
        await drain(rig.engine)
        async with rig.sessions() as session:
            trade = await session.scalar(
                select(CopyTrade).where(CopyTrade.event_key == "unrelated")
            )
            assert trade.status == "executed"
    finally:
        release.set()
        await retry


async def test_new_exit_invalidates_inflight_retry_snapshot(rig):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(bid="0.4")
    await sell(rig, qty="5")
    entered, release = asyncio.Event(), asyncio.Event()
    original_prepare = rig.engine.prepare_copy

    async def prepare(e):
        result = await original_prepare(e)
        if e.event_key == "exit_retry":
            entered.set()
            await release.wait()
        return result

    rig.engine.prepare_copy = prepare
    retry = asyncio.create_task(retry_due(rig))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        rig.client.get_book.return_value = book()
        await sell(rig, "new-sell", qty="5")
        release.set()
        await retry
    finally:
        release.set()
        await retry
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert await session.scalar(select(Position)) is None
        orders = list(await session.scalars(select(PaperOrder).where(PaperOrder.side == "SELL")))
        assert sum(o.filled_shares for o in orders) == 10


async def test_pending_exit_blocks_new_buy_budget(rig):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(bid="0.4")
    await sell(rig, qty="5", timestamp=100)
    rig.engine._schedule_copy(1, activity("new-buy", timestamp=101))
    await drain(rig.engine)
    async with rig.sessions() as session:
        trade = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "new-buy"))
        assert trade.skip_reason == "exit_pending"
        assert (await session.get(Account, 1)).paper_balance == 95


async def test_retry_never_uses_default_zero_fee_after_metadata_failure(rig):
    await seed_buy(rig)
    rig.client.get_book.return_value = book(bid="0.4")
    await sell(rig)
    rig.client.get_book.return_value = book()
    rig.client.get_fee_rate.side_effect = ValueError("fee_identity_mismatch")
    await retry_due(rig)
    async with rig.sessions() as session:
        intent = await session.get(ExitIntent, (1, "token"))
        assert intent.remaining == 10
        assert intent.last_reason == "exit_market_data_unavailable"
        assert (await session.get(Account, 1)).paper_balance == 95


@pytest.mark.parametrize("cents", ["0", "5", "12.25"])
async def test_telegram_writes_cents_in_separate_policy(rig, monkeypatch, cents):
    from app.bot import TelegramApp

    monkeypatch.setattr("app.bot.SessionLocal", rig.sessions)
    app = object.__new__(TelegramApp)
    app.settings = rig.engine.settings
    app._edit_panel = AsyncMock()
    msg = SimpleNamespace(
        text=f"/setslippage {cents}",
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=1),
        delete=AsyncMock(),
    )
    await app.setslippage(msg)
    async with rig.sessions() as session:
        assert (await get_execution_policy(session, rig.engine.settings)).slippage_price == D(
            cents
        ) / 100
        assert (await session.get(Account, 1)).slippage_bps == 500


async def test_fill_and_receipt_rollback_together_on_database_error(rig):
    # Explicit BEGIN also enables transactional savepoints on SQLite, whose
    # default driver otherwise starts transactions only on the first DML.
    from sqlalchemy import text

    e = activity("rollback")
    prepared = await rig.engine.prepare_copy(e)
    async with rig.sessions() as session:
        await session.execute(text("BEGIN"))
        leader = await session.get(Leader, 1)
        await rig.engine.process_event(session, leader, e, prepared)
        await session.rollback()
    async with rig.sessions() as session:
        assert await session.scalar(select(SourceReceipt)) is None
        assert await session.scalar(select(PaperOrder)) is None
        assert await session.scalar(select(Position)) is None
        assert (await session.get(Account, 1)).paper_balance == 100
    await seed_buy(rig, "rollback")
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 95


async def test_legacy_dedup_migration_does_not_rewrite_money_or_repeat_trade(rig):
    raw = "0xabc:100:token:token:BUY:10.00:0.50"
    await seed_buy(rig, raw)
    async with rig.sessions() as session:
        await initialize_execution(session, rig.engine.settings)
        await session.commit()
    canonical = copy_event_key(raw, "0x" + "1" * 40)
    await seed_buy(rig, canonical)
    async with rig.sessions() as session:
        await initialize_execution(session, rig.engine.settings)
        assert len(list(await session.scalars(select(CopyTrade)))) == 1
        assert (await session.get(Account, 1)).paper_balance == 95
        assert await session.get(SourceReceipt, canonical)
        assert (await get_execution_policy(session, rig.engine.settings)).slippage_price == D(
            "0.05"
        )


def record(token, side, qty, price, fee="0", status="filled", identity=1):
    return SimpleNamespace(
        token_id=token,
        side=side,
        filled_shares=D(qty),
        average_fill_price=D(price),
        fee=D(fee),
        status=status,
        id=identity,
    )


def test_pnl_never_consumes_other_tokens_or_duplicates_shared_settlement():
    rows = [
        (record("a", "BUY", "10", "0.2", "0.1"), 1),
        (record("b", "BUY", "10", "0.8"), 1),
        (record("a", "BUY", "10", "0.4"), 2),
        (record("b", "SELL", "10", "1", status="settled"), None),
        (record("a", "SELL", "20", "0", status="settled"), None),
    ]
    h, warnings = replay(rows)
    assert not warnings
    assert h["b", 1].realized == 2
    assert h["a", 1].realized == D("-2.1")
    assert h["a", 2].realized == -4
    assert all(x.shares == 0 and x.cost == 0 for x in h.values())


def test_legacy_cross_owner_sell_is_flagged_not_hidden():
    h, warnings = replay(
        [(record("a", "BUY", "10", "0.2"), 2), (record("a", "SELL", "10", "0.4"), 1)]
    )
    assert warnings == ["historical_cross_owner_sell:1"]
    assert h["a", 2].realized == 2


def test_read_only_audit_exposes_actual_balance_and_share_mismatches():
    from app.ledger_audit import summarize

    account = SimpleNamespace(starting_balance=D(100), paper_balance=D(95), realized_pnl=D(0))
    positions = [SimpleNamespace(token_id="a", shares=D(10), cost_basis=D(5))]
    rows = [(record("a", "BUY", "10", "0.5"), 1)]
    assert summarize(account, rows, positions, [], [])["consistent"]
    account.paper_balance = D(96)
    positions[0].shares = D(9)
    report = summarize(account, rows, positions, [], [])
    assert not report["consistent"]
    assert report["cash_gap"] == 1
    assert report["share_mismatches"][0]["orders_shares"] == 10


async def test_restart_does_not_forget_sell_barrier_in_legacy_mode(rig):
    await seed_buy(rig)
    await sell(rig)
    rig.engine = CopyEngine(rig.engine.settings, rig.client)
    await seed_buy(rig, "late-fragment")
    async with rig.sessions() as session:
        assert await session.scalar(select(Position)) is None
        trade = await session.scalar(
            select(CopyTrade).where(CopyTrade.event_key == "late-fragment")
        )
        assert trade.skip_reason == "buy_superseded_by_sell"


async def test_invalid_future_sell_does_not_poison_following_buys(rig):
    await sell(rig, timestamp=99999999999)
    await seed_buy(rig)
    async with rig.sessions() as session:
        assert (await session.scalar(select(Position))).shares == 10
        assert (await session.get(Account, 1)).paper_balance == 95


async def test_slow_other_leader_buy_on_same_token_does_not_block_our_sell(rig):
    await seed_buy(rig)
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.client.get_market.side_effect
    calls = 0

    async def slow_first(token):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return original(token)

    rig.client.get_market.side_effect = slow_first
    rig.engine._schedule_copy(2, activity("other-leader-buy"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        rig.engine._schedule_copy(1, activity("priority-sell", side="SELL", timestamp=101))
        await asyncio.wait_for(asyncio.shield(rig.engine._pending["priority-sell"]), 1)
        async with rig.sessions() as session:
            trade = await session.scalar(
                select(CopyTrade).where(CopyTrade.event_key == "priority-sell")
            )
            assert trade.status == "executed"
            assert (await session.get(Account, 1)).paper_balance == 100
    finally:
        release.set()
        await drain(rig.engine)


async def test_full_exit_releases_exact_cost_despite_rounded_historical_vwap(rig):
    b = book()
    b.asks = [(D("0.5"), D(2)), (D("0.51"), D(1))]
    rig.client.get_book.return_value = b
    await seed_buy(rig)
    rig.client.get_book.return_value = book()
    await sell(rig)
    async with rig.sessions() as session:
        account = await session.get(Account, 1)
        assert await session.scalar(select(Position)) is None
        assert account.paper_balance == D("99.99")
        assert account.realized_pnl == D("-0.01")
