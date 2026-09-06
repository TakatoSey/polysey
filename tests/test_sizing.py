"""Regression tests for balance-based, cumulative entry allocation."""
import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import Base
from app.engine import CopyEngine
from app.models import (
    Account,
    CopyTrade,
    Leader,
    LeaderSizingProfile,
    PaperOrder,
    Position,
    SizingAudit,
    SizingEntry,
)
from app.polymarket import Book, LeaderActivity
from app.repository import get_or_create_account
from app.sizing import entry_bucket, entry_budget, sample_entries

D = Decimal
TOLERANCE = D("0.000001")


def event(key, notional="20", *, token="token", side="BUY", timestamp=100, price="0.5"):
    return LeaderActivity(
        event_key=key,
        timestamp=timestamp,
        condition_id=token,
        token_id=token,
        side=side,
        size=D(notional) / D(price),
        price=D(price),
        title=token,
        outcome="Yes",
        slug=token,
        received_at=time.time(),
        received_monotonic=time.monotonic(),
    )


def entry(**changes):
    values = {
        "leader_notional": D(40),
        "leader_shares": D(80),
        "reference_notional": D(20),
        "base_budget": D(5),
        "max_budget": D(30),
        "max_multiplier": D(3),
        "spent": D(0),
        "closed": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def decision(state=None, **changes):
    values = {
        "ask": D("0.5"),
        "event_price": D("0.5"),
        "cash": D(100),
        "exposure_room": D(50),
        "current_max": D(30),
        "fee_rate": D(0),
        "slippage_bps": 500,
        "min_notional": D("1.1"),
        "min_shares": D(1),
    }
    values.update(changes)
    return entry_budget(state or entry(), **values)


def test_sampling_groups_fragments_per_token_and_source_bucket():
    sample = sample_entries(
        [event("a", "10"), event("b", "10", timestamp=101),
         event("c", "40", timestamp=102), event("d", "60", token="other")],
        before=106, seconds=2, min_samples=3,
    )
    assert sample.reference_notional == 40
    assert sample.sample_count == 3
    assert (sample.sample_start, sample.sample_end) == (100, 104)


def test_sampling_deduplicates_events_before_summing():
    same = event("duplicate", "10")
    sample = sample_entries(
        [same, same, event("other", "30", timestamp=102)],
        before=104, seconds=2, min_samples=2,
    )
    assert sample.reference_notional == 20
    assert sample.sample_count == 2


def test_sampling_trims_extremes_not_fragments():
    notionals = ["1"] + ["20"] * 8 + ["1000"]
    sample = sample_entries(
        [event(str(i), amount, timestamp=100 + i * 2) for i, amount in enumerate(notionals)],
        before=120, seconds=2, min_samples=3,
    )
    assert sample.reference_notional == 20
    assert sample.sample_count == 10


def test_sampling_excludes_open_future_and_older_than_seven_day_buckets():
    cutoff = 1_800_000_101
    sample = sample_entries(
        [event("closed", "20", timestamp=cutoff - 3),
         event("open", "200", timestamp=cutoff - 1),
         event("future", "2000", timestamp=cutoff + 1),
         event("ancient", "3000", timestamp=cutoff - 7 * 86400 - 3)],
        before=cutoff, seconds=2, min_samples=1,
    )
    assert sample.reference_notional == 20
    assert sample.sample_count == 1
    assert sample.sample_end <= cutoff


def test_sampling_excludes_buy_sell_mixtures_even_if_sell_seen_first():
    sample = sample_entries(
        [event("sell", "10", side="SELL"), event("buy", "100"),
         event("valid", "20", timestamp=102),
         event("sell-only", "99", timestamp=104, side="SELL")],
        before=106, seconds=2, min_samples=1,
    )
    assert sample.reference_notional == 20
    assert sample.sample_count == 1


def test_sampling_sell_only_and_invalid_buys_do_not_meet_sample_minimum():
    invalid = event("invalid", "20")
    invalid.size = D("NaN")
    activities = [event("sell", "20", side="SELL"), invalid,
                  event("valid", "20", timestamp=102)]
    assert sample_entries(activities, before=104, seconds=2, min_samples=2) is None


def test_sampling_does_not_leak_current_burst_into_reference():
    activities = [event("old", "20", timestamp=98), event("current", "400", timestamp=100)]
    sample = sample_entries(activities, before=100, seconds=2, min_samples=1)
    assert sample.reference_notional == 20
    assert sample.sample_end == 100


@pytest.mark.parametrize("timestamp,seconds,expected", [(100, 2, 100), (101, 2, 100), (104, 3, 102)])
def test_bucket_uses_source_time_not_arrival_time(timestamp, seconds, expected):
    assert entry_bucket(timestamp, seconds) == expected


def test_entry_budget_is_relative_to_typical_entry_not_fixed_five_dollars():
    result = decision()
    assert result.target_budget == 10
    assert result.order_budget == 10
    assert result.reason is None


def test_entry_budget_subtracts_prior_actual_all_in_spend():
    result = decision(entry(spent=D("3.15")))
    assert result.target_budget == 10
    assert result.order_budget == D("6.85")


def test_entry_budget_limits_large_leader_entries_to_multiplier():
    result = decision(entry(leader_notional=D(1000), leader_shares=D(2000)))
    assert result.target_budget == 15
    assert result.order_budget == 15


@pytest.mark.parametrize("ask,expected_factor", [("0.4", "1"), ("0.5", "1"), ("0.52", None)])
def test_price_factor_never_increases_budget_for_cheaper_price(ask, expected_factor):
    result = decision(ask=D(ask))
    factor = D(expected_factor) if expected_factor else D("0.5") / D(ask)
    assert result.price_factor == factor
    assert result.target_budget == 10 * factor
    assert 0 <= result.price_factor <= 1


def test_slippage_uses_stricter_current_price_or_entry_vwap():
    current_is_lower = decision(event_price=D("0.4"))
    assert current_is_lower.reference_price == D("0.4")
    assert current_is_lower.reason == "no_liquidity_within_slippage"
    current_is_higher = decision(event_price=D("0.6"), ask=D("0.55"))
    assert current_is_higher.reference_price == D("0.5")
    assert current_is_higher.reason == "no_liquidity_within_slippage"


@pytest.mark.parametrize("constraint", ["cash", "exposure_room", "current_max"])
def test_notional_and_fee_together_respect_every_cash_cap(constraint):
    result = decision(fee_rate=D("0.07"), **{constraint: D(3)})
    all_in = result.order_budget * D("1.035")
    assert abs(all_in - 3) < D("1e-20")
    assert result.reason is None


def test_entry_start_max_budget_cannot_be_raised_mid_burst():
    result = decision(entry(max_budget=D(3)), current_max=D(30))
    assert result.target_budget == 3
    assert result.order_budget == 3


@pytest.mark.parametrize("minimum", [{"min_notional": D(11)}, {"min_shares": D(21)}])
def test_minimum_never_inflates_a_budget(minimum):
    result = decision(**minimum)
    assert result.target_budget == 10
    assert result.order_budget == 10
    assert result.reason == "sizing_below_minimum"


def test_spent_target_and_closed_entry_cannot_receive_new_funds():
    result = decision(entry(spent=D(12)))
    assert result.order_budget == 0
    assert result.reason == "sizing_entry_budget_used"
    assert decision(entry(closed=True)).reason == "sizing_entry_closed"


@pytest.fixture
async def sizing_rig(tmp_path, monkeypatch):
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sizing.db'}")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr("app.engine.SessionLocal", sessions)
    timestamp = entry_bucket(int(time.time()), 2)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Account(id=1, paper_balance=100, starting_balance=100,
                            trade_size=5, max_trade_size=30))
        for leader_id in (1, 2):
            session.add(Leader(id=leader_id, address="0x" + str(leader_id) * 40,
                               initialized=True, last_timestamp=timestamp - 2))
            session.add(LeaderSizingProfile(
                leader_id=leader_id, reference_notional=20, sample_count=10,
                sample_start=timestamp - 100, sample_end=timestamp - 2,
                refreshed_at=datetime.now(UTC),
            ))
        await session.commit()
    book = Book(
        bids=[(D("0.5"), D(1000))], asks=[(D("0.5"), D(1000))],
        tick_size=D("0.01"), min_order_size=D(1), neg_risk=False,
    )
    client = SimpleNamespace(
        get_market=AsyncMock(side_effect=lambda token: {
            "tokens": [{"token_id": token}], "closed": False,
            "accepting_orders": True, "seconds_delay": 0,
        }),
        get_fee_rate=AsyncMock(return_value=D(0)),
        get_book=AsyncMock(return_value=book),
        get_activity=AsyncMock(return_value=[]),
        get_resolution=AsyncMock(return_value=None),
        # It must never be needed to allocate an entry from our own cash.
        get_user_position_value=AsyncMock(side_effect=AssertionError("not leader capital")),
    )
    settings = Settings(
        _env_file=None, SMART_SIZING_ENABLED=True, COPY_BALANCE_PCT="0.05",
        SMART_SIZING_BURST_SECONDS=2, SMART_SIZING_MIN_SAMPLES=3,
        SMART_SIZING_MAX_MULTIPLIER=3, MIN_COPY_NOTIONAL="1.1",
        MAX_OUTCOME_EXPOSURE=50,
    )
    rig = SimpleNamespace(engine=CopyEngine(settings, client), client=client,
                          sessions=sessions, book=book, timestamp=timestamp)
    yield rig
    await rig.engine.stop()
    tasks = list(rig.engine._pending.values()) + list(rig.engine._leader_polls.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await db.dispose()


async def copy(rig, key, notional="20", *, leader_id=1, offset=0, **kwargs):
    activity = event(key, notional, timestamp=rig.timestamp + offset, **kwargs)
    rig.engine._schedule_copy(leader_id, activity)
    await asyncio.wait_for(asyncio.gather(*list(rig.engine._pending.values())), timeout=5)
    await asyncio.sleep(0)
    return activity


@pytest.mark.parametrize("fragments", [["40"], ["10"] * 4, ["0.5", "0.5", "1", "2", "36"]])
async def test_split_and_unsplit_entries_have_same_cumulative_spend(sizing_rig, fragments):
    for index, amount in enumerate(fragments):
        await copy(sizing_rig, f"buy-{index}", amount)
    async with sizing_rig.sessions() as session:
        account = await session.get(Account, 1)
        state = await session.scalar(select(SizingEntry))
        assert abs(account.paper_balance - 90) < TOLERANCE
        assert state.leader_notional == 40
        assert state.reference_notional == 20
        assert state.cash_at_start == 100
        assert state.base_budget == 5
        assert abs(state.spent - 10) < TOLERANCE
        assert len(list(await session.scalars(select(SizingEntry)))) == 1
    sizing_rig.client.get_user_position_value.assert_not_awaited()


async def test_subminimum_fragments_accumulate_without_rounding_up(sizing_rig):
    await copy(sizing_rig, "tiny-1", "2")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert await session.scalar(select(Position)) is None
        assert (await session.scalar(select(SizingEntry))).spent == 0
    await copy(sizing_rig, "tiny-2", "2")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
    await copy(sizing_rig, "tiny-3", "2")
    async with sizing_rig.sessions() as session:
        assert abs((await session.get(Account, 1)).paper_balance - D("98.5")) < TOLERANCE
        assert (await session.scalar(select(SizingEntry))).leader_notional == 6
        assert (await session.scalar(select(Position))).shares == 3


async def test_exact_replay_does_not_increase_entry_or_spend(sizing_rig):
    await copy(sizing_rig, "unique", "40")
    await copy(sizing_rig, "unique", "40")
    await sizing_rig.engine.stop()
    sizing_rig.engine = CopyEngine(sizing_rig.engine.settings, sizing_rig.client)
    await copy(sizing_rig, "unique", "40")
    async with sizing_rig.sessions() as session:
        assert len(list(await session.scalars(select(CopyTrade)))) == 1
        assert len(list(await session.scalars(select(PaperOrder)))) == 1
        assert (await session.scalar(select(SizingEntry))).leader_notional == 40
        assert abs((await session.get(Account, 1)).paper_balance - 90) < TOLERANCE


async def test_restart_preserves_entry_cash_reference_and_cumulative_target(sizing_rig):
    await copy(sizing_rig, "before-restart", "10")
    await sizing_rig.engine.stop()
    async with sizing_rig.sessions() as session:
        (await session.get(LeaderSizingProfile, 1)).reference_notional = 200
        await session.commit()
    sizing_rig.engine = CopyEngine(sizing_rig.engine.settings, sizing_rig.client)
    await copy(sizing_rig, "after-restart", "10")
    async with sizing_rig.sessions() as session:
        state = await session.scalar(select(SizingEntry))
        assert state.reference_notional == 20
        assert state.base_budget == 5
        assert state.cash_at_start == 100
        assert state.leader_notional == 20
        assert abs(state.spent - 5) < TOLERANCE
        assert abs((await session.get(Account, 1)).paper_balance - 95) < TOLERANCE


async def test_leader_profiles_and_entry_budgets_are_independent(sizing_rig):
    async with sizing_rig.sessions() as session:
        (await session.get(LeaderSizingProfile, 2)).reference_notional = 40
        await session.commit()
    await copy(sizing_rig, "leader-one", "20", leader_id=1)
    await copy(sizing_rig, "leader-two", "20", leader_id=2)
    async with sizing_rig.sessions() as session:
        entries = list(await session.scalars(select(SizingEntry).order_by(SizingEntry.leader_id)))
        assert len(entries) == 2
        assert entries[0].reference_notional == 20
        assert entries[1].reference_notional == 40
        assert abs(entries[0].spent - 5) < TOLERANCE
        assert abs(entries[1].spent - D("2.375")) < TOLERANCE
        assert abs((await session.get(Account, 1)).paper_balance - D("92.625")) < TOLERANCE


async def test_sell_closes_only_same_leader_same_and_older_buckets(sizing_rig):
    await copy(sizing_rig, "old", "20")
    await copy(sizing_rig, "current", "20", offset=2)
    await copy(sizing_rig, "future", "20", offset=4)
    await copy(sizing_rig, "other-leader", "20", leader_id=2)
    await copy(sizing_rig, "exit", "40", side="SELL", offset=2)
    async with sizing_rig.sessions() as session:
        entries = list(await session.scalars(select(SizingEntry)))
        states = {(state.leader_id, state.bucket_start): state.closed for state in entries}
        assert states[(1, sizing_rig.timestamp)] is True
        assert states[(1, sizing_rig.timestamp + 2)] is True
        assert states[(1, sizing_rig.timestamp + 4)] is False
        assert states[(2, sizing_rig.timestamp)] is False


async def test_fill_fees_are_part_of_cumulative_cash_limit(sizing_rig):
    sizing_rig.client.get_fee_rate.return_value = D("0.07")
    async with sizing_rig.sessions() as session:
        (await session.get(Account, 1)).max_trade_size = 3
        await session.commit()
    await copy(sizing_rig, "fee-limited", "40")
    async with sizing_rig.sessions() as session:
        state = await session.scalar(select(SizingEntry))
        account = await session.get(Account, 1)
        order = await session.scalar(select(PaperOrder))
        assert order.status == "filled"
        assert order.fee > 0
        assert D("2.9999") <= state.spent <= D(3)
        assert D(97) <= account.paper_balance <= D("97.0001")


async def test_slippage_rejection_does_not_consume_cumulative_cash_budget(sizing_rig):
    sizing_rig.book.asks = [(D("0.53"), D(1000))]
    await copy(sizing_rig, "too-expensive", "20")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert (await session.scalar(select(SizingEntry))).spent == 0
        assert (await session.scalar(select(CopyTrade))).skip_reason == "no_liquidity_within_slippage"
    sizing_rig.book.asks = [(D("0.5"), D(1000))]
    await copy(sizing_rig, "normal-price", "20")
    async with sizing_rig.sessions() as session:
        assert abs((await session.get(Account, 1)).paper_balance - 90) < TOLERANCE


async def test_audit_explains_target_and_remaining_order_budget(sizing_rig):
    await copy(sizing_rig, "first-part", "10")
    await copy(sizing_rig, "second-part", "10")
    async with sizing_rig.sessions() as session:
        audits = list(await session.scalars(select(SizingAudit).order_by(SizingAudit.copy_trade_id)))
        assert len(audits) == 2
        assert audits[0].target_budget == D("2.5")
        assert audits[0].spent_before == 0
        assert audits[1].target_budget == 5
        assert audits[1].spent_before == D("2.5")
        assert audits[1].order_budget == D("2.5")


async def test_no_profile_does_not_silently_revert_to_fixed_dollar_copy(sizing_rig):
    async with sizing_rig.sessions() as session:
        await session.delete(await session.get(LeaderSizingProfile, 1))
        await session.commit()
    await copy(sizing_rig, "unknown-reference", "40")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert await session.scalar(select(Position)) is None
        trade = await session.scalar(select(CopyTrade))
        assert trade.status == "skipped"
        assert "sizing" in trade.skip_reason


@pytest.mark.parametrize("problem", ["future_sample", "stale_sample", "too_few_samples"])
async def test_unusable_profile_never_spends_cash(sizing_rig, problem):
    async with sizing_rig.sessions() as session:
        profile = await session.get(LeaderSizingProfile, 1)
        if problem == "future_sample":
            profile.sample_end = sizing_rig.timestamp + 2
        elif problem == "stale_sample":
            profile.sample_end = sizing_rig.timestamp - 7 * 86400 - 2
        else:
            profile.sample_count = 2
        await session.commit()
    await copy(sizing_rig, "no-safe-reference", "40")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert await session.scalar(select(Position)) is None
        assert (await session.scalar(select(CopyTrade))).skip_reason == "sizing_profile_unavailable"


async def test_new_bucket_snapshots_remaining_own_cash(sizing_rig):
    await copy(sizing_rig, "initial-entry", "20")
    await copy(sizing_rig, "new-entry", "20", offset=2)
    async with sizing_rig.sessions() as session:
        entries = list(await session.scalars(select(SizingEntry).order_by(SizingEntry.bucket_start)))
        assert len(entries) == 2
        assert entries[0].base_budget == 5
        assert entries[1].cash_at_start == 95
        assert entries[1].base_budget == D("4.75")
        assert entries[1].spent == D("4.75")
        assert (await session.get(Account, 1)).paper_balance == D("90.25")


async def test_late_buy_cannot_reopen_bucket_after_sell(sizing_rig):
    await copy(sizing_rig, "initial-entry", "20")
    await copy(sizing_rig, "exit", "20", side="SELL")
    await copy(sizing_rig, "late-entry-fragment", "20")
    async with sizing_rig.sessions() as session:
        state = await session.scalar(select(SizingEntry))
        assert state.closed is True
        assert state.leader_notional == 20
        assert (await session.get(Account, 1)).paper_balance == 100
        assert await session.scalar(select(Position)) is None
        late = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "late-entry-fragment"))
        assert late.skip_reason == "sizing_entry_closed"


