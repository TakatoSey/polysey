import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import Base
from app.engine import CopyEngine
from app.models import Account, CopyTrade, Leader, PaperOrder, Position, RiskRule
from app.polymarket import Book, LeaderActivity


def activity(key, token="token", side="BUY", timestamp=100):
    return LeaderActivity(
        event_key=key,
        timestamp=timestamp,
        condition_id=token,
        token_id=token,
        side=side,
        size=Decimal(10),
        price=Decimal("0.5"),
        title=token,
        outcome="Yes",
        slug=token,
        received_at=time.time(),
        received_monotonic=time.monotonic(),
    )


def market(condition):
    return {
        "tokens": [{"token_id": condition}],
        "closed": False,
        "accepting_orders": True,
        "seconds_delay": 0,
    }


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    # Separate real connections, unlike an in-memory SQLite StaticPool.
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'latency.db'}")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr("app.engine.SessionLocal", sessions)
    async with db.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Account(id=1, paper_balance=100, starting_balance=100))
        session.add_all(
            [
                Leader(id=1, address="0x" + "1" * 40, initialized=True, last_timestamp=100),
                Leader(id=2, address="0x" + "2" * 40, initialized=True, last_timestamp=100),
            ]
        )
        await session.commit()
    book = Book(
        bids=[(Decimal("0.5"), Decimal(1000))],
        asks=[(Decimal("0.5"), Decimal(1000))],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal(1),
        neg_risk=False,
    )
    client = SimpleNamespace(
        get_market=AsyncMock(side_effect=market),
        get_fee_rate=AsyncMock(return_value=Decimal(0)),
        get_book=AsyncMock(return_value=book),
        get_activity=AsyncMock(return_value=[]),
        get_resolution=AsyncMock(return_value=None),
    )
    settings = Settings(_env_file=None, COPY_BALANCE_PCT=1, LEADER_ORDER_SCALE=1)
    engine = CopyEngine(settings, client)
    yield SimpleNamespace(engine=engine, client=client, sessions=sessions, book=book)
    await engine.stop()
    tasks = list(engine._pending.values()) + list(engine._leader_polls.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await db.dispose()


async def drain(engine):
    await asyncio.wait_for(asyncio.gather(*list(engine._pending.values())), timeout=3)
    await asyncio.sleep(0)  # run task completion callbacks


async def wait_for_order(rig, token):
    async def wait():
        while True:
            async with rig.sessions() as session:
                order = await session.scalar(select(PaperOrder).where(PaperOrder.token_id == token))
                if order:
                    return order
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), timeout=3)


async def test_slow_book_does_not_block_another_token(rig):
    entered, release = asyncio.Event(), asyncio.Event()

    async def get_book(token):
        if token == "slow":
            entered.set()
            await release.wait()
        return rig.book

    rig.client.get_book.side_effect = get_book
    rig.engine._schedule_copy(1, activity("slow", "slow"))
    await asyncio.wait_for(entered.wait(), 2)
    rig.engine._schedule_copy(2, activity("fast", "fast"))
    assert (await wait_for_order(rig, "fast")).status == "filled"
    release.set()
    await drain(rig.engine)


