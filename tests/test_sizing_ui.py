from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import SIZING_REASONS, TelegramApp
from app.db import Base
from app.models import Account, Leader


def panel(*, smart=True, profiles=None):
    app = object.__new__(TelegramApp)
    app.settings = SimpleNamespace(
        smart_sizing_enabled=smart,
        # The panel shows the trading mode on every screen.
        live=False,
        live_dry_run=False,
        copy_balance_pct=Decimal("0.05"),
        leader_order_scale=Decimal("0.1"),
        smart_sizing_max_multiplier=Decimal(3),
        smart_sizing_burst_seconds=2,
        smart_sizing_min_samples=3,
        sizing_conviction_power=Decimal("1.5"),
        sizing_odds_weight=Decimal("0.5"),
        min_copy_notional=Decimal("1.10"),
        paper_initial_balance=Decimal(100),
        telegram_allowed_user_id=7,
    )
    app.engine = SimpleNamespace(_leader_sizing_profiles=profiles or {})
    return app


ACCOUNT = SimpleNamespace(
    paper_balance=Decimal(150), trade_size=Decimal(5), max_trade_size=Decimal(30)
)


def test_smart_settings_explain_percentage_and_do_not_present_five_dollars_as_base():
    text = panel()._sizing_summary(ACCOUNT)
    assert "5.0% свободных денег" in text
    assert "сейчас $7.50" in text
    assert "$30.00, включая комиссию" in text
    assert "$5.00" not in text
    assert "/setsize не влияет" in text


def test_classic_settings_keep_fixed_size_and_leader_scale():
    text = panel(smart=False)._sizing_summary(ACCOUNT)
    assert "классический" in text
    assert "$5.00" in text
    assert "10.0%" in text


def test_profile_warming_up_is_visible_until_minimum_samples():
    app = panel(
        profiles={
            1: SimpleNamespace(reference_notional=Decimal(20), sample_count=2),
        }
    )
    assert not app._sizing_profile_ready(1)
    text = app._leader_sizing_text(1)
    assert "2 из 3 серий" in text
    assert "BUY пропускаются" in text
    assert "0 из 3 серий" in app._leader_sizing_text(2)


@pytest.mark.parametrize(
    "stamp",
    [
        datetime(2026, 9, 6, 12, 30),
        datetime(2026, 9, 6, 12, 30, tzinfo=UTC),
        datetime(2026, 9, 6, 15, 30, tzinfo=timezone(timedelta(hours=3))),
    ],
)
def test_profile_shows_grouped_entry_sample_count_and_utc_time(stamp):
    app = panel(
        profiles={
            1: SimpleNamespace(
                reference_notional=Decimal("14.60"),
                sample_count=42,
                refreshed_at=stamp,
            ),
        }
    )
    text = app._leader_sizing_text(1)
    assert "Типичная серия" in text
    assert "$14.60" in text
    assert "42 в выборке" in text
    assert "06.09 12:30 UTC" in text


def test_sizing_help_discloses_bucket_boundaries_and_residual_accounting():
    text = panel()._sizing_help()
    assert "уже потраченное" in text
    assert "включая комиссии" in text
    assert "На границе окна" in text
    assert "не ждёт окончания окна" in text
    assert "Лучшая цена бюджет не увеличивает" in text
    assert "не баланс трейдера" in text


def test_new_rejections_have_human_readable_descriptions():
    assert set(SIZING_REASONS) >= {
        "sizing_profile_unavailable",
        "sizing_entry_closed",
        "sizing_below_minimum",
        "sizing_entry_budget_used",
        "sizing_exposure_limit",
        "stale_signal",
    }
    assert all("_" not in value for value in SIZING_REASONS.values())


@pytest.mark.asyncio
async def test_non_finite_maximum_is_rejected_without_database_write():
    app = panel()
    app._edit_panel = AsyncMock()
    message = SimpleNamespace(
        text="/setmax NaN",
        from_user=SimpleNamespace(id=7),
        chat=SimpleNamespace(id=7),
        delete=AsyncMock(),
    )
    await app.setmax(message)
    assert app._edit_panel.await_args.args[0] == "Формат: /setmax 5"


@pytest.mark.asyncio
async def test_fixed_leader_size_is_saved_from_single_message_panel(tmp_path, monkeypatch):
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ui.db'}")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr("app.bot.SessionLocal", sessions)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Account(id=1, paper_balance=100, starting_balance=100, max_trade_size=30))
        session.add(Leader(id=1, address="0x" + "1" * 40, initialized=True))
        await session.commit()
    app = panel()
    app._delete_input = AsyncMock()
    app._leader_detail = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"leader_id": 1, "page": 0}
    message = SimpleNamespace(
        text="7,50",
        from_user=SimpleNamespace(id=7),
        chat=SimpleNamespace(id=7),
    )
    try:
        await app.receive_leader_fixed(message, state)
        async with sessions() as session:
            assert (await session.get(Leader, 1)).fixed_trade_size == Decimal("7.50")
        state.clear.assert_awaited_once()
        app._leader_detail.assert_awaited_once_with(1, 0, 7)
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_fixed_leader_percent_replaces_fixed_size(tmp_path, monkeypatch):
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'percent-ui.db'}")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr("app.bot.SessionLocal", sessions)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Account(id=1, paper_balance=100, starting_balance=100, max_trade_size=30))
        session.add(
            Leader(
                id=1,
                address="0x" + "1" * 40,
                initialized=True,
                fixed_trade_size=Decimal("7.50"),
            )
        )
        await session.commit()
    app = panel()
    app._delete_input = AsyncMock()
    app._leader_detail = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"leader_id": 1, "page": 0}
    message = SimpleNamespace(text="5", from_user=SimpleNamespace(id=7), chat=SimpleNamespace(id=7))
    try:
        await app.receive_leader_percent(message, state)
        async with sessions() as session:
            leader = await session.get(Leader, 1)
            assert leader.fixed_trade_percent == Decimal("5")
            assert leader.fixed_trade_size is None
        state.clear.assert_awaited_once()
    finally:
        await db.dispose()


@pytest.mark.parametrize(
    "entered, stored",
    [("250", Decimal(250)), ("1000", Decimal(1000)), ("0", None), ("1001", None), ("abc", None)],
)
@pytest.mark.asyncio
async def test_leader_percent_accepts_above_one_hundred_and_rejects_out_of_range(
    tmp_path, monkeypatch, entered, stored
):
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'percent-range.db'}")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr("app.bot.SessionLocal", sessions)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Account(id=1, paper_balance=100, starting_balance=100, max_trade_size=30))
        session.add(Leader(id=1, address="0x" + "2" * 40, initialized=True))
        await session.commit()
    app = panel()
    app._delete_input = AsyncMock()
    app._leader_detail = AsyncMock()
    app._edit_panel = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"leader_id": 1, "page": 0}
    message = SimpleNamespace(
        text=entered, from_user=SimpleNamespace(id=7), chat=SimpleNamespace(id=7)
    )
    try:
        await app.receive_leader_percent(message, state)
        async with sessions() as session:
            assert (await session.get(Leader, 1)).fixed_trade_percent == stored
        assert app._leader_detail.await_count == (1 if stored is not None else 0)
    finally:
        await db.dispose()
