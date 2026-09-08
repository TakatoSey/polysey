from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import Base
from app.engine import CopyEngine
from app.models import Account, PaperOrder, Position


@pytest.mark.asyncio
async def test_user_positions_settle_once_and_release_cash(monkeypatch):
    db = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr("app.engine.SessionLocal", sessions)
    async with db.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with sessions() as session:
            session.add(Account(id=1, paper_balance=Decimal("1.76"), starting_balance=100))
            for token, shares, basis, outcome in [
                ("nyc-no", "70.3008", "25.81", "No"),
                ("bay-yes", "88.3183", "5.23", "Yes"),
                ("btc-up", "125.9913", "67.20", "Up"),
            ]:
                session.add(
                    Position(
                        token_id=token,
                        condition_id=token,
                        title=token,
                        outcome=outcome,
                        shares=Decimal(shares),
                        cost_basis=Decimal(basis),
                    )
                )
            await session.commit()
        client = AsyncMock()
        client.get_resolution.side_effect = lambda condition, outcome, token: Decimal(
            1 if token == "btc-up" else 0
        )
        engine = CopyEngine(Settings(_env_file=None), client)
        await engine.settle_once()
        await engine.settle_once()
        messages = [engine.notifications.get_nowait() for _ in range(3)]
        rendered = "\n".join(messages)
        assert "Positions Auto-Processed!" in rendered
        assert "btc-up" in rendered and "Up WON" in rendered
        assert "nyc-no" in rendered and "No LOST" in rendered
        assert "PnL:" in rendered
        async with sessions() as session:
            account = await session.get(Account, 1)
            assert account.paper_balance == Decimal("127.7513")
            assert account.realized_pnl == Decimal("27.7513")
            assert list(await session.scalars(select(Position))) == []
            records = list(await session.scalars(select(PaperOrder)))
            assert len(records) == 3
            assert all(order.status == "settled" for order in records)
    finally:
        await db.dispose()


def test_settlement_notification_has_payout_pnl_and_market_link():
    message = CopyEngine.build_settlement_notification(
        "Bitcoin Up or Down?",
        "Down",
        Decimal("1.447366"),
        Decimal(1),
        Decimal("1.10"),
        "btc-updown-5m-1788857400",
        "bitcoin-up-or-down-september-8",
    )
    assert "Down WON" in message
    assert "Payout: <b>$1.45</b>" in message
    assert "<b>+$0.35</b>" in message
    assert (
        "https://polymarket.com/event/bitcoin-up-or-down-september-8/"
        "btc-updown-5m-1788857400" in message
    )
