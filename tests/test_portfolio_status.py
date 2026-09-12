import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import TelegramApp
from app.db import Base
from app.models import Account, PaperOrder, Position


def panel(client):
    app = object.__new__(TelegramApp)
    app.engine = SimpleNamespace(client=client)
    return app


POSITION = SimpleNamespace(condition_id="market", token_id="down", outcome="Down")


@pytest.mark.asyncio
@pytest.mark.parametrize("payout", [Decimal(0), Decimal("0.5"), Decimal(1)])
async def test_resolved_position_uses_payout_without_orderbook(payout):
    client = AsyncMock()
    client.get_resolution.return_value = payout
    quote, status, note = await panel(client)._position_quote(POSITION)
    assert quote == payout
    assert "Выплата" in note
    if payout == Decimal("0.5"):
        assert "Split" in status
    client.get_book.assert_not_called()


@pytest.mark.asyncio
async def test_resolution_error_does_not_hide_book_quote():
    client = AsyncMock()
    client.get_resolution.side_effect = RuntimeError("timeout")
    client.get_book.return_value = SimpleNamespace(bids=[(Decimal("0.4"), Decimal(20))])
    quote, status, _ = await panel(client)._position_quote(POSITION)
    assert quote == Decimal("0.4")
    assert "Unknown" in status


@pytest.mark.asyncio
async def test_api_failure_and_empty_bid_are_different_states():
    client = AsyncMock()
    client.get_resolution.return_value = None
    client.get_book.side_effect = RuntimeError("404")
    quote, _, note = await panel(client)._position_quote(POSITION)
    assert quote is None
    assert "Стакан недоступен" in note
    client.get_book.side_effect = None
    client.get_book.return_value = SimpleNamespace(bids=[])
    quote, _, note = await panel(client)._position_quote(POSITION)
    assert quote is None
    assert "last trade" in note


@pytest.mark.asyncio
async def test_empty_bid_uses_last_trade_mark_without_claiming_executable_sell():
    client = AsyncMock()
    client.get_resolution.return_value = None
    client.get_book.return_value = SimpleNamespace(bids=[])
    client.get_last_trade_price.return_value = Decimal("0.01")
    quote, _, note = await panel(client)._position_quote(POSITION)
    assert quote == Decimal("0.01")
    assert "Mark" in note
    assert "последней сделке" in note


@pytest.mark.asyncio
async def test_closed_market_without_a_published_result_says_so():
    client = AsyncMock()
    client.get_resolution.return_value = None
    client.get_book.return_value = SimpleNamespace(bids=[])
    client.get_last_trade_price.return_value = None
    client.get_market.return_value = {"closed": True}

    quote, status, note = await panel(client)._position_quote(POSITION)

    assert quote is None
    assert "Settling" in status
    assert "итог ещё не опубликован" in note


@pytest.mark.asyncio
async def test_an_unpriced_position_does_not_blank_the_total_for_the_others():
    app = panel(AsyncMock())

    def position(identifier, cost):
        return SimpleNamespace(
            id=identifier,
            condition_id="market",
            token_id=f"t{identifier}",
            title=f"Market {identifier}",
            outcome="Yes",
            shares=Decimal(10),
            average_price=Decimal("0.5"),
            cost_basis=cost,
        )

    rows = [position(1, Decimal(5)), position(2, Decimal(4))]
    app._portfolio_data_v2 = AsyncMock(return_value=(rows, SimpleNamespace()))
    app._position_quote = AsyncMock(
        side_effect=[
            (Decimal("0.7"), "🟢 Open", "Оценка по лучшему bid"),
            (None, "⏳ Settling", "Рынок закрыт, итог ещё не опубликован"),
        ]
    )

    text = await app._portfolio_text_v2()

    # Priced leg is 10 shares at 70c against a $5 cost.
    assert "Total PnL: +$2.00 (+40.0%)" in text
    assert "без оценки: 1 из 2" in text


