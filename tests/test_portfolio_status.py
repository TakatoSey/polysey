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
    assert "ожидает зачисления" in note
    if payout == Decimal("0.5"):
        assert "Разделённая" in status
    client.get_book.assert_not_called()


@pytest.mark.asyncio
async def test_resolution_error_does_not_hide_book_quote():
    client = AsyncMock()
    client.get_resolution.side_effect = RuntimeError("timeout")
    client.get_book.return_value = SimpleNamespace(bids=[(Decimal("0.4"), Decimal(20))])
    quote, status, _ = await panel(client)._position_quote(POSITION)
    assert quote == Decimal("0.4")
    assert "Не удалось проверить" in status


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
    assert "Нет заявок" in note