async def test_sell_without_our_position_also_prevents_stale_entry(sizing_rig):
    await copy(sizing_rig, "exit-before-rest", "20", side="SELL", offset=2)
    await copy(sizing_rig, "late-buy", "20")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert await session.scalar(select(Position)) is None
        late = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "late-buy"))
        assert late.skip_reason == "sizing_entry_closed"


async def test_late_fragment_cannot_top_up_superseded_entry(sizing_rig):
    await copy(sizing_rig, "old-entry", "20")
    await copy(sizing_rig, "new-entry", "20", offset=2)
    await copy(sizing_rig, "late-old-fragment", "40")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == D("90.25")
        entries = list(await session.scalars(select(SizingEntry).order_by(SizingEntry.bucket_start)))
        assert entries[0].leader_notional == 20
        late = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "late-old-fragment"))
        assert late.skip_reason == "sizing_entry_closed"


async def test_partial_fill_consumes_only_actual_debit(sizing_rig):
    sizing_rig.book.asks = [(D("0.5"), D(3))]
    await copy(sizing_rig, "partial", "20")
    async with sizing_rig.sessions() as session:
        state = await session.scalar(select(SizingEntry))
        order = await session.scalar(select(PaperOrder))
        assert order.status == "partial"
        assert state.spent == D("1.5")
        assert (await session.get(Account, 1)).paper_balance == D("98.5")
    sizing_rig.book.asks = [(D("0.5"), D(1000))]
    await copy(sizing_rig, "next-fragment", "20")
    async with sizing_rig.sessions() as session:
        assert (await session.scalar(select(SizingEntry))).spent == 10
        assert (await session.get(Account, 1)).paper_balance == 90


