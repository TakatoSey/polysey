import asyncio
import contextlib
import signal
from decimal import Decimal

import structlog

from .bot import TelegramApp
from .config import get_settings
from .db import SessionLocal, init_db, single_process
from .engine import CopyEngine
from .executor import LiveExecutor, PaperExecutor
from .live import LiveTrader, LiveTradingUnavailable
from .live_state import LiveState
from .logging import configure_logging
from .polymarket import PolymarketClient
from .repository import (
    add_leader,
    claim_database,
    get_leader,
    get_or_create_account,
    initialize_execution,
)
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


async def start_live(settings, client):
    """Bring up real trading, or refuse to start at all.

    A misconfigured live bot must not fall back to paper: it would look like it
    is trading while it is not, which is the one failure nobody notices.
    """
    trader = LiveTrader(settings)
    try:
        account = await trader.start()
    except LiveTradingUnavailable as exc:
        log.error("live_trading_unavailable", error=str(exc))
        raise
    state = LiveState(trader, client.http, settings)
    snapshot = await state.start()
    if not snapshot.read_ok:
        await state.stop()
        raise LiveTradingUnavailable(f"exchange state unavailable: {snapshot.error}")
    log.warning(
        "trading_mode",
        mode="live",
        note="REAL money orders are enabled",
        funder=account.funder,
        usdc=str(snapshot.cash),
        positions=len(snapshot.positions),
        max_order_usdc=str(settings.live_max_order_usdc),
        max_daily_loss_usdc=str(settings.live_max_daily_loss_usdc),
        dry_run=settings.live_dry_run,
    )
    return trader, state, LiveExecutor(trader, settings, state)


async def run_bot(settings) -> None:
    await init_db()
    async with SessionLocal() as session:
        # Before anything reads or writes money: this database must belong to
        # this mode and this wallet.
        claim = await claim_database(session, settings)
        log.info(
            "database_claim",
            trading_mode=claim.trading_mode,
            funder=claim.funder or None,
            claimed_at=str(claim.claimed_at),
        )
        # Initialize defaults before either the Telegram or trading loops start.
        # Existing account limits are intentionally preserved. In live mode the
        # ledger starts empty rather than at PAPER_INITIAL_BALANCE: the real
        # figure arrives from the exchange, and a placeholder would otherwise be
        # a number an entry could be sized against.
        await get_or_create_account(
            session,
            Decimal(0) if settings.live else settings.paper_initial_balance,
            settings=settings,
        )
        await initialize_execution(session, settings)
        if settings.default_leader_address and not settings.live:
            # Seed the configured leader once; do not silently re-enable one
            # the user intentionally disabled from the Telegram panel.
            if not await get_leader(session, settings.default_leader_address):
                await add_leader(session, settings.default_leader_address)
        elif settings.default_leader_address:
            # Never start copying someone with real money because an example
            # address was left in .env. Adding a leader stays a deliberate act.
            log.warning(
                "default_leader_not_seeded_in_live",
                address=settings.default_leader_address,
                hint="add the leaders you want from the Telegram panel",
            )
        await session.commit()
    client = PolymarketClient(settings)
    await client.start()
    executor, live_state, trader = PaperExecutor(), None, None
    if settings.live:
        trader, live_state, executor = await start_live(settings, client)
    else:
        log.info("trading_mode", mode="paper", note="no real orders are sent")
    engine = CopyEngine(settings, client, executor=executor, live_state=live_state)
    rtds = (
        RTDSTradeStream(engine.on_rtds_trade, engine.tracked_addresses)
        if settings.rtds_enabled
        else None
    )
    if rtds:
        await rtds.start()
    telegram = TelegramApp(settings, engine)
    # Identity and exclusivity of the Telegram token before any worker starts.
    await telegram.verify_identity()
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
        if live_state is not None:
            await live_state.stop()
        if trader is not None:
            await trader.close()
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
