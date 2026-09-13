from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.bot as bot_module
from app.bot import TelegramApp
from app.config import Settings
from app.db import Base
from app.engine import CopyEngine
from app.models import (
    Account,
    CopyTrade,
    Leader,
    LeaderPosition,
    LeaderSizingProfile,
    PaperOrder,
    Position,
    SizingEntry,
)
from app.repository import get_leaders, remove_leader

D = Decimal


@pytest.fixture
async def admin_rig(tmp_path, monkeypatch):
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr(bot_module, "SessionLocal", sessions)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(
            Account(id=1, paper_balance=D("40"), starting_balance=D(100), realized_pnl=D("-3"))
        )
        session.add(Leader(id=1, address="0x" + "a" * 40, initialized=True, last_timestamp=99))
        session.add(
            LeaderSizingProfile(
                leader_id=1,
                reference_notional=D(20),
                sample_count=9,
                sample_start=1,
                sample_end=2,
            )
        )
        session.add(
            CopyTrade(
                id=1,
                leader_id=1,
                event_key="event",
                timestamp=1,
                token_id="token",
                condition_id="condition",
                side="BUY",
                leader_size=D(10),
                leader_price=D("0.5"),
                status="executed",
            )
        )
        session.add(
            PaperOrder(
                id=1,
                copy_trade_id=1,
                token_id="token",
                side="BUY",
                requested_shares=D(10),
                filled_shares=D(10),
                average_fill_price=D("0.5"),
                fee=D(0),
                status="filled",
            )
        )
        session.add(
            Position(
                id=1,
                token_id="token",
                condition_id="condition",
                title="Market",
                outcome="Yes",
                shares=D(10),
                average_price=D("0.5"),
                cost_basis=D(5),
            )
        )
        session.add(
            SizingEntry(
                leader_id=1,
                token_id="token",
                bucket_start=0,
                cash_at_start=D(100),
                base_budget=D(5),
                reference_notional=D(20),
                max_budget=D(30),
                max_multiplier=D(3),
            )
        )
        await session.commit()
    app = object.__new__(TelegramApp)
    app.settings = SimpleNamespace(
        paper_initial_balance=D(100),
        telegram_allowed_user_id=7,
        default_slippage_cents=D(5),
        # Read by the settings screen this rig also exercises.
        smart_sizing_enabled=True,
        # The panel shows the trading mode on every screen.
        live=False,
        live_dry_run=False,
        copy_balance_pct=D("0.05"),
        leader_order_scale=D("0.1"),
        smart_sizing_max_multiplier=D(3),
        smart_sizing_burst_seconds=2,
        smart_sizing_min_samples=3,
        min_copy_notional=D("1.10"),
        max_outcome_exposure=D(50),
        min_cash_reserve_pct=D("0.25"),
        exit_retry_enabled=True,
    )
    engine = CopyEngine(Settings(_env_file=None), SimpleNamespace())
    # State a wiped database can no longer explain, as a live bot would hold it.
    engine._buy_batches["stale"] = object()
    engine._sell_watermarks[(1, "token")] = 99
    engine._leader_floors[1] = 99
    engine._leader_sizing_profiles[1] = object()
    engine._profile_refresh_attempt[1] = 0.0
    app.engine = engine
    app._edit_panel = AsyncMock()
    app._delete_input = AsyncMock()
    yield SimpleNamespace(app=app, sessions=sessions)
    await db.dispose()


def command(text):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=7),
        chat=SimpleNamespace(id=7),
    )


async def test_deposit_raises_cash_and_starting_balance_so_pnl_stays_a_result(admin_rig):
    await admin_rig.app.addbalance(command("/addbalance 25.50"))
    async with admin_rig.sessions() as session:
        account = await session.get(Account, 1)
        assert account.paper_balance == D("65.50")
        # Depositing must not read as profit, nor erase the loss already taken.
        assert account.starting_balance == D("125.50")
        assert account.realized_pnl == D("-3")


@pytest.mark.parametrize(
    "text", ["/addbalance", "/addbalance 0", "/addbalance -5", "/addbalance x"]
)
async def test_deposit_rejects_bad_amounts_without_touching_the_account(admin_rig, text):
    await admin_rig.app.addbalance(command(text))
    assert admin_rig.app._edit_panel.await_args.args[0] == "Формат: /addbalance 50"
    async with admin_rig.sessions() as session:
        assert (await session.get(Account, 1)).paper_balance == D(40)


