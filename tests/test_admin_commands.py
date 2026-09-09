from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.bot as bot_module
from app.bot import TelegramApp
from app.db import Base
from app.models import (
    Account,
    CopyTrade,
    Leader,
    LeaderSizingProfile,
    PaperOrder,
    Position,
    SizingEntry,
)
from app.priority import PriorityLock

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
    )
    app.engine = SimpleNamespace(_ledger_lock=PriorityLock(), _buy_batches={"stale": object()})
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


async def test_reset_clears_trading_state_and_restores_the_starting_balance(admin_rig):
    balance = await admin_rig.app._reset_database()

    assert balance == D(100)
    async with admin_rig.sessions() as session:
        account = await session.get(Account, 1)
        assert account.paper_balance == D(100)
        assert account.starting_balance == D(100)
        assert account.realized_pnl == 0
        for model in (CopyTrade, PaperOrder, Position, SizingEntry):
            assert list(await session.scalars(select(model))) == []
        # A fresh test still tracks the same leaders and their public statistics.
        leader = await session.get(Leader, 1)
        assert leader is not None and leader.last_timestamp == 99
        assert (await session.get(LeaderSizingProfile, 1)).sample_count == 9
    assert admin_rig.app.engine._buy_batches == {}


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