async def test_profile_from_other_grouping_duration_cannot_size_new_entry(sizing_rig):
    async with sizing_rig.sessions() as session:
        (await session.get(LeaderSizingProfile, 1)).bucket_seconds = 4
        await session.commit()
    await copy(sizing_rig, "wrong-profile-duration", "40")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 100
        assert (await session.scalar(select(CopyTrade))).skip_reason == "sizing_profile_unavailable"


async def test_changed_duration_cannot_reallocate_existing_entry(sizing_rig):
    await copy(sizing_rig, "original-duration", "20")
    sizing_rig.engine.settings.smart_sizing_burst_seconds = 1
    await copy(sizing_rig, "changed-duration", "20")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 95
        state = await session.scalar(select(SizingEntry))
        assert state.bucket_seconds == 2
        assert state.leader_notional == 20
        late = await session.scalar(select(CopyTrade).where(CopyTrade.event_key == "changed-duration"))
        assert late.skip_reason == "sizing_entry_closed"


async def test_changed_duration_cannot_create_overlapping_entry(sizing_rig):
    sizing_rig.timestamp = entry_bucket(sizing_rig.timestamp, 4)
    sizing_rig.engine.settings.smart_sizing_burst_seconds = 4
    async with sizing_rig.sessions() as session:
        profile = await session.get(LeaderSizingProfile, 1)
        profile.bucket_seconds = 4
        profile.sample_end = sizing_rig.timestamp - 4
        await session.commit()
    await copy(sizing_rig, "four-second-entry", "20")
    sizing_rig.engine.settings.smart_sizing_burst_seconds = 2
    async with sizing_rig.sessions() as session:
        (await session.get(LeaderSizingProfile, 1)).bucket_seconds = 2
        await session.commit()
    await copy(sizing_rig, "overlapping-two-second-entry", "20", offset=2)
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 95
        assert len(list(await session.scalars(select(SizingEntry)))) == 1
        late = await session.scalar(select(CopyTrade).where(
            CopyTrade.event_key == "overlapping-two-second-entry"
        ))
        assert late.skip_reason == "sizing_entry_closed"


