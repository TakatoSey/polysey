import asyncio
import contextlib
import signal

import structlog

from .bot import TelegramApp
from .config import get_settings
from .db import SessionLocal, init_db, single_process
from .engine import CopyEngine
from .logging import configure_logging
from .polymarket import PolymarketClient
from .repository import add_leader, get_leader, get_or_create_account, initialize_execution
from .rtds import RTDSTradeStream

log = structlog.get_logger(__name__)

# How long a stopping engine is given to finish the ledger work already in
# flight. Docker's own grace period before SIGKILL is ten seconds by default.
SHUTDOWN_GRACE_SECONDS = 5


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    async with single_process():
        await run_bot(settings)


def install_stop_handlers(stopping: asyncio.Event) -> None:
    """Turn a container stop into an ordinary shutdown, not a kill.

    `docker compose stop/restart` sends SIGTERM. Without a handler the
    interpreter dies where it stands: no `finally` runs, the HTTP client and
    database sessions are never closed, and the advisory lock is left to the
    server to reap.
    """
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(received, lambda name=name: _request_stop(stopping, name))


def _request_stop(stopping: asyncio.Event, signal_name: str) -> None:
    if not stopping.is_set():
        log.info("shutdown_requested", signal=signal_name)
    stopping.set()


async def run_bot(settings) -> None:
    await init_db()
    async with SessionLocal() as session:
        # Initialize defaults before either the Telegram or trading loops start.
        # Existing account limits are intentionally preserved.
        await get_or_create_account(session, settings.paper_initial_balance, settings=settings)
        await initialize_execution(session, settings)
        if settings.default_leader_address:
            # Seed the configured leader once; do not silently re-enable one
            # the user intentionally disabled from the Telegram panel.
            if not await get_leader(session, settings.default_leader_address):
                await add_leader(session, settings.default_leader_address)
        await session.commit()
    client = PolymarketClient(settings)
    await client.start()
    engine = CopyEngine(settings, client)
    rtds = (
        RTDSTradeStream(engine.on_rtds_trade, engine.tracked_addresses)
        if settings.rtds_enabled
        else None
    )
    if rtds:
        await rtds.start()
    telegram = TelegramApp(settings, engine)
    stopping = asyncio.Event()
    install_stop_handlers(stopping)
    workers = {
        asyncio.create_task(engine.run(), name="engine"),
        asyncio.create_task(telegram.run(), name="telegram"),
        asyncio.create_task(telegram.notify_loop(), name="notifications"),
    }
    waiter = asyncio.create_task(stopping.wait(), name="shutdown")
    try:
        done, _ = await asyncio.wait({*workers, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Ask the engine to finish the pass it is in before anything is torn
        # down, then stop waiting: a stuck worker must not block the exit.
        await engine.stop()
        engine_task = next(task for task in workers if task.get_name() == "engine")
        await asyncio.wait({engine_task}, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in (*workers, waiter):
            task.cancel()
        await asyncio.gather(*workers, waiter, return_exceptions=True)
        if rtds:
            await rtds.stop()
        await client.close()
        await telegram.close()
        log.info("shutdown_complete")
    for task in done & workers:
        # A worker that ended on its own ended for a reason; surface it so the
        # container restarts instead of idling with nothing running.
        if not task.cancelled() and task.exception():
            raise task.exception()


if __name__ == "__main__":
    asyncio.run(main())