@pytest.mark.asyncio
async def test_portfolio_screen_shows_price_value_pnl_percent_and_win_payout():
    app = panel(AsyncMock())
    row = SimpleNamespace(
        id=1,
        condition_id="market",
        token_id="down",
        title="Bitcoin Up or Down?",
        outcome="Down",
        shares=Decimal("14.252714"),
        average_price=Decimal("0.463"),
        cost_basis=Decimal("6.60"),
    )
    account = SimpleNamespace(paper_balance=Decimal("93.40"), realized_pnl=Decimal(0))
    app._portfolio_data_v2 = AsyncMock(return_value=([row], account))
    app._position_quote = AsyncMock(
        return_value=(
            Decimal("0.62"),
            "⏳ Результат ещё не подтверждён",
            "Mark по последней сделке",
        )
    )

    text = await app._portfolio_text_v2()

    assert "Avg/Now: 46.30¢ → 62.00¢" in text
    assert "Cost/Value: $6.60 → $8.84" in text
    assert "PnL: +$2.24 (+33.9%)" in text
    assert "To Win: $14.25" in text
    assert "Total PnL: +$2.24 (+33.9%)" in text


def buttons(keyboard):
    return [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("position:")
    ]


@pytest.mark.asyncio
async def test_portfolio_is_ordered_by_last_buy_and_its_buttons_follow_the_list(
    tmp_path, monkeypatch
):
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'portfolio-order.db'}")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr("app.bot.SessionLocal", sessions)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    # Bought in the order a, b, c, d, e, f; f is the most recent.
    order_of_purchase = ["a", "b", "c", "d", "e", "f"]
    async with sessions() as session:
        session.add(Account(id=1, paper_balance=Decimal(100), starting_balance=Decimal(100)))
        for index, token in enumerate(order_of_purchase):
            session.add(
                Position(
                    id=index + 1,
                    token_id=token,
                    condition_id=f"market-{token}",
                    title=f"Market {token}",
                    outcome="Yes",
                    shares=Decimal(10),
                    average_price=Decimal("0.50"),
                    cost_basis=Decimal(5),
                    # Opening order is deliberately the reverse of buying order:
                    # the list must follow the buys, not the row age.
                    opened_at=opened + timedelta(minutes=len(order_of_purchase) - index),
                )
            )
            session.add(
                PaperOrder(
                    token_id=token,
                    side="BUY",
                    requested_shares=Decimal(10),
                    filled_shares=Decimal(10),
                    average_fill_price=Decimal("0.50"),
                    status="filled",
                    created_at=opened + timedelta(minutes=index),
                )
            )
        # Neither a later sell nor a rejected buy is a purchase.
        session.add(
            PaperOrder(
                token_id="a",
                side="SELL",
                requested_shares=Decimal(1),
                filled_shares=Decimal(1),
                average_fill_price=Decimal("0.60"),
                status="filled",
                created_at=opened + timedelta(hours=5),
            )
        )
        session.add(
            PaperOrder(
                token_id="a",
                side="BUY",
                requested_shares=Decimal(1),
                filled_shares=Decimal(0),
                average_fill_price=Decimal(0),
                status="rejected",
                created_at=opened + timedelta(hours=5),
            )
        )
        await session.commit()
    app = panel(AsyncMock())
    app.settings = SimpleNamespace(paper_initial_balance=Decimal(100))
    app._position_quote = AsyncMock(return_value=(Decimal("0.60"), "🟢 Open", "bid"))
    identifier = {token: index + 1 for index, token in enumerate(order_of_purchase)}
    newest_first = list(reversed(order_of_purchase))
    try:
        text, keyboard = await app._portfolio_screen(0)
        listed = re.findall(r"\d+\. <b>Market (\w)</b>", text)
        assert listed == newest_first[:5]
        assert buttons(keyboard) == [f"position:{identifier[token]}:0" for token in listed]

        text, keyboard = await app._portfolio_screen(1)
        listed = re.findall(r"\d+\. <b>Market (\w)</b>", text)
        assert listed == newest_first[5:]
        assert buttons(keyboard) == [f"position:{identifier[token]}:1" for token in listed]
    finally:
        await db.dispose()