async def test_same_token_sell_cannot_overtake_buy(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def get_market(condition):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return market(condition)

    rig.client.get_market.side_effect = get_market
    rig.engine._schedule_copy(1, activity("buy"))
    await asyncio.wait_for(entered.wait(), 2)
    rig.engine._schedule_copy(1, activity("sell", side="SELL", timestamp=101))
    await asyncio.sleep(0.03)
    # Book acquisition starts immediately and is allowed to overlap metadata;
    # the token predecessor still prevents execution from overtaking the BUY.
    assert rig.client.get_book.await_count == 2
    release.set()
    await drain(rig.engine)
    async with rig.sessions() as session:
        trades = list(await session.scalars(select(CopyTrade).order_by(CopyTrade.id)))
        assert [(t.side, t.status) for t in trades] == [("BUY", "executed"), ("SELL", "executed")]
        assert await session.scalar(select(Position)) is None
        assert (await session.get(Account, 1)).paper_balance == 100


async def test_concurrent_buys_cannot_overspend_and_replay_is_idempotent(rig):
    async with rig.sessions() as session:
        (await session.get(Account, 1)).paper_balance = Decimal(5)
        await session.commit()
    first = activity("one", "one")
    rig.engine._schedule_copy(1, first)
    rig.engine._schedule_copy(1, first)
    rig.engine._schedule_copy(2, activity("two", "two"))
    await drain(rig.engine)
    rig.engine._schedule_copy(1, first)  # duplicate after process restart / REST retry
    await drain(rig.engine)
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 0
        orders = list(await session.scalars(select(PaperOrder)))
        assert len(orders) == 2
        assert sorted(o.status for o in orders) == ["filled", "rejected"]


async def test_slow_settlement_does_not_block_copy_or_overwrite_balance(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    async with rig.sessions() as session:
        (await session.get(Account, 1)).paper_balance = Decimal(95)
        session.add(
            Position(
                token_id="resolved",
                condition_id="resolved",
                title="resolved",
                outcome="Yes",
                shares=10,
                cost_basis=5,
                average_price=Decimal("0.5"),
            )
        )
        await session.commit()

    async def resolution(*args):
        entered.set()
        await release.wait()
        return Decimal(1)

    rig.client.get_resolution.side_effect = resolution
    settling = asyncio.create_task(rig.engine.settle_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rig.engine._schedule_copy(1, activity("new-buy"))
        await drain(rig.engine)
        release.set()
        await asyncio.wait_for(settling, 2)
        # $95 - $5 copied BUY + $10 payout. No stale $95 + $10 overwrite.
        async with rig.sessions() as session:
            assert (await session.get(Account, 1)).paper_balance == 100
            assert (await session.get(Account, 1)).realized_pnl == 5
            assert len(list(await session.scalars(select(Position)))) == 1
        await rig.engine.settle_once()  # the open token has the mocked winning payout too
        async with rig.sessions() as session:
            assert (await session.get(Account, 1)).paper_balance == 110
    finally:
        release.set()
        await settling


async def test_pause_rechecked_after_preparation(rig):
    entered, release = asyncio.Event(), asyncio.Event()

    async def get_market(condition):
        entered.set()
        await release.wait()
        return market(condition)

    rig.client.get_market.side_effect = get_market
    rig.engine._schedule_copy(1, activity("paused"))
    await asyncio.wait_for(entered.wait(), 2)
    async with rig.sessions() as session:
        (await session.get(Account, 1)).paused = True
        await session.commit()
    release.set()
    await drain(rig.engine)
    async with rig.sessions() as session:
        assert await session.scalar(select(PaperOrder)) is None


async def test_slow_leader_poll_does_not_block_fast_leader(rig):
    entered, release = asyncio.Event(), asyncio.Event()

    async def get_activity(address):
        if address == "0x" + "1" * 40:
            entered.set()
            await release.wait()
            return []
        return [activity("fast-leader", "fast")]

    rig.client.get_activity.side_effect = get_activity
    polling = asyncio.create_task(rig.engine.poll_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await wait_for_order(rig, "fast")).status == "filled"
    finally:
        release.set()
        await polling
    await drain(rig.engine)


async def test_fast_leader_keeps_polling_while_other_request_is_stuck(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    fast_calls = 0

    async def get_activity(address):
        nonlocal fast_calls
        if address == "0x" + "1" * 40:
            entered.set()
            await release.wait()
        else:
            fast_calls += 1
        return []

    rig.client.get_activity.side_effect = get_activity
    await rig.engine.poll_background_once()
    await asyncio.wait_for(entered.wait(), 2)
    slow_task = rig.engine._leader_polls[1]
    for _ in range(3):
        await asyncio.sleep(0.02)
        await rig.engine.poll_background_once()
    await asyncio.sleep(0.02)
    assert fast_calls >= 3
    assert rig.engine._leader_polls[1] is slow_task
    release.set()
    await slow_task


async def test_aged_book_is_refetched_before_execution(rig):
    acquired = asyncio.Event()
    original_book = rig.client.get_book

    async def book(token):
        acquired.set()
        return await original_book(token)

    rig.client.get_book = book
    await rig.engine._ledger_lock.acquire()
    rig.engine._schedule_copy(1, activity("aged"))
    try:
        await asyncio.wait_for(acquired.wait(), 2)
        await asyncio.sleep(0.3)
    finally:
        rig.engine._ledger_lock.release()
    await drain(rig.engine)
    assert original_book.await_count == 2


async def test_duplicate_settlement_checks_only_pay_once(rig):
    async with rig.sessions() as session:
        session.add(
            Position(
                token_id="win",
                condition_id="win",
                title="win",
                outcome="Yes",
                shares=10,
                cost_basis=5,
                average_price=Decimal("0.5"),
            )
        )
        await session.commit()
    rig.client.get_resolution.return_value = Decimal(1)
    await asyncio.gather(rig.engine.settle_once(), rig.engine.settle_once())
    async with rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 110
        assert len(list(await session.scalars(select(PaperOrder)))) == 1


async def test_shutdown_cancels_blocked_poll_and_preparation(rig):
    blocked = asyncio.Event()
    entered = asyncio.Event()

    async def get_market(condition):
        entered.set()
        await blocked.wait()

    async def get_activity(address):
        await blocked.wait()

    rig.client.get_market.side_effect = get_market
    rig.client.get_activity.side_effect = get_activity
    rig.engine._schedule_copy(1, activity("blocked"))
    running = asyncio.create_task(rig.engine.run())
    await asyncio.wait_for(entered.wait(), 2)
    await rig.engine.stop()
    await asyncio.wait_for(running, 2)
    assert not rig.engine._pending
    assert not rig.engine._leader_polls


async def test_exchange_delay_runs_concurrently_and_book_is_fetched_after_it(rig, monkeypatch):
    original_sleep = asyncio.sleep
    entered, release = asyncio.Event(), asyncio.Event()
    delayed = 0

    async def sleep(seconds):
        nonlocal delayed
        if seconds > 2.5:
            delayed += 1
            if delayed == 2:
                entered.set()
            await release.wait()
        else:
            await original_sleep(seconds)

    monkeypatch.setattr("app.engine.asyncio.sleep", sleep)
    rig.client.get_market.side_effect = lambda c: {**market(c), "seconds_delay": 3}
    rig.engine._schedule_copy(1, activity("a"))
    rig.engine._schedule_copy(1, activity("b"))
    await asyncio.wait_for(entered.wait(), 2)
    rig.client.get_book.assert_not_called()
    release.set()
    await drain(rig.engine)
    assert rig.client.get_book.await_count == 2


async def test_overflow_does_not_checkpoint_past_unprocessed_event(rig):
    rig.engine.settings.copy_queue_limit = 1
    rig.client.get_activity.return_value = [
        activity("a", timestamp=100),
        activity("b", timestamp=101),
    ]
    # Only one active leader for this recovery test.
    async with rig.sessions() as session:
        (await session.get(Leader, 2)).active = False
        await session.commit()
    await rig.engine.poll_once()
    async with rig.sessions() as session:
        assert (await session.get(Leader, 1)).last_timestamp == 100
    await drain(rig.engine)
    await rig.engine.poll_once()
    await drain(rig.engine)
    async with rig.sessions() as session:
        assert len(list(await session.scalars(select(CopyTrade)))) == 2


async def test_risk_exit_delay_does_not_lock_balance(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    async with rig.sessions() as session:
        session.add(
            Position(
                token_id="risk",
                condition_id="risk",
                title="risk",
                outcome="Yes",
                shares=10,
                cost_basis=8,
                average_price=Decimal("0.8"),
            )
        )
        session.add(RiskRule(token_id="risk", enabled=True, stop_loss_pct=Decimal("0.1")))
        await session.commit()

    async def get_market(condition):
        if condition == "risk":
            entered.set()
            await release.wait()
        return market(condition)

    rig.client.get_market.side_effect = get_market
    monitoring = asyncio.create_task(rig.engine.monitor_risk_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rig.engine._schedule_copy(1, activity("buy-during-risk"))
        await drain(rig.engine)
        assert (await wait_for_order(rig, "token")).status == "filled"
    finally:
        release.set()
        await monitoring
    async with rig.sessions() as session:
        assert await session.scalar(select(Position).where(Position.token_id == "risk")) is None


@pytest.mark.parametrize("value", [0, -1, 0.01])
def test_poll_interval_cannot_busy_loop(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, POLL_INTERVAL_SECONDS=value)
