"""One instance = one Telegram token + one database.

A paper ledger holds invented money. Sizing a real order against it would
spend money that does not exist, and two bots on one token quietly steal each
other's updates. Both are prevented here rather than in a README.
"""

from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import TelegramApp
from app.config import Settings
from app.db import Base
from app.models import CopyTrade, InstanceClaim, Leader
from app.repository import DatabaseBelongsToAnotherBot, claim_database

WALLET = "0x" + "a" * 40
OTHER_WALLET = "0x" + "b" * 40


def paper_settings():
    return Settings(_env_file=None)


def live_settings(funder=WALLET):
    return Settings(
        _env_file=None,
        TRADING_MODE="live",
        LIVE_CONFIRM="I_UNDERSTAND_REAL_MONEY",
        POLYMARKET_PRIVATE_KEY="0x" + "ab" * 32,
        POLYMARKET_FUNDER=funder,
    )


@pytest.fixture
async def sessions(tmp_path):
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claim.db'}")
    maker = async_sessionmaker(db, expire_on_commit=False)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield maker
    await db.dispose()


async def seed_history(maker):
    async with maker() as session:
        session.add(Leader(id=1, address="0x" + "1" * 40, initialized=True))
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
        await session.commit()


async def claim(maker, settings):
    async with maker() as session:
        result = await claim_database(session, settings)
        await session.commit()
        return result


async def test_a_fresh_database_is_claimed_by_whoever_opens_it(sessions):
    stamped = await claim(sessions, paper_settings())

    assert (stamped.trading_mode, stamped.funder) == ("paper", "")
    # Opening it again in the same mode is ordinary.
    assert (await claim(sessions, paper_settings())).trading_mode == "paper"


async def test_a_live_bot_refuses_a_database_that_holds_paper_history(sessions):
    await claim(sessions, paper_settings())
    await seed_history(sessions)

    with pytest.raises(DatabaseBelongsToAnotherBot) as refusal:
        await claim(sessions, live_settings())

    message = str(refusal.value)
    # The message has to say what to do, not just that something is wrong.
    assert "paper" in message and "live" in message
    assert "DATABASE_URL" in message
    async with sessions() as session:
        assert (await session.get(InstanceClaim, 1)).trading_mode == "paper"


async def test_an_empty_database_may_change_mode(sessions):
    await claim(sessions, paper_settings())

    stamped = await claim(sessions, live_settings())

    # Nothing was recorded yet, so there is no ledger to mix.
    assert (stamped.trading_mode, stamped.funder) == ("live", WALLET)


async def test_two_wallets_never_share_one_live_ledger(sessions):
    await claim(sessions, live_settings())
    await seed_history(sessions)

    with pytest.raises(DatabaseBelongsToAnotherBot) as refusal:
        await claim(sessions, live_settings(funder=OTHER_WALLET))

    assert WALLET in str(refusal.value) and OTHER_WALLET in str(refusal.value)


async def test_the_same_live_wallet_keeps_its_database(sessions):
    await claim(sessions, live_settings())
    await seed_history(sessions)

    stamped = await claim(sessions, live_settings())

    assert (stamped.trading_mode, stamped.funder) == ("live", WALLET)


async def test_a_live_claim_without_a_wallet_adopts_the_configured_one(sessions):
    async with sessions() as session:
        session.add(InstanceClaim(id=1, trading_mode="live", funder=""))
        await session.commit()

    stamped = await claim(sessions, live_settings())

    assert stamped.funder == WALLET


# ------------------------------------------------------------ telegram identity


class FakeBot:
    def __init__(self, *, username="polyseyBot", conflict=False, unauthorized=False):
        self.username = username
        self.conflict = conflict
        self.unauthorized = unauthorized
        self.get_me = AsyncMock(side_effect=self._me)

    async def _me(self):
        if self.unauthorized:
            raise TelegramUnauthorizedError(method=None, message="Unauthorized")
        return SimpleNamespace(username=self.username, id=42)

    async def __call__(self, method):
        if self.conflict:
            raise TelegramConflictError(method=method, message="terminated by other getUpdates")
        return []


def panel(bot, settings=None):
    app = object.__new__(TelegramApp)
    app.settings = settings or paper_settings()
    app.bot = bot
    return app


async def test_the_bot_names_itself_on_start():
    app = panel(FakeBot(username="polyliveBot"), live_settings())

    assert await app.verify_identity() == "polyliveBot"


async def test_a_token_already_being_polled_stops_the_second_instance():
    app = panel(FakeBot(conflict=True))

    with pytest.raises(RuntimeError) as refusal:
        await app.verify_identity()

    message = str(refusal.value)
    # aiogram would retry this forever in silence, stealing updates instead.
    assert "already polling" in message
    assert "@BotFather" in message


async def test_an_invalid_token_is_named_as_such():
    app = panel(FakeBot(unauthorized=True))

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        await app.verify_identity()
