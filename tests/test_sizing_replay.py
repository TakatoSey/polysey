"""Replaying stored entries must never quietly guess at a row it cannot model."""

from decimal import Decimal as D
from types import SimpleNamespace

from app.sizing import entry_budget
from app.sizing_replay import Parameters, raw_target, summarize

CURRENT = Parameters(D("1.5"), D("0.5"), D(3))


def audit(**changes):
    values = {
        "copy_trade_id": 1,
        "base_budget": D(5),
        "reference_notional": D(20),
        "leader_notional": D(40),
        "leader_vwap": D("0.5"),
        "price_factor": D(1),
        "odds_factor": D(1),
        "target_budget": D("14.142135"),
        "spent_before": D(0),
        "order_budget": D("14.142135"),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_replay_reproduces_what_live_sizing_actually_produced():
    # The audit row a live entry would have written, recomputed by the replay.
    entry = SimpleNamespace(
        leader_notional=D(40),
        leader_shares=D(80),
        reference_notional=D(20),
        base_budget=D(5),
        max_budget=D(1000),
        max_multiplier=D(3),
        spent=D(0),
        closed=False,
    )
    live = entry_budget(
        entry,
        ask=D("0.5"),
        event_price=D("0.5"),
        cash=D(1000),
        exposure_room=D(1000),
        current_max=D(1000),
        fee_rate=D(0),
        slippage_price=D("0.05"),
        min_notional=D("1.1"),
        min_shares=D(1),
        conviction_power=CURRENT.conviction_power,
        odds_weight=CURRENT.odds_weight,
    )
    row = audit(target_budget=live.target_budget, odds_factor=live.odds_factor)

    assert abs(raw_target(row, CURRENT) - live.target_budget) < D("0.0001")


def test_a_clamped_row_is_excluded_rather_than_guessed():
    # target_budget below the formula means a cap bound it; the replay cannot
    # know that cap, so the row must not be projected.
    clamped = audit(target_budget=D(3))

    report = summarize([clamped], CURRENT, Parameters(D(2), D("0.3"), D(3)))

    assert report["projected"] == 0
    assert report["excluded"] == 1
    assert report["was_total"] == 0


def test_a_stronger_power_raises_an_above_norm_entry():
    row = audit()
    row.target_budget = raw_target(row, CURRENT)

    report = summarize([row], CURRENT, Parameters(D(2), D("0.5"), D(3)))

    assert report["projected"] == 1
    assert report["bigger"] == 1
    assert report["becomes_total"] > report["was_total"]


def test_a_row_without_a_reference_notional_is_never_projected():
    report = summarize([audit(reference_notional=D(0))], CURRENT, CURRENT)
    assert (report["projected"], report["excluded"]) == (0, 1)