async def test_reset_clears_everything_traded_and_keeps_only_the_leaders(admin_rig):
    async with admin_rig.sessions() as session:
        leader = await session.get(Leader, 1)
        leader.fixed_trade_percent, leader.min_buy_price = D(50), D("0.10")
        await session.commit()

    balance = await admin_rig.app._reset_database()

    assert balance == D(100)
    async with admin_rig.sessions() as session:
        account = await session.get(Account, 1)
        assert account.paper_balance == D(100)
        assert account.starting_balance == D(100)
        assert account.realized_pnl == 0
        for model in (CopyTrade, PaperOrder, Position, SizingEntry, LeaderSizingProfile):
            assert list(await session.scalars(select(model))) == []
        # The leaders themselves stay, with the settings chosen for them.
        leader = await session.get(Leader, 1)
        assert leader is not None
        assert (leader.fixed_trade_percent, leader.min_buy_price) == (D(50), D("0.10"))
    # A sell barrier or poll checkpoint left in memory would keep skipping
    # copies against trades the database no longer holds.
    engine = admin_rig.app.engine
    assert engine._buy_batches == {}
    assert engine._sell_watermarks == {}
    assert engine._leader_floors == {}
    assert engine._leader_sizing_profiles == {}
    assert engine._profile_refresh_attempt == {}


async def test_deleting_a_leader_without_history_really_deletes_the_row(admin_rig):
    async with admin_rig.sessions() as session:
        session.add(Leader(id=2, address="0x" + "b" * 40, initialized=True))
        session.add(
            LeaderSizingProfile(
                leader_id=2,
                reference_notional=D(20),
                sample_count=5,
                sample_start=1,
                sample_end=2,
            )
        )
        session.add(LeaderPosition(leader_id=2, token_id="token", shares=D(0)))
        await session.commit()
    engine = admin_rig.app.engine
    engine._leader_sizing_profiles[2] = object()
    engine._sell_watermarks[(2, "token")] = 99

    await admin_rig.app._dispatch("leader_remove_confirm:2:0", 7)

    async with admin_rig.sessions() as session:
        assert await session.get(Leader, 2) is None
        assert await session.get(LeaderSizingProfile, 2) is None
        assert list(await session.scalars(select(LeaderPosition))) == []
    # Only the deleted leader is forgotten; the other one keeps its state.
    assert 2 not in engine._leader_sizing_profiles
    assert list(engine._sell_watermarks) == [(1, "token")]
    assert 1 in engine._leader_sizing_profiles


async def test_deleting_a_traded_leader_hides_them_without_orphaning_history(admin_rig):
    await admin_rig.app._dispatch("leader_remove_confirm:1:0", 7)

    async with admin_rig.sessions() as session:
        leader = await session.get(Leader, 1)
        # The row has to stay: orders, released cost and PNL are attributed to it.
        assert leader is not None
        assert (leader.active, leader.removed) == (False, True)
        assert await get_leaders(session) == []
        assert (await session.scalar(select(CopyTrade))).leader_id == 1
    # Barriers for a leader we stopped following are dropped with them.
    assert admin_rig.app.engine._sell_watermarks == {}
    assert admin_rig.app.engine._leader_floors == {}


async def test_adding_a_deleted_address_again_brings_the_same_leader_back(admin_rig):
    async with admin_rig.sessions() as session:
        leader = await session.get(Leader, 1)
        await remove_leader(session, leader)
        await session.commit()

    await admin_rig.app._save_leader("0x" + "a" * 40, 7)

    async with admin_rig.sessions() as session:
        leader = await session.get(Leader, 1)
        assert (leader.active, leader.removed) == (True, False)
        assert [row.id for row in await get_leaders(session)] == [1]


async def test_stats_screen_reports_copy_rate_and_ranks_skip_reasons(admin_rig):
    async with admin_rig.sessions() as session:
        for index, (status, reason) in enumerate(
            [
                ("skipped", "no_liquidity_within_slippage"),
                ("skipped", "no_liquidity_within_slippage"),
                ("skipped", "sizing_below_minimum"),
                ("retry_pending", "entry_price_drop"),
            ]
        ):
            session.add(
                CopyTrade(
                    id=index + 2,
                    leader_id=1,
                    event_key=f"skip-{index}",
                    timestamp=1,
                    token_id="token",
                    condition_id="condition",
                    side="BUY",
                    leader_size=D(10),
                    leader_price=D("0.5"),
                    status=status,
                    skip_reason=reason,
                )
            )
        await session.commit()

    text = await admin_rig.app._stats_text()

    # One executed from the fixture, three skipped, one still waiting on price.
    assert "Сигналов BUY: <b>5</b>" in text
    assert "Исполнено: <b>1</b> (20%)" in text
    assert "Ждут цену: 1" in text
    assert "2 · цена вне slippage" in text
    assert "1 · минимум рынка выше нашего размера" in text


async def test_stats_screen_says_so_when_there_were_no_signals(admin_rig):
    async with admin_rig.sessions() as session:
        await session.execute(delete(CopyTrade))
        await session.commit()
    assert "Сигналов BUY не было" in await admin_rig.app._stats_text()