async def test_disabled_smart_mode_ignores_persisted_smart_profiles(sizing_rig):
    sizing_rig.engine.settings.smart_sizing_enabled = False
    await copy(sizing_rig, "legacy-mode", "40")
    async with sizing_rig.sessions() as session:
        # Legacy LEADER_ORDER_SCALE=.1 yields $4, unlike smart mode's $10.
        assert (await session.get(Account, 1)).paper_balance == 96
        assert await session.scalar(select(SizingEntry)) is None
        assert await session.scalar(select(SizingAudit)) is None


async def test_switching_legacy_to_smart_cannot_remint_same_entry_budget(sizing_rig):
    sizing_rig.engine.settings.smart_sizing_enabled = False
    await copy(sizing_rig, "legacy-fill", "40")
    sizing_rig.engine.settings.smart_sizing_enabled = True
    await copy(sizing_rig, "smart-fragment-of-legacy-entry", "20")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 96
        assert await session.scalar(select(SizingEntry)) is None
        late = await session.scalar(select(CopyTrade).where(
            CopyTrade.event_key == "smart-fragment-of-legacy-entry"
        ))
        assert late.skip_reason == "sizing_entry_closed"
    # A genuinely new bucket remains eligible after the mode switch.
    await copy(sizing_rig, "new-smart-entry", "20", offset=2)
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == D("91.2")
        assert (await session.scalar(select(SizingEntry))).spent == D("4.8")


