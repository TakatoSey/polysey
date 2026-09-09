from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import TelegramApp


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