@pytest.mark.parametrize(
    "data", ["position:1", "leader_view:x:0", "leaders:abc", "portfolio:none", "leader_toggle:1"]
)
async def test_stale_keyboard_data_answers_instead_of_dying_silently(admin_rig, data):
    app = admin_rig.app
    app.callback = TelegramApp.callback.__get__(app)
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=7),
        message=SimpleNamespace(message_id=5),
        answer=AsyncMock(),
    )

    await app.callback(query)

    assert "устарела" in app._edit_panel.await_args.args[0]


async def test_reset_asks_before_wiping_and_only_the_owner_may_ask(admin_rig):
    await admin_rig.app.reset(command("/reset"))
    prompt = admin_rig.app._edit_panel.await_args.args[0]
    assert "Сбросить базу?" in prompt

    stranger = command("/reset")
    stranger.from_user = SimpleNamespace(id=999)
    admin_rig.app._edit_panel.reset_mock()
    await admin_rig.app.reset(stranger)
    admin_rig.app._edit_panel.assert_not_awaited()
    async with admin_rig.sessions() as session:
        assert list(await session.scalars(select(CopyTrade))) != []


async def test_buy_notification_toggle_flips_the_flag_and_its_own_button(admin_rig):
    async def labels():
        _text, keyboard = await admin_rig.app._settings_screen()
        return [button.text for row in keyboard.inline_keyboard for button in row]

    assert "🔕 Не уведомлять о покупках" in await labels()

    await admin_rig.app._dispatch("notify_buys_toggle", 7)

    async with admin_rig.sessions() as session:
        assert (await session.get(Account, 1)).notify_buys is False
    assert "🔔 Уведомлять о покупках" in await labels()
    assert "выключены" in await admin_rig.app._settings_text_v2()

    await admin_rig.app._dispatch("notify_buys_toggle", 7)

    async with admin_rig.sessions() as session:
        assert (await session.get(Account, 1)).notify_buys is True


async def test_stats_flag_fills_whose_fee_was_only_an_estimate(admin_rig):
    assert "оценке" not in await admin_rig.app._stats_text()
    async with admin_rig.sessions() as session:
        session.add(
            PaperOrder(
                id=2,
                copy_trade_id=1,
                token_id="token",
                side="BUY",
                requested_shares=D(4),
                filled_shares=D(4),
                average_fill_price=D("0.5"),
                fee=D("0.05"),
                status="filled",
                fee_estimated=True,
            )
        )
        await session.commit()

    text = await admin_rig.app._stats_text()

    # One of the two fills paid a fee we estimated ourselves.
    assert "Комиссия по оценке: 1 из 2" in text


async def seed_history(sessions, count, rejected=()):
    """Orders older than the former 30-row window, newest last."""
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with sessions() as session:
        for index in range(1, count + 1):
            session.add(
                PaperOrder(
                    id=index + 1,
                    copy_trade_id=1,
                    token_id="token",
                    side="BUY",
                    requested_shares=D(index),
                    filled_shares=D(0) if index in rejected else D(index),
                    average_fill_price=D("0.5"),
                    fee=D(0),
                    status="rejected" if index in rejected else "filled",
                    reason="no_liquidity" if index in rejected else None,
                    created_at=base + timedelta(minutes=index),
                )
            )
        await session.commit()


async def test_history_pages_over_everything_stored_not_the_newest_thirty(admin_rig):
    await seed_history(admin_rig.sessions, 39)

    text, keyboard = await admin_rig.app._orders_screen(0, "all")

    # 40 orders in total: the rig's own plus the 39 seeded.
    assert "Страница 1/5 · всего 40" in text
    # Eight rows: the rig's own order plus seeded 39 down to 33.
    assert "39.00 shares" in text and "33.00 shares" in text
    assert "32.00 shares" not in text
    pages = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("orders:")
    ]
    assert "orders:1:all" in pages

    # The oldest order used to be unreachable from Telegram entirely.
    last_page, _keyboard = await admin_rig.app._orders_screen(4, "all")
    assert "Страница 5/5" in last_page
    assert "1.00 shares" in last_page


async def test_a_page_beyond_the_end_clamps_instead_of_showing_nothing(admin_rig):
    await seed_history(admin_rig.sessions, 9)

    text, _keyboard = await admin_rig.app._orders_screen(99, "all")

    assert "Страница 2/2" in text
    assert "Записей нет." not in text


async def test_the_filter_counts_pages_over_that_filter_only(admin_rig):
    await seed_history(admin_rig.sessions, 20, rejected=(3, 7, 11))

    skipped, _keyboard = await admin_rig.app._orders_screen(0, "skip")
    filled, _keyboard = await admin_rig.app._orders_screen(0, "done")

    assert "Страница" not in skipped  # three rejections fit on one page
    assert skipped.count("Причина") == 3
    assert "Страница 1/3 · всего 18" in filled
