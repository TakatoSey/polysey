"""A trade notification cannot be reconstructed from anywhere else, so it must
survive the first refusal — and a burst must not provoke that refusal."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from app.bot import TelegramApp


def panel(queue=None):
    app = object.__new__(TelegramApp)
    app.settings = SimpleNamespace(telegram_allowed_user_id=7)
    app.engine = SimpleNamespace(notifications=queue or asyncio.Queue())
    app.bot = SimpleNamespace(send_message=AsyncMock())
    return app


def instant_sleep(monkeypatch):
    """Record what the panel waits without the test itself losing asyncio.

    Patching asyncio.sleep through the module object would replace it for
    every caller in the process, this test included.
    """
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(
        "app.bot.asyncio",
        SimpleNamespace(sleep=sleep, Queue=asyncio.Queue, Semaphore=asyncio.Semaphore),
    )
    return slept


def retry_after(seconds):
    return TelegramRetryAfter(
        method=SimpleNamespace(), message="Too Many Requests", retry_after=seconds
    )


async def test_rate_limited_notification_waits_the_stated_time_and_arrives(monkeypatch):
    app = panel()
    slept = instant_sleep(monkeypatch)
    app.bot.send_message = AsyncMock(side_effect=[retry_after(3), None])

    assert await app._send_notification("copied a buy") is True

    assert app.bot.send_message.await_count == 2
    # Telegram states the wait; retrying sooner is refused again.
    assert slept == [3.5]


async def test_a_transient_network_error_is_retried_then_reported(monkeypatch):
    app = panel()
    slept = instant_sleep(monkeypatch)
    app.bot.send_message = AsyncMock(side_effect=TelegramNetworkError(method=None, message="down"))

    assert await app._send_notification("copied a buy") is False

    assert app.bot.send_message.await_count == app.SEND_ATTEMPTS
    assert slept == [1.0, 2.0, 4.0]


async def test_a_rejected_message_is_not_retried_forever(monkeypatch):
    app = panel()
    instant_sleep(monkeypatch)
    app.bot.send_message = AsyncMock(side_effect=ValueError("bad html"))

    assert await app._send_notification("copied a buy") is False

    # A malformed message will not become valid on the next attempt.
    assert app.bot.send_message.await_count == 1


async def test_a_burst_is_sent_as_one_message_which_is_what_gets_rate_limited():
    queue = asyncio.Queue()
    app = panel(queue)
    for index in range(5):
        queue.put_nowait(f"copy {index}")

    message = app._drain_notifications(await queue.get())

    assert queue.empty()
    for index in range(5):
        assert f"copy {index}" in message
    assert message.count("➖➖➖") == 4


async def test_what_does_not_fit_in_one_message_stays_queued():
    queue = asyncio.Queue()
    app = panel(queue)
    long_message = "x" * 2000
    for _ in range(3):
        queue.put_nowait(long_message)

    message = app._drain_notifications(await queue.get())

    # Two fit under the character cap; the rest is handed back whole.
    assert len(message) <= app.MESSAGE_LIMIT
    assert message.count(long_message) == 1
    assert queue.qsize() == 2


async def test_notify_loop_keeps_serving_after_a_failed_send(monkeypatch):
    queue = asyncio.Queue()
    app = panel(queue)
    instant_sleep(monkeypatch)
    delivered, arrived = [], asyncio.Event()

    async def send(chat_id, text, **kwargs):
        delivered.append(text)
        arrived.set()
        if len(delivered) == 1:
            raise ValueError("bad html")

    app.bot.send_message = AsyncMock(side_effect=send)
    queue.put_nowait("first")
    worker = asyncio.create_task(app.notify_loop())
    try:
        await asyncio.wait_for(arrived.wait(), timeout=1)
        arrived.clear()
        queue.put_nowait("second")
        await asyncio.wait_for(arrived.wait(), timeout=1)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    # One rejected message must not take the loop down with it.
    assert delivered == ["first", "second"]
