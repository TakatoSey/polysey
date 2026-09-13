"""SIGTERM is how `docker compose stop/restart` asks the bot to finish."""

import asyncio
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.__main__ import install_stop_handlers
from app.bot import TelegramApp


@pytest.mark.parametrize("delivered", [signal.SIGTERM, signal.SIGINT])
async def test_a_stop_signal_asks_for_shutdown_instead_of_killing_the_process(delivered):
    stopping = asyncio.Event()
    install_stop_handlers(stopping)

    os.kill(os.getpid(), delivered)
    # Without a handler the default action ends the process here, and nothing
    # in run_bot's finally — engine stop, client close, lock release — runs.
    await asyncio.wait_for(stopping.wait(), timeout=1)

    assert stopping.is_set()


async def test_repeated_signals_are_one_shutdown():
    stopping = asyncio.Event()
    install_stop_handlers(stopping)

    for _ in range(3):
        os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(stopping.wait(), timeout=1)

    assert stopping.is_set()


async def test_closing_the_panel_releases_the_telegram_session():
    app = object.__new__(TelegramApp)
    app.bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))

    await app.close()

    app.bot.session.close.assert_awaited_once()


async def test_a_failing_telegram_session_does_not_block_the_rest_of_shutdown():
    app = object.__new__(TelegramApp)
    app.bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock(side_effect=OSError("gone"))))

    await app.close()  # the client and the advisory lock still have to be released
