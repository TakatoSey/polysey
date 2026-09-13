"""What the exchange says we have, and whether we may act on it.

Sizing must not wait for a balance request on the hot path, and it must not
invent one either. So the exchange is read in the background, our own fills are
projected onto that last read, and the next read replaces the projection.

Every number here is either something Polymarket reported or our own spend
since it reported. When the two disagree by more than the configured tolerance,
new entries stop instead of being sized from numbers nobody can vouch for.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import structlog

log = structlog.get_logger(__name__)

ZERO = Decimal(0)


@dataclass
class Snapshot:
    """One reading of the exchange, with our own activity since then."""

    cash: Decimal = ZERO
    positions: dict[str, Decimal] = field(default_factory=dict)
    read_at: float = 0.0
    read_ok: bool = False
    spent_since: Decimal = ZERO
    shares_since: dict[str, Decimal] = field(default_factory=dict)
    error: str | None = None

    @property
    def cash_now(self) -> Decimal:
        """Exchange cash minus what we have committed since it was read."""
        return max(ZERO, self.cash - self.spent_since)

    def shares_now(self, token_id: str) -> Decimal:
        held = self.positions.get(token_id, ZERO) + self.shares_since.get(token_id, ZERO)
        return max(ZERO, held)

    @property
    def age(self) -> float:
        return float("inf") if not self.read_at else time.monotonic() - self.read_at


def utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


class LiveState:
    """Background reader of exchange cash and positions."""

    def __init__(self, trader, http, settings):
        self.trader = trader
        self.http = http
        self.settings = settings
        self.snapshot = Snapshot()
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._refresh_now = asyncio.Event()

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> Snapshot:
        await self.refresh()
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="live-state")
        return self.snapshot

    async def stop(self) -> None:
        self._stop.set()
        self._refresh_now.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._refresh_now.wait(), timeout=self.settings.live_state_refresh_seconds
                )
            except TimeoutError:
                pass
            self._refresh_now.clear()
            if self._stop.is_set():
                return
            try:
                await self.refresh()
            except Exception:
                log.exception("live_state_refresh_failed")

    def invalidate(self) -> None:
        """Ask for a fresh read as soon as possible, after our own fill."""
        self._refresh_now.set()

    # ------------------------------------------------------------------ reads

    async def refresh(self) -> Snapshot:
        """Read cash and positions from the exchange and drop the projection."""
        async with self._lock:
            # Spend recorded while this read is in flight must survive it: the
            # exchange may not have settled our newest fill yet, and dropping
            # the projection would hand that money out a second time.
            spent_before = self.snapshot.spent_since
            shares_before = dict(self.snapshot.shares_since)
            try:
                cash = await self.trader.cash()
                rows = await self.trader.positions(self.http, self.settings.data_api)
            except Exception as exc:
                self.snapshot.read_ok = False
                self.snapshot.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("live_state_unavailable", error=self.snapshot.error)
                return self.snapshot
            in_flight = self.snapshot.spent_since - spent_before
            since = {
                token: amount - shares_before.get(token, ZERO)
                for token, amount in self.snapshot.shares_since.items()
                if amount != shares_before.get(token, ZERO)
            }
            self.snapshot = Snapshot(
                cash=cash,
                positions={row.token_id: row.shares for row in rows},
                read_at=time.monotonic(),
                read_ok=True,
                spent_since=in_flight,
                shares_since=since,
            )
            log.info(
                "live_state",
                usdc=str(cash),
                positions=len(self.snapshot.positions),
            )
            return self.snapshot

    # ------------------------------------------------- our own fills, projected

    def note_buy(self, token_id: str, usdc: Decimal, shares: Decimal) -> None:
        self.snapshot.spent_since += usdc
        self.snapshot.shares_since[token_id] = (
            self.snapshot.shares_since.get(token_id, ZERO) + shares
        )
        self.invalidate()

    def note_sell(self, token_id: str, usdc: Decimal, shares: Decimal) -> None:
        self.snapshot.spent_since -= usdc
        self.snapshot.shares_since[token_id] = (
            self.snapshot.shares_since.get(token_id, ZERO) - shares
        )
        self.invalidate()

    # -------------------------------------------------------------- interlocks

    def cash_drift(self, ledger_cash: Decimal) -> Decimal:
        """How far our ledger's cash is from the exchange's last reading."""
        if not self.snapshot.read_ok:
            return ZERO
        return (ledger_cash - self.snapshot.cash_now).copy_abs()

    def entry_block(self, ledger_cash: Decimal) -> str | None:
        """Why a new entry must not be sized right now, if it must not.

        Exits are deliberately not blocked by any of this: refusing to reduce
        an open position is the one thing worse than acting on a stale number.
        """
        if not self.snapshot.read_ok:
            return "live_state_unavailable"
        if self.snapshot.age > max(30.0, self.settings.live_state_refresh_seconds * 6):
            return "live_state_stale"
        if self.cash_drift(ledger_cash) > self.settings.live_drift_tolerance_usdc:
            return "live_ledger_drift"
        return None