async def test_profile_refresh_excludes_checkpoint_bucket_and_newer_events(sizing_rig):
    activities = [
        event("old-a", "10", timestamp=sizing_rig.timestamp - 6),
        event("old-b", "20", timestamp=sizing_rig.timestamp - 4),
        event("old-c", "30", timestamp=sizing_rig.timestamp - 2),
        event("current", "1000", timestamp=sizing_rig.timestamp),
        event("future", "10000", timestamp=sizing_rig.timestamp + 2),
    ]
    async with sizing_rig.sessions() as session:
        leader = await session.get(Leader, 1)
        leader.last_timestamp = sizing_rig.timestamp
        await sizing_rig.engine._refresh_sizing_profile(session, leader, activities)
        profile = await session.get(LeaderSizingProfile, 1)
        assert profile.reference_notional == 20
        assert profile.sample_count == 3
        assert profile.sample_end == sizing_rig.timestamp
        assert profile.bucket_seconds == 2
    assert sizing_rig.engine._leader_sizing_profiles[1].reference_notional == 20


async def test_restart_keeps_saved_profile_when_activity_window_has_no_samples(sizing_rig):
    await sizing_rig.engine.stop()
    sizing_rig.engine = CopyEngine(sizing_rig.engine.settings, sizing_rig.client)
    async with sizing_rig.sessions() as session:
        leader = await session.get(Leader, 1)
        await sizing_rig.engine._refresh_sizing_profile(session, leader, [])
    assert sizing_rig.engine._leader_sizing_profiles[1].reference_notional == 20
    await copy(sizing_rig, "saved-profile-entry", "40")
    async with sizing_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == 90


async def test_frequent_profile_refresh_attempt_keeps_daily_reference(sizing_rig):
    activities = [event(f"old-{i}", "20", timestamp=sizing_rig.timestamp - 10 - i * 2)
                  for i in range(3)]
    async with sizing_rig.sessions() as session:
        leader = await session.get(Leader, 1)
        await sizing_rig.engine._refresh_sizing_profile(session, leader, activities)
        profile = await session.get(LeaderSizingProfile, 1)
        first_refreshed_at = profile.refreshed_at
        bigger = [event(f"big-{i}", "200", timestamp=sizing_rig.timestamp - 10 - i * 2)
                  for i in range(3)]
        await sizing_rig.engine._refresh_sizing_profile(session, leader, bigger)
        assert profile.reference_notional == 20
        assert profile.refreshed_at == first_refreshed_at


async def test_account_bootstrap_uses_settings_without_overwriting_existing_account(sizing_rig):
    settings = sizing_rig.engine.settings.model_copy(update={
        "default_trade_size": D(7), "max_trade_size": D(24), "default_slippage_bps": 300,
    })
    async with sizing_rig.sessions() as session:
        await session.delete(await session.get(Account, 1))
        await session.commit()
        account = await get_or_create_account(session, D(123), settings=settings)
        assert account.paper_balance == 123
        assert account.starting_balance == 123
        assert account.trade_size == 7
        assert account.max_trade_size == 24
        assert account.slippage_bps == 300
        await session.commit()
    async with sizing_rig.sessions() as session:
        different = settings.model_copy(update={"max_trade_size": D(99)})
        account = await get_or_create_account(session, D(999), settings=different)
        assert account.paper_balance == 123
        assert account.starting_balance == 123
        assert account.trade_size == 7
        assert account.max_trade_size == 24
