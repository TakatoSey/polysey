from __future__ import annotations

import asyncio
import hashlib
import html
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .accounting import Holding, inventory
from .config import Settings
from .db import SessionLocal
from .models import (
    CopyTrade,
    ExitIntent,
    Leader,
    LeaderPosition,
    LeaderSizingProfile,
    PaperOrder,
    Position,
    RiskRule,
    SizingAudit,
    SizingEntry,
    SourceReceipt,
)
from .paper import execute_buy_fak_by_budget, execute_fak
from .polymarket import Book, LeaderActivity, PolymarketClient, copy_event_key
from .priority import PriorityLock
from .repository import (
    apply_fill,
    get_execution_policy,
    get_or_create_account,
    get_position,
    get_risk,
)
from .sizing import entry_bucket, entry_budget, sample_entries

log = structlog.get_logger(__name__)


@dataclass
class PreparedCopy:
    market: dict | None = None
    fee_rate: Decimal = Decimal(0)
    exchange_delay: float = 0.0
    error: Exception | None = None
    ready_at: float = 0.0
    book: Book | None = None
    book_at: float = 0.0
    book_error: Exception | None = None


class CopyEngine:
    def __init__(self, settings: Settings, client: PolymarketClient):
        self.settings = settings
        self.client = client
        self.stop_event = asyncio.Event()
        self.notifications: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        # Network preparation is concurrent; all portfolio mutations remain serialized.
        self._ledger_lock = PriorityLock()
        self._prepare_slots = asyncio.Semaphore(settings.copy_prepare_concurrency)
        self._poll_slots = asyncio.Semaphore(8)
        self._maintenance_slots = asyncio.Semaphore(4)
        self._exit_slots = asyncio.Semaphore(2)
        self._exit_workers = {}
        self._buy_batches = {}
        self._sell_watermarks = {}
        self._buy_preparations = {}
        self._pending: dict[str, asyncio.Task] = {}
        self._leader_polls: dict[int, asyncio.Task] = {}
        self._token_tails: dict[tuple[int, str], asyncio.Task] = {}
        self._leader_floors: dict[int, int] = {}
        self._leader_sizing_profiles: dict[int, LeaderSizingProfile] = {}
        self._profile_refresh_attempt: dict[int, float] = {}
        self.tracked_addresses: set[str] = set()

    async def notify(self, message: str) -> None:
        try:
            self.notifications.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("notification_queue_full")

    @staticmethod
    def build_buy_notification(leader: Leader, event: LeaderActivity, fill) -> str:
        trader_name = (
            event.trader_name or leader.label or (f"{leader.address[:8]}…{leader.address[-6:]}")
        )
        profile_url = f"https://polymarket.com/profile/{leader.address}"
        total_debit = fill.notional + fill.fee
        return (
            "✅ <b>BUY скопирован</b>\n\n"
            f"<b>{html.escape(event.title)}</b>\n"
            f"Исход: <b>{html.escape(event.outcome)}</b>\n\n"
            f'Трейдер: <a href="{profile_url}">{html.escape(trader_name)}</a>\n'
            f"Сумма: <b>${fill.notional:.4f}</b>\n"
            f"Получено: <b>{fill.shares:.4f} shares</b>\n"
            f"Цена: ${fill.average_price:.4f} · комиссия ${fill.fee:.5f}\n"
            f"Списано всего: ${total_debit:.4f}"
        )

    @staticmethod
    def calculate_own_buy_capacity(account, settings: Settings, fee_rate: Decimal) -> Decimal:
        cash_budget = account.paper_balance / (Decimal(1) + fee_rate)
        return min(
            cash_budget,
            account.max_trade_size,
            account.trade_size,
            cash_budget * settings.copy_balance_pct,
        )

    @classmethod
    def calculate_buy_budget(
        cls, account, settings: Settings, leader_notional: Decimal, fee_rate: Decimal
    ):
        """Size from our cash first, using leader notional as a soft proportional ceiling."""
        own_budget = cls.calculate_own_buy_capacity(account, settings, fee_rate)
        if leader_notional <= 0 or own_budget < settings.min_copy_notional:
            return max(Decimal(0), min(own_budget, leader_notional))
        proportional = leader_notional * settings.leader_order_scale
        # We intentionally copy all valid leader buys. A small leader order is
        # rounded up to our executable minimum, never above our own budget.
        return min(own_budget, max(settings.min_copy_notional, proportional))

    @staticmethod
    def ensure_book_minimum_budget(
        budget: Decimal,
        own_capacity: Decimal,
        book,
        reference_price: Decimal,
        slippage_price: Decimal | None = None,
        slippage_bps: int | None = None,
    ) -> Decimal:
        """Raise a valid small copy to the exchange's share minimum when affordable."""
        if not book.asks or reference_price <= 0:
            return budget
        best_ask = book.asks[0][0]
        distance = (
            slippage_price
            if slippage_price is not None
            else reference_price * Decimal(slippage_bps or 0) / Decimal(10000)
        )
        max_price = reference_price + distance
        required = book.min_order_size * best_ask
        if best_ask <= max_price and budget < required <= own_capacity:
            return required
        return budget

    @staticmethod
    def record_rejection(
        session,
        copy_trade: CopyTrade,
        event: LeaderActivity,
        reason: str,
        requested_shares: Decimal = Decimal(0),
    ) -> None:
        """Persist every skipped event so an active bot can never fail silently."""
        session.add(
            PaperOrder(
                copy_trade_id=copy_trade.id,
                token_id=event.token_id,
                side=event.side,
                requested_shares=requested_shares,
                filled_shares=Decimal(0),
                average_fill_price=Decimal(0),
                fee=Decimal(0),
                status="rejected",
                reason=reason[:240],
            )
        )

    async def run(self) -> None:
        log.info(
            "copy_latency_config",
            poll_seconds=self.settings.poll_interval_seconds,
            artificial_delay_seconds=self.settings.copy_latency_seconds,
            prepare_concurrency=self.settings.copy_prepare_concurrency,
            max_age_rtds_seconds=self.settings.max_signal_age_rtds_seconds,
            max_age_rest_seconds=self.settings.max_signal_age_rest_seconds,
            exit_retry_enabled=self.settings.exit_retry_enabled,
            exit_retry_seconds=self.settings.exit_retry_seconds,
        )
        if self.settings.copy_latency_seconds:
            log.warning("artificial_copy_delay_enabled", hint="Set COPY_LATENCY_SECONDS=0")
        loops = [
            asyncio.create_task(
                self._repeat(self.poll_background_once, self.settings.poll_interval_seconds)
            ),
            asyncio.create_task(
                self._repeat(self.settle_once, self.settings.maintenance_interval_seconds)
            ),
            asyncio.create_task(
                self._repeat(self.monitor_risk_once, self.settings.maintenance_interval_seconds)
            ),
            asyncio.create_task(
                self._repeat(self.retry_exits_once, self.settings.exit_retry_seconds)
            ),
        ]
        try:
            await asyncio.gather(*loops)
        finally:
            tasks = (
                loops
                + list(self._leader_polls.values())
                + list(set(self._pending.values()))
                + list(self._exit_workers.values())
            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _repeat(self, stage, interval: float) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                await stage()
            except Exception:
                log.exception("engine_stage_failed", stage=stage.__name__)
            # Start-to-start cadence, not HTTP duration plus another full interval.
            remaining = max(0.05, interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self.stop_event.set()

    async def on_rtds_trade(self, event: LeaderActivity) -> None:
        if not event.trader_address:
            return "untracked"
        event.event_key = copy_event_key(event.event_key, event.trader_address)
        async with SessionLocal() as session:
            leader = await session.scalar(
                select(Leader).where(Leader.address == event.trader_address)
            )
            if not leader or not leader.active or not leader.initialized:
                return "untracked"
            if await session.scalar(
                select(CopyTrade.id).where(CopyTrade.event_key == event.event_key)
            ):
                return "duplicate"
            if await session.get(SourceReceipt, event.event_key):
                return "duplicate"
        self._schedule_copy(leader.id, event)
        return "scheduled"

    async def poll_background_once(self) -> None:
        await self.poll_once(wait=False)

    async def poll_once(self, *, wait: bool = True) -> None:
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
            if account.paused:
                return
            leaders = list(
                (await session.scalars(select(Leader).where(Leader.active.is_(True)))).all()
            )
            self.tracked_addresses.clear()
            self.tracked_addresses.update(leader.address.lower() for leader in leaders)

        tasks = []
        for leader in leaders:
            task = self._leader_polls.get(leader.id)
            if task is None:
                task = asyncio.create_task(self._poll_leader(leader))
                self._leader_polls[leader.id] = task

                def done(completed, leader_id=leader.id):
                    if self._leader_polls.get(leader_id) is completed:
                        self._leader_polls.pop(leader_id, None)
                    if not completed.cancelled() and completed.exception():
                        log.error(
                            "leader_poll_failed",
                            leader_id=leader_id,
                            error=str(completed.exception()),
                        )

                task.add_done_callback(done)
            tasks.append(task)
        if wait:
            await asyncio.gather(*tasks)

    async def _poll_leader(self, leader: Leader) -> None:
        async with self._poll_slots:
            try:
                profile_method = getattr(self.client, "get_public_profile", None)
                profile_task = (
                    asyncio.create_task(profile_method(leader.address)) if profile_method else None
                )
                activities = await self.client.get_activity(leader.address)
                profile = None
                if profile_task:
                    try:
                        profile = await profile_task
                    except Exception as exc:
                        log.info(
                            "leader_profile_unavailable",
                            leader=leader.address,
                            error=type(exc).__name__,
                        )
            except Exception:
                log.exception("leader_activity_failed", leader=leader.address)
                if profile_task and not profile_task.done():
                    profile_task.cancel()
                return
            async with SessionLocal() as session:
                db_leader = await session.scalar(select(Leader).where(Leader.id == leader.id))
                if not db_leader or not db_leader.active:
                    return
                if profile:
                    db_leader.label = profile
                await self._refresh_sizing_profile(session, db_leader, activities)
                if not activities:
                    return
                if not db_leader.initialized:
                    db_leader.last_timestamp = max(event.timestamp for event in activities) + 1
                    db_leader.initialized = True
                    await session.commit()
                    log.info(
                        "leader_initialized",
                        leader=leader.address,
                        last_timestamp=db_leader.last_timestamp,
                    )
                    self._leader_floors[leader.id] = db_leader.last_timestamp
                    return
                # Keep the session's lower bound stable: late-indexed events must
                # not disappear just because another token finished sooner.
                floor = self._leader_floors.setdefault(leader.id, db_leader.last_timestamp)
                new_events = [event for event in activities if event.timestamp >= floor]
                if not new_events:
                    return
                existing = set(
                    await session.scalars(
                        select(CopyTrade.event_key).where(
                            CopyTrade.event_key.in_([event.event_key for event in new_events])
                        )
                    )
                )
                existing.update(
                    await session.scalars(
                        select(SourceReceipt.event_key).where(
                            SourceReceipt.event_key.in_([event.event_key for event in new_events])
                        )
                    )
                )
                outstanding = []
                for event in new_events:
                    if event.event_key in existing:
                        continue
                    outstanding.append(event.timestamp)
                    self._schedule_copy(leader.id, event)
                # Never checkpoint past an uncommitted/overflowed event.
                checkpoint = (
                    min(outstanding) if outstanding else max(e.timestamp for e in new_events)
                )
                db_leader.last_timestamp = checkpoint
                await session.commit()

    async def _refresh_sizing_profile(self, session, leader, activities) -> None:
        if not self.settings.smart_sizing_enabled:
            return
        last = self._profile_refresh_attempt.get(leader.id)
        interval = (
            self.settings.smart_sizing_stats_refresh_seconds
            if leader.id in self._leader_sizing_profiles
            else 60
        )
        if last is not None and time.monotonic() - last < interval:
            return
        now = int(time.time())
        # Exclude events about to be copied, including their entire source bucket.
        cutoff = min(now, leader.last_timestamp) if leader.initialized else now
        sample = sample_entries(
            activities,
            before=entry_bucket(cutoff, self.settings.smart_sizing_burst_seconds),
            seconds=self.settings.smart_sizing_burst_seconds,
            min_samples=self.settings.smart_sizing_min_samples,
        )
        profile = await session.get(LeaderSizingProfile, leader.id)
        if sample:
            if profile is None:
                profile = LeaderSizingProfile(leader_id=leader.id)
                session.add(profile)
            profile.reference_notional = sample.reference_notional
            profile.sample_count = sample.sample_count
            profile.sample_start, profile.sample_end = sample.sample_start, sample.sample_end
            profile.bucket_seconds = self.settings.smart_sizing_burst_seconds
            profile.refreshed_at = datetime.now(UTC)
            await session.commit()
        if (
            profile
            and profile.sample_end >= now - 7 * 86400
            and profile.bucket_seconds == self.settings.smart_sizing_burst_seconds
        ):
            self._leader_sizing_profiles[leader.id] = profile
        else:
            self._leader_sizing_profiles.pop(leader.id, None)
        self._profile_refresh_attempt[leader.id] = time.monotonic()

    def _schedule_copy(self, leader_id: int, event: LeaderActivity) -> None:
        if event.event_key in self._pending:
            return
        limit = self.settings.copy_queue_limit + (64 if event.side == "SELL" else 0)
        if len(self._pending) >= limit:
            log.warning("copy_queue_full", pending=len(self._pending), event_key=event.event_key)
            return  # REST retries it; no checkpoint or fake rejection.
        if not event.received_at:
            event.received_at, event.received_monotonic = time.time(), time.monotonic()
        owner_key = (leader_id, event.token_id)
        if event.side == "SELL" and self._valid_timestamp(event):
            self._sell_watermarks[owner_key] = max(
                event.timestamp, self._sell_watermarks.get(owner_key, 0)
            )
            for stamp, cancel in self._buy_preparations.get(owner_key, []):
                if stamp <= event.timestamp:
                    cancel.set()
        batch_key = (
            *owner_key,
            entry_bucket(event.timestamp, self.settings.smart_sizing_burst_seconds),
            self._sell_watermarks.get(owner_key, 0),
        )
        if event.side == "BUY" and self.settings.smart_sizing_enabled:
            batch = self._buy_batches.get(batch_key)
            if batch:
                batch[0].append(event)
                self._pending[event.event_key] = batch[1]
                return
        # Preserve this leader's chronology, but an unrelated leader's slow
        # BUY on the same outcome must not hold up our already-owned exit.
        predecessor = self._token_tails.get(owner_key)
        events = [event]
        if event.side == "BUY" and self.settings.smart_sizing_enabled:
            task = asyncio.create_task(
                self._execute_batch(leader_id, events, batch_key, predecessor)
            )
            self._buy_batches[batch_key] = (events, task)
        else:
            task = asyncio.create_task(self._execute_queued(leader_id, event, predecessor))
        self._pending[event.event_key] = task
        self._token_tails[owner_key] = task

        def done(completed):
            for item in events:
                self._pending.pop(item.event_key, None)
            if self._buy_batches.get(batch_key, (None, None))[1] is completed:
                self._buy_batches.pop(batch_key, None)
            if self._token_tails.get(owner_key) is completed:
                self._token_tails.pop(owner_key, None)
            if not completed.cancelled() and completed.exception():
                log.error(
                    "copy_worker_failed",
                    event_key=event.event_key,
                    error=str(completed.exception()),
                )

        task.add_done_callback(done)

    def _buy_signal_reason(self, event):
        if event.side != "BUY":
            return None
        now = time.time()
        if not self._valid_timestamp(event):
            return "invalid_signal_timestamp"
        age_limit = (
            self.settings.max_signal_age_rtds_seconds
            if event.source == "rtds"
            else self.settings.max_signal_age_rest_seconds
        )
        if now - event.timestamp > age_limit:
            return "stale_signal"
        return None

    @staticmethod
    def _valid_timestamp(event):
        return 0 < event.timestamp <= time.time() + 2

    async def _execute_batch(self, leader_id, events, batch_key, predecessor):
        prepared = await self._prepare_event(leader_id, events[0])
        if predecessor:
            try:
                await asyncio.shield(predecessor)
            except Exception:
                log.warning("predecessor_failed", token_id=events[0].token_id)
        self._buy_batches.pop(batch_key, None)
        async with self._execution_slot(events[0], prepared, 10), SessionLocal() as session:
            leader = await session.get(Leader, leader_id)
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            if not leader or not leader.active or account.paused:
                return
            eligible = []
            received = set(
                await session.scalars(
                    select(SourceReceipt.event_key).where(
                        SourceReceipt.event_key.in_([e.event_key for e in events])
                    )
                )
            )
            for event in events:
                if event.event_key in received:
                    continue
                if self._buy_signal_reason(event) or not self._valid_trade(event):
                    await self.process_event(session, leader, event, prepared)
                else:
                    eligible.append(event)
            if eligible:
                qty = sum((e.size for e in eligible), Decimal(0))
                price = sum((e.size * e.price for e in eligible), Decimal(0)) / qty
                key = (
                    eligible[0].event_key
                    if len(eligible) == 1
                    else "batch:"
                    + hashlib.sha256(
                        "|".join(sorted(e.event_key for e in eligible)).encode()
                    ).hexdigest()
                )
                combined = replace(
                    eligible[0],
                    event_key=key,
                    size=qty,
                    price=price,
                    timestamp=min(e.timestamp for e in eligible),
                )
                await self.process_event(
                    session, leader, combined, prepared, source_events=eligible
                )
                log.info(
                    "copy_batch",
                    leader_id=leader_id,
                    fragments=len(eligible),
                    token_id=combined.token_id,
                    source=combined.source,
                    age_seconds=round(time.time() - combined.timestamp, 3),
                )
            await session.commit()
            messages = session.info.pop("notifications", [])
        for message in messages:
            await self.notify(message)

        first = events[0]
        log.info(
            "copy_latency",
            event_key=first.event_key,
            fragments=len(events),
            source=first.source,
            source_age_seconds=round(max(0, first.received_at - first.timestamp), 3),
            prepare_ms=round((prepared.ready_at - first.received_monotonic) * 1000, 1),
            after_prepare_ms=round((time.monotonic() - prepared.ready_at) * 1000, 1),
            bot_ms=round((time.monotonic() - first.received_monotonic) * 1000, 1),
            exchange_delay_seconds=prepared.exchange_delay,
        )

    @staticmethod
    def _valid_trade(event):
        return (
            event.side in {"BUY", "SELL"}
            and event.size.is_finite()
            and event.price.is_finite()
            and event.size > 0
            and Decimal(0) < event.price < Decimal(1)
        )

    async def _prepare_event(self, leader_id, event):
        if not self._valid_timestamp(event) or not self._valid_trade(event):
            return PreparedCopy(ready_at=time.monotonic(), error=ValueError("invalid_source_event"))
        if event.side == "SELL":
            return await self.prepare_copy(event)
        key = (leader_id, event.token_id)
        if self._buy_signal_reason(event) or self._sell_watermarks.get(key, -1) >= event.timestamp:
            return PreparedCopy(ready_at=time.monotonic(), error=ValueError("buy_not_current"))
        cancel = asyncio.Event()
        item = (event.timestamp, cancel)
        self._buy_preparations.setdefault(key, []).append(item)
        prepare = asyncio.create_task(self.prepare_copy(event))
        superseded = asyncio.create_task(cancel.wait())
        try:
            limit = (
                self.settings.max_signal_age_rtds_seconds
                if event.source == "rtds"
                else self.settings.max_signal_age_rest_seconds
            )
            done, _ = await asyncio.wait(
                [prepare, superseded],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=max(0, event.timestamp + limit - time.time()),
            )
            if not done:
                return PreparedCopy(
                    ready_at=time.monotonic(), error=ValueError("signal_age_deadline")
                )
            if superseded in done:
                return PreparedCopy(ready_at=time.monotonic(), error=ValueError("buy_not_current"))
            return await prepare
        finally:
            for task in (prepare, superseded):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prepare, superseded, return_exceptions=True)
            self._buy_preparations[key].remove(item)
            if not self._buy_preparations[key]:
                self._buy_preparations.pop(key)

    @asynccontextmanager
    async def _execution_slot(self, event, prepared, priority):
        # Recheck after waiting for the cash lock. Refresh outside it, then
        # reacquire; bounded attempts prevent a busy loop under overload.
        for attempt in range(4):
            await self._ledger_lock.acquire(priority)
            if (
                attempt == 3
                or prepared.error
                or prepared.book_error
                or self._buy_signal_reason(event)
                or (prepared.book is not None and time.monotonic() - prepared.book_at <= 0.25)
            ):
                break
            self._ledger_lock.release()
            try:
                async with self._exit_slots if event.side == "SELL" else self._prepare_slots:
                    prepared.book = await self.client.get_book(event.token_id)
                prepared.book_at = time.monotonic()
            except Exception as exc:
                prepared.book_error = exc
        try:
            yield
        finally:
            self._ledger_lock.release()

    async def prepare_copy(self, event: LeaderActivity) -> PreparedCopy:
        prepared = PreparedCopy()
        started = time.monotonic()
        tasks = []
        try:
            async with self._exit_slots if event.side == "SELL" else self._prepare_slots:
                market_task = asyncio.create_task(self.client.get_market(event.condition_id))
                fee_task = asyncio.create_task(
                    self.client.get_fee_rate(event.condition_id, event.title)
                )

                async def fetch_book():
                    book = await self.client.get_book(event.token_id)
                    return book, time.monotonic()

                book_task = asyncio.create_task(fetch_book())
                tasks = [market_task, fee_task, book_task]
                results = await asyncio.gather(market_task, fee_task, return_exceptions=True)
                market, fee_rate = results
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
            if not any(str(t.get("token_id")) == event.token_id for t in market.get("tokens", [])):
                raise ValueError("trade_token_mismatch")
            if market.get("closed") is not False or market.get("accepting_orders") is not True:
                raise ValueError("market_not_accepting_orders")
            delay = float(market["seconds_delay"])
            if not 0 <= delay <= 60:
                raise ValueError("invalid_market_delay")
            prepared.market, prepared.fee_rate, prepared.exchange_delay = market, fee_rate, delay
            # The exchange delay starts when the signal is received. Metadata and
            # book requests run during it; this models the fastest valid taker path.
            remaining = delay + self.settings.copy_latency_seconds - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
            prepared.book, prepared.book_at = await book_task
        except Exception as exc:
            prepared.error = exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        prepared.ready_at = time.monotonic()
        return prepared

    async def _execute_queued(self, leader_id, event, predecessor) -> None:
        prepared = await self._prepare_event(leader_id, event)
        if predecessor:
            try:
                await asyncio.shield(predecessor)
            except Exception:
                log.warning("predecessor_failed", token_id=event.token_id)
        async with self._execution_slot(event, prepared, 0 if event.side == "SELL" else 10):
            async with SessionLocal() as session:
                leader = await session.get(Leader, leader_id)
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
                # Recheck controls after waiting, not just at discovery time.
                if not leader or not leader.active or account.paused:
                    return
                await self.process_event(session, leader, event, prepared)
                await session.commit()
                messages = session.info.pop("notifications", [])
        for message in messages:
            await self.notify(message)
        log.info(
            "copy_latency",
            event_key=event.event_key,
            source=event.source,
            source_age_seconds=round(max(0, event.received_at - event.timestamp), 3),
            prepare_ms=round((prepared.ready_at - event.received_monotonic) * 1000, 1),
            after_prepare_ms=round((time.monotonic() - prepared.ready_at) * 1000, 1),
            bot_ms=round((time.monotonic() - event.received_monotonic) * 1000, 1),
            exchange_delay_seconds=prepared.exchange_delay,
        )

    async def _get_sizing_entry(self, session, leader_id, event, account):
        seconds = self.settings.smart_sizing_burst_seconds
        start = entry_bucket(event.timestamp, seconds)
        # A SELL is a barrier even if we had no position to sell. A later bucket
        # also prevents late REST events from reviving a superseded entry.
        barrier = await session.scalar(
            select(CopyTrade.id)
            .where(
                CopyTrade.leader_id == leader_id,
                CopyTrade.token_id == event.token_id,
                ((CopyTrade.side == "SELL") & (CopyTrade.timestamp >= start))
                | (CopyTrade.timestamp >= start + seconds),
            )
            .limit(1)
        )
        if barrier:
            return None, "sizing_entry_closed"
        key = (leader_id, event.token_id, start)
        entry = await session.get(SizingEntry, key)
        if entry is not None and entry.bucket_seconds != seconds:
            return None, "sizing_entry_closed"
        if entry is None:
            overlap = await session.scalar(
                select(SizingEntry.bucket_start)
                .where(
                    SizingEntry.leader_id == leader_id,
                    SizingEntry.token_id == event.token_id,
                    SizingEntry.bucket_start < start + seconds,
                    SizingEntry.bucket_start + SizingEntry.bucket_seconds > start,
                )
                .limit(1)
            )
            # Upgrade / switching legacy->smart mid-entry must not mint a fresh
            # budget for an entry whose old frozen cash/target we cannot recover.
            old_fill = await session.scalar(
                select(PaperOrder.id)
                .join(
                    CopyTrade,
                    CopyTrade.id == PaperOrder.copy_trade_id,
                )
                .where(
                    CopyTrade.leader_id == leader_id,
                    CopyTrade.token_id == event.token_id,
                    CopyTrade.timestamp >= start,
                    CopyTrade.timestamp < start + seconds,
                    CopyTrade.side == "BUY",
                    PaperOrder.filled_shares > 0,
                    PaperOrder.status.in_(["filled", "partial"]),
                )
                .limit(1)
            )
            if overlap is not None or old_fill is not None:
                return None, "sizing_entry_closed"
            profile = await session.get(LeaderSizingProfile, leader_id)
            if (
                profile is None
                or profile.reference_notional <= 0
                or profile.sample_count < self.settings.smart_sizing_min_samples
                or profile.bucket_seconds != seconds
                or profile.sample_end > start
                or profile.sample_end < int(time.time()) - 7 * 86400
            ):
                return None, "sizing_profile_unavailable"
            entry = SizingEntry(
                leader_id=leader_id,
                token_id=event.token_id,
                bucket_start=start,
                bucket_seconds=seconds,
                cash_at_start=account.paper_balance,
                base_budget=account.paper_balance * self.settings.copy_balance_pct,
                reference_notional=profile.reference_notional,
                max_budget=account.max_trade_size,
                max_multiplier=self.settings.smart_sizing_max_multiplier,
                leader_notional=Decimal(0),
                leader_shares=Decimal(0),
                spent=Decimal(0),
                closed=False,
            )
            session.add(entry)
        if entry.closed:
            return None, "sizing_entry_closed"
        entry.leader_notional += event.size * event.price
        entry.leader_shares += event.size
        return entry, None

    async def process_event(
        self,
        session,
        leader: Leader,
        event: LeaderActivity,
        prepared: PreparedCopy | None = None,
        *,
        source_events=None,
    ) -> None:
        detection_lag = max(0, (event.received_at or time.time()) - event.timestamp)
        source_events = source_events or [event]
        if await session.scalar(
            select(SourceReceipt.event_key)
            .where(SourceReceipt.event_key.in_([e.event_key for e in source_events]))
            .limit(1)
        ):
            return
        if event.trader_name and not leader.label:
            leader.label = event.trader_name[:120]
        copy_trade = CopyTrade(
            leader_id=leader.id,
            event_key=event.event_key,
            timestamp=event.timestamp if self._valid_timestamp(event) else 0,
            token_id=event.token_id,
            condition_id=event.condition_id,
            side=event.side,
            leader_size=event.size if event.size.is_finite() else Decimal(0),
            leader_price=event.price if event.price.is_finite() else Decimal(0),
            status="detected",
        )
        try:
            async with session.begin_nested():
                session.add(copy_trade)
                await session.flush()
                for source in source_events:
                    session.add(
                        SourceReceipt(event_key=source.event_key, copy_trade_id=copy_trade.id)
                    )
                await session.flush()
        except IntegrityError:
            return

        if not self._valid_trade(event):
            copy_trade.status, copy_trade.skip_reason = "skipped", "invalid_size_or_price"
            self.record_rejection(session, copy_trade, event, copy_trade.skip_reason)
            return
        if not self._valid_timestamp(event):
            copy_trade.status, copy_trade.skip_reason = "skipped", "invalid_signal_timestamp"
            self.record_rejection(session, copy_trade, event, copy_trade.skip_reason)
            return

        if event.side == "SELL":
            await session.execute(
                update(SizingEntry)
                .where(
                    SizingEntry.leader_id == leader.id,
                    SizingEntry.token_id == event.token_id,
                    SizingEntry.bucket_start <= event.timestamp,
                )
                .values(closed=True)
            )

        leader_pos = await session.scalar(
            select(LeaderPosition).where(
                LeaderPosition.leader_id == leader.id,
                LeaderPosition.token_id == event.token_id,
            )
        )
        before_leader_shares = leader_pos.shares if leader_pos else Decimal(0)
        await self.update_leader_position(session, leader.id, event, leader_pos)
        account = await get_or_create_account(session, self.settings.paper_initial_balance)
        policy = await get_execution_policy(session, self.settings)
        if event.side == "SELL":
            await self._accept_exit(
                session,
                leader,
                event,
                copy_trade,
                account,
                before_leader_shares,
                policy.slippage_price,
                prepared,
            )
            return
        signal_reason = self._buy_signal_reason(event)
        if self._sell_watermarks.get((leader.id, event.token_id), -1) >= event.timestamp:
            signal_reason = "buy_superseded_by_sell"
        elif await session.scalar(
            select(CopyTrade.id)
            .where(
                CopyTrade.leader_id == leader.id,
                CopyTrade.token_id == event.token_id,
                CopyTrade.side == "SELL",
                CopyTrade.timestamp >= event.timestamp,
            )
            .limit(1)
        ):
            # The in-memory fast path is empty after restart; the committed
            # SELL remains a barrier even in the legacy sizing mode.
            signal_reason = "buy_superseded_by_sell"
        pending_exit = await session.get(ExitIntent, (leader.id, event.token_id))
        if pending_exit and pending_exit.remaining > 0:
            signal_reason = "exit_pending"
        if signal_reason:
            copy_trade.status, copy_trade.skip_reason = "skipped", signal_reason
            self.record_rejection(session, copy_trade, event, signal_reason)
            log.info(
                "copy_rejected",
                event_key=event.event_key,
                reason=signal_reason,
                source=event.source,
                source_timestamp=event.timestamp,
                received_age_seconds=round(max(0, event.received_at - event.timestamp), 3),
                age_limit_seconds=(
                    self.settings.max_signal_age_rtds_seconds
                    if event.source == "rtds"
                    else self.settings.max_signal_age_rest_seconds
                ),
                current_age_seconds=round(max(0, time.time() - event.timestamp), 3),
                source_age_seconds=round(time.time() - event.timestamp, 3),
            )
            return
        try:
            prepared = prepared or await self.prepare_copy(event)
            if prepared.error:
                raise prepared.error
            fee_rate = prepared.fee_rate
        except Exception as exc:
            copy_trade.status = "failed"
            detail = str(exc) or type(exc).__name__
            copy_trade.skip_reason = f"market_data:{detail}"[:200]
            self.record_rejection(session, copy_trade, event, copy_trade.skip_reason)
            log.warning("copy_data_rejected", condition_id=event.condition_id, error=str(exc))
            return
        smart_buy = event.side == "BUY" and self.settings.smart_sizing_enabled
        smart_entry = decision = None
        if smart_buy:
            # Sizing needs the executable ask. No fixed-dollar budget is applied.
            target_shares = Decimal(0)
        elif event.side == "BUY":
            # The configured copy size is a total cash budget. Reserve room for
            # the taker fee so apply_fill cannot reject a fill after consuming
            # the whole available balance on notional alone.
            leader_notional = event.size * event.price
            buy_budget = self.calculate_buy_budget(
                account, self.settings, leader_notional, fee_rate
            )
            existing = await get_position(session, event.token_id)
            existing_exposure = existing.cost_basis if existing else Decimal(0)
            base_capacity = self.calculate_own_buy_capacity(account, self.settings, fee_rate)
            own_capacity = min(
                base_capacity,
                max(Decimal(0), self.settings.max_outcome_exposure - existing_exposure),
            )
            buy_budget = min(
                buy_budget,
                own_capacity,
            )
            target_shares = buy_budget / event.price if event.price else Decimal(0)
            if buy_budget < self.settings.min_copy_notional:
                copy_trade.status = "skipped"
                copy_trade.skip_reason = "below_min_copy_notional"
                self.record_rejection(
                    session, copy_trade, event, copy_trade.skip_reason, target_shares
                )
                log.info(
                    "copy_rejected",
                    leader=leader.address,
                    side=event.side,
                    event_key=event.event_key,
                    reason=copy_trade.skip_reason,
                    detection_lag_seconds=round(detection_lag, 3),
                    paper_balance=str(account.paper_balance),
                    leader_notional=str(leader_notional),
                    calculated_budget=str(buy_budget),
                )
                return
        if target_shares <= 0 and not smart_buy:
            copy_trade.status = "skipped"
            copy_trade.skip_reason = "invalid_size_or_price"
            self.record_rejection(session, copy_trade, event, copy_trade.skip_reason)
            return
        try:
            if prepared.book_error:
                raise prepared.book_error
            book = prepared.book
            if book is None or time.monotonic() - prepared.book_at > 0.25:
                raise ValueError("execution_book_expired")
            if event.side == "BUY":
                if smart_buy:
                    smart_entry, reason = await self._get_sizing_entry(
                        session, leader.id, event, account
                    )
                    if smart_entry is None:
                        copy_trade.status, copy_trade.skip_reason = "skipped", reason
                        self.record_rejection(session, copy_trade, event, reason)
                        return
                    existing = await get_position(session, event.token_id)
                    exposure = existing.cost_basis if existing else Decimal(0)
                    ask = book.asks[0][0] if book.asks else Decimal(0)
                    decision = entry_budget(
                        smart_entry,
                        ask=ask,
                        event_price=event.price,
                        cash=account.paper_balance,
                        exposure_room=max(
                            Decimal(0), self.settings.max_outcome_exposure - exposure
                        ),
                        current_max=account.max_trade_size,
                        fee_rate=fee_rate,
                        slippage_price=policy.slippage_price,
                        min_notional=self.settings.min_copy_notional,
                        min_shares=book.min_order_size,
                    )
                    session.add(
                        SizingAudit(
                            copy_trade_id=copy_trade.id,
                            bucket_start=smart_entry.bucket_start,
                            base_budget=smart_entry.base_budget,
                            reference_notional=smart_entry.reference_notional,
                            leader_notional=smart_entry.leader_notional,
                            leader_vwap=decision.leader_vwap,
                            price_factor=decision.price_factor,
                            target_budget=decision.target_budget,
                            spent_before=smart_entry.spent,
                            order_budget=decision.order_budget,
                        )
                    )
                    buy_budget = decision.order_budget
                    target_shares = buy_budget / ask if ask > 0 else Decimal(0)
                    log.info(
                        "copy_sizing",
                        leader=leader.address,
                        token_id=event.token_id,
                        base=str(smart_entry.base_budget),
                        typical_entry=str(smart_entry.reference_notional),
                        leader_entry=str(smart_entry.leader_notional),
                        leader_vwap=str(decision.leader_vwap),
                        price_factor=str(decision.price_factor),
                        target=str(decision.target_budget),
                        spent=str(smart_entry.spent),
                        order_budget=str(buy_budget),
                        reason=decision.reason,
                        ask=str(ask),
                        reference_price=str(decision.reference_price),
                        slippage_price=str(policy.slippage_price),
                        min_buy_price=str(decision.reference_price - policy.slippage_price),
                        max_buy_price=str(decision.reference_price + policy.slippage_price),
                        min_copy_notional=str(self.settings.min_copy_notional),
                        min_order_notional=str(book.min_order_size * ask),
                        cash_available=str(account.paper_balance),
                        exposure_room=str(
                            max(Decimal(0), self.settings.max_outcome_exposure - exposure)
                        ),
                    )
                    if decision.reason:
                        copy_trade.status, copy_trade.skip_reason = "skipped", decision.reason
                        self.record_rejection(
                            session, copy_trade, event, decision.reason, target_shares
                        )
                        return
                else:
                    buy_budget = self.ensure_book_minimum_budget(
                        buy_budget,
                        own_capacity,
                        book,
                        event.price,
                        policy.slippage_price,
                    )
                if book.asks:
                    target_shares = buy_budget / book.asks[0][0]
                final_reason = self._buy_signal_reason(event)
                if final_reason:
                    copy_trade.status, copy_trade.skip_reason = "skipped", final_reason
                    self.record_rejection(session, copy_trade, event, final_reason, target_shares)
                    return
                if time.monotonic() - prepared.book_at > 0.25:
                    raise ValueError("execution_book_expired")
                fill = execute_buy_fak_by_budget(
                    book,
                    buy_budget,
                    fee_rate,
                    reference_price=decision.reference_price if decision else event.price,
                    slippage_price=policy.slippage_price,
                )
            else:
                fill = execute_fak(
                    book,
                    event.side,
                    target_shares,
                    fee_rate,
                    reference_price=event.price,
                    slippage_price=policy.slippage_price,
                )
        except Exception as exc:
            copy_trade.status = "failed"
            detail = str(exc) or type(exc).__name__
            copy_trade.skip_reason = f"book_error:{detail}"[:200]
            self.record_rejection(session, copy_trade, event, copy_trade.skip_reason, target_shares)
            return

        order = PaperOrder(
            copy_trade_id=copy_trade.id,
            token_id=event.token_id,
            side=event.side,
            requested_shares=target_shares,
            filled_shares=fill.shares,
            average_fill_price=fill.average_price,
            fee=fill.fee,
            status=fill.status,
            reason=fill.reason,
        )
        session.add(order)
        if fill.shares <= 0:
            copy_trade.status = "skipped"
            copy_trade.skip_reason = fill.reason or "no_fill"
            best_price = (
                book.asks[0][0]
                if event.side == "BUY" and book.asks
                else book.bids[0][0]
                if book.bids
                else None
            )
            log.info(
                "copy_rejected",
                leader=leader.address,
                side=event.side,
                reason=copy_trade.skip_reason,
                detection_lag_seconds=round(detection_lag, 3),
                leader_price=str(event.price),
                best_book_price=str(best_price) if best_price is not None else None,
                requested_shares=str(target_shares),
                reference_price=str(event.price),
                best_ask=str(book.asks[0][0]) if event.side == "BUY" and book.asks else None,
                slippage_price=str(policy.slippage_price),
                max_buy_price=str(event.price + policy.slippage_price)
                if event.side == "BUY"
                else None,
            )
            return
        try:
            await apply_fill(
                session,
                fill,
                event.token_id,
                event.side,
                event.title,
                event.outcome,
                event.condition_id,
                account,
            )
        except ValueError as exc:
            copy_trade.status = "skipped"
            copy_trade.skip_reason = str(exc)
            order.status = "rejected"
            order.reason = str(exc)
            order.filled_shares = order.average_fill_price = order.fee = Decimal(0)
            return
        copy_trade.status = "executed"
        if smart_entry:
            smart_entry.spent += fill.notional + fill.fee
        log.info(
            "copy_executed",
            leader=leader.address,
            side=event.side,
            detection_lag_seconds=round(detection_lag, 3),
            leader_price=str(event.price),
            fill_price=str(fill.average_price),
            filled_shares=str(fill.shares),
            fee=str(fill.fee),
        )
        # Keep the chat quiet: only successful BUY copies are user-facing
        # notifications. Rejections/partial misses remain visible in Ордера.
        if event.side == "BUY":
            message = self.build_buy_notification(leader, event, fill)
            if decision:
                message += (
                    f"\n\nБюджет серии с комиссией: ${decision.target_budget:.2f}"
                    f"\nИспользовано в серии: ${smart_entry.spent:.2f}"
                )
            session.info.setdefault("notifications", []).append(message)

    async def update_leader_position(
        self, session, leader_id: int, event: LeaderActivity, position: LeaderPosition | None
    ) -> None:
        if not position:
            position = LeaderPosition(
                leader_id=leader_id, token_id=event.token_id, shares=Decimal(0)
            )
            session.add(position)
            await session.flush()
        if event.side == "BUY":
            position.shares += event.size
        else:
            position.shares = max(Decimal(0), position.shares - event.size)
        # Late-indexed events must not resurrect the leader's old inventory.
        newer = await session.scalar(
            select(CopyTrade.id)
            .where(
                CopyTrade.leader_id == leader_id,
                CopyTrade.token_id == event.token_id,
                CopyTrade.timestamp > event.timestamp,
            )
            .limit(1)
        )
        if newer:
            rows = (
                await session.execute(
                    select(CopyTrade.side, CopyTrade.leader_size)
                    .where(
                        CopyTrade.leader_id == leader_id,
                        CopyTrade.token_id == event.token_id,
                        CopyTrade.leader_size > 0,
                        CopyTrade.leader_price > 0,
                        CopyTrade.leader_price < 1,
                        CopyTrade.timestamp > 0,
                    )
                    .order_by(CopyTrade.timestamp, CopyTrade.id)
                )
            ).all()
            shares = Decimal(0)
            for side, qty in rows:
                shares = shares + qty if side == "BUY" else max(Decimal(0), shares - qty)
            position.shares = shares

    async def _accept_exit(
        self, session, leader, event, trade, account, before, distance, prepared
    ):
        pos = await get_position(session, event.token_id)
        holdings, warnings = await inventory(session, event.token_id)
        own = holdings.get((event.token_id, leader.id), Holding())
        newer_buy = await session.scalar(
            select(CopyTrade.id)
            .join(PaperOrder)
            .where(
                CopyTrade.leader_id == leader.id,
                CopyTrade.token_id == event.token_id,
                CopyTrade.side == "BUY",
                CopyTrade.timestamp > event.timestamp,
                PaperOrder.filled_shares > 0,
                PaperOrder.status.in_(["filled", "partial"]),
            )
            .limit(1)
        )
        if newer_buy:
            trade.status, trade.skip_reason = "skipped", "out_of_order_exit"
            self.record_rejection(session, trade, event, trade.skip_reason)
            return
        if not pos or own.shares <= 0 or before <= 0 or warnings:
            reason = "ambiguous_inventory" if warnings else "no_position_to_sell"
            trade.status, trade.skip_reason = "skipped", reason
            self.record_rejection(session, trade, event, reason)
            return
        key = (leader.id, event.token_id)
        intent = await session.get(ExitIntent, key)
        if intent and intent.remaining > 0 and event.timestamp < intent.source_timestamp:
            trade.status, trade.skip_reason = "skipped", "out_of_order_exit"
            self.record_rejection(session, trade, event, trade.skip_reason)
            return
        if intent is None:
            intent = ExitIntent(
                leader_id=leader.id,
                token_id=event.token_id,
                remaining=Decimal(0),
                generation=0,
                attempts=0,
                position_id=pos.id,
            )
            session.add(intent)
        if intent.position_id != pos.id:
            intent.remaining = Decimal(0)
        remaining = min(own.shares, intent.remaining)
        free = max(Decimal(0), own.shares - remaining)
        # Each fragment adds only its share of inventory not ALREADY reserved.
        remaining += free * min(Decimal(1), event.size / before)
        # Close a sub-0.01-share rounding tail only after >=99% is requested.
        if own.shares - remaining < Decimal("0.01") and remaining >= own.shares * Decimal("0.99"):
            remaining = own.shares
        intent.remaining = min(remaining, own.shares)
        intent.position_id, intent.copy_trade_id = pos.id, trade.id
        # A genuinely new SELL may update the limit; retry never lowers it.
        new_floor = max(Decimal(0), event.price - distance)
        intent.min_price = (
            new_floor
            if intent.generation == 0 or event.timestamp >= intent.source_timestamp
            else intent.min_price
        )
        intent.source_timestamp = event.timestamp
        intent.generation += 1
        intent.attempts, intent.next_attempt = 0, Decimal(0)
        trade.status, trade.skip_reason = "exit_pending", None
        if prepared is None:
            prepared = PreparedCopy(error=ValueError("exit_awaiting_preparation"))
        await self._fill_exit(session, intent, account, prepared)
        if trade.status != "executed":
            self.record_rejection(
                session, trade, event, intent.last_reason or "exit_pending", intent.remaining
            )
            trade.skip_reason = intent.last_reason

    async def monitor_risk_once(self) -> None:
        async with SessionLocal() as session:
            token_ids = list(
                await session.scalars(
                    select(Position.token_id)
                    .join(RiskRule, RiskRule.token_id == Position.token_id)
                    .where(Position.shares > 0, RiskRule.enabled.is_(True))
                )
            )
        await asyncio.gather(*(self._monitor_risk_token(token) for token in token_ids))

    async def _monitor_risk_token(self, token_id: str) -> None:
        try:
            async with self._maintenance_slots:
                book = await self.client.get_book(token_id)
            if not book.bids:
                return
            async with self._ledger_lock.hold(0), SessionLocal() as session:
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
                position = await get_position(session, token_id)
                rule = await get_risk(session, token_id)
                if account.paused or not position or not rule or not rule.enabled:
                    return
                current = book.bids[0][0]
                if not rule.high_water_price or current > rule.high_water_price:
                    rule.high_water_price = current
                trigger = None
                if rule.stop_loss_pct is not None and current <= position.average_price * (
                    Decimal(1) - rule.stop_loss_pct
                ):
                    trigger = "stop-loss"
                elif rule.take_profit_pct is not None and current >= position.average_price * (
                    Decimal(1) + rule.take_profit_pct
                ):
                    trigger = "take-profit"
                elif (
                    rule.trailing_pct is not None
                    and rule.high_water_price
                    and current <= rule.high_water_price * (Decimal(1) - rule.trailing_pct)
                ):
                    trigger = "trailing-stop"
                if not trigger:
                    await session.commit()
                    return
                position_id, requested_shares = position.id, position.shares
                rule_values = (rule.stop_loss_pct, rule.take_profit_pct, rule.trailing_pct)
                event = LeaderActivity(
                    event_key="risk",
                    timestamp=int(time.time()),
                    condition_id=position.condition_id,
                    token_id=token_id,
                    side="SELL",
                    size=requested_shares,
                    price=current,
                    title=position.title,
                    outcome=position.outcome,
                    slug="",
                )
                await session.commit()
            prepared = await self.prepare_copy(event)
            if prepared.error:
                raise prepared.error
            async with self._execution_slot(event, prepared, 0), SessionLocal() as session:
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
                position = await get_position(session, token_id)
                rule = await get_risk(session, token_id)
                if (
                    account.paused
                    or not position
                    or position.id != position_id
                    or not rule
                    or not rule.enabled
                    or rule_values != (rule.stop_loss_pct, rule.take_profit_pct, rule.trailing_pct)
                ):
                    return
                requested_shares = min(requested_shares, position.shares)
                book = prepared.book
                if (
                    book is None
                    or prepared.book_error
                    or time.monotonic() - prepared.book_at > 0.25
                ):
                    return
                shares_before = position.shares
                policy = await get_execution_policy(session, self.settings)
                fill = execute_fak(
                    book,
                    "SELL",
                    requested_shares,
                    prepared.fee_rate,
                    reference_price=event.price,
                    slippage_price=policy.slippage_price,
                )
                if fill.shares <= 0:
                    return
                remaining = position.shares - fill.shares
                await apply_fill(
                    session,
                    fill,
                    token_id,
                    "SELL",
                    position.title,
                    position.outcome,
                    position.condition_id,
                    account,
                )
                rule.enabled = remaining > Decimal("0.00000001")
                await session.execute(
                    update(SizingEntry)
                    .where(
                        SizingEntry.token_id == token_id,
                    )
                    .values(closed=True)
                )
                await session.execute(
                    update(ExitIntent)
                    .where(
                        ExitIntent.token_id == token_id,
                        ExitIntent.remaining > 0,
                    )
                    .values(
                        remaining=ExitIntent.remaining * remaining / shares_before,
                        generation=ExitIntent.generation + 1,
                    )
                )
                session.add(
                    PaperOrder(
                        token_id=position.token_id,
                        side="SELL",
                        requested_shares=requested_shares,
                        filled_shares=fill.shares,
                        average_fill_price=fill.average_price,
                        fee=fill.fee,
                        status=fill.status,
                        reason=trigger,
                    )
                )
                await session.commit()
        except Exception as exc:
            log.warning("risk_data_unavailable", token_id=token_id, error=str(exc))

    async def settle_once(self) -> None:
        async with SessionLocal() as session:
            positions = list(
                (await session.scalars(select(Position).where(Position.shares > 0))).all()
            )
        await asyncio.gather(*(self._settle_position(position) for position in positions))

    async def retry_exits_once(self) -> None:
        """Only explicit, still-open intents. Never replay old rejected orders."""
        if not self.settings.exit_retry_enabled:
            return
        async with SessionLocal() as session:
            intents = list(
                await session.scalars(
                    select(ExitIntent)
                    .where(
                        ExitIntent.remaining > 0,
                        ExitIntent.next_attempt <= Decimal(str(time.time())),
                    )
                    .limit(128)
                )
            )
        for intent in intents:
            self._schedule_exit((intent.leader_id, intent.token_id))

    def _schedule_exit(self, key):
        if key in self._exit_workers or self.stop_event.is_set():
            return
        task = asyncio.create_task(self._retry_exit(key))
        self._exit_workers[key] = task

        def done(completed):
            self._exit_workers.pop(key, None)
            if not completed.cancelled() and completed.exception():
                log.error("exit_worker_failed", key=key, error=str(completed.exception()))

        task.add_done_callback(done)

    async def _retry_exit(self, key):
        async with SessionLocal() as session:
            intent = await session.get(ExitIntent, key)
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            leader = await session.get(Leader, key[0])
            pos = await get_position(session, key[1])
            if (
                not intent
                or intent.remaining <= 0
                or account.paused
                or not leader
                or not leader.active
                or intent.next_attempt > Decimal(str(time.time()))
            ):
                return
            generation = intent.generation
            if not pos or pos.id != intent.position_id:
                async with self._ledger_lock.hold(0):
                    current = await session.get(ExitIntent, key, populate_existing=True)
                    if current and current.generation == generation:
                        current.remaining = 0
                        current.last_reason = "position_closed"
                        await session.commit()
                return
            event = LeaderActivity(
                event_key="exit_retry",
                timestamp=int(time.time()),
                condition_id=pos.condition_id,
                token_id=pos.token_id,
                side="SELL",
                size=intent.remaining,
                price=max(Decimal("0.000001"), intent.min_price),
                title=pos.title,
                outcome=pos.outcome,
                slug="",
            )
        # Metadata, exchange delay and book refresh never hold the cash lock.
        prepared = await self.prepare_copy(event)
        async with self._execution_slot(event, prepared, 0), SessionLocal() as session:
            intent = await session.get(ExitIntent, key)
            leader = await session.get(Leader, key[0])
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            if (
                not intent
                or intent.remaining <= 0
                or intent.generation != generation
                or account.paused
                or not leader
                or not leader.active
            ):
                return
            await self._fill_exit(session, intent, account, prepared)
            await session.commit()

    async def _fill_exit(self, session, intent, account, prepared):
        pos = await get_position(session, intent.token_id)
        holdings, warnings = await inventory(session, intent.token_id)
        own = holdings.get((intent.token_id, intent.leader_id), Holding())
        if not pos or pos.id != intent.position_id or own.shares <= Decimal("0.00000001"):
            intent.remaining, intent.last_reason = 0, "position_closed"
            return
        if warnings:
            intent.last_reason = "ambiguous_inventory"
        elif prepared.error or prepared.book_error:
            intent.last_reason = "exit_market_data_unavailable"
        elif prepared.book is None or time.monotonic() - prepared.book_at > 0.25:
            intent.last_reason = "exit_book_expired"
        else:
            target = min(intent.remaining, own.shares, pos.shares)
            fill = execute_fak(
                prepared.book,
                "SELL",
                target,
                prepared.fee_rate,
                reference_price=intent.min_price,
                slippage_price=Decimal(0),
            )
            intent.last_reason = fill.reason
            if fill.shares > 0:
                await apply_fill(
                    session,
                    fill,
                    pos.token_id,
                    "SELL",
                    pos.title,
                    pos.outcome,
                    pos.condition_id,
                    account,
                    cost_to_release=own.cost * fill.shares / own.shares,
                )
                intent.remaining = max(Decimal(0), intent.remaining - fill.shares)
                if intent.remaining <= Decimal("0.00000001"):
                    intent.remaining = 0
                session.add(
                    PaperOrder(
                        copy_trade_id=intent.copy_trade_id,
                        token_id=pos.token_id,
                        side="SELL",
                        requested_shares=target,
                        filled_shares=fill.shares,
                        average_fill_price=fill.average_price,
                        fee=fill.fee,
                        status=fill.status,
                        reason="exit_intent",
                    )
                )
                trade = await session.get(CopyTrade, intent.copy_trade_id)
                trade.status, trade.skip_reason = "executed", None
                intent.attempts = 0
                log.info(
                    "exit_filled",
                    leader_id=intent.leader_id,
                    token_id=intent.token_id,
                    shares=str(fill.shares),
                    remaining=str(intent.remaining),
                    fee=str(fill.fee),
                )
        intent.attempts += 1
        wait = min(30, self.settings.exit_retry_seconds * 2 ** min(intent.attempts - 1, 5))
        intent.next_attempt = Decimal(str(time.time() + wait))

    async def _settle_position(self, snapshot: Position) -> None:
        try:
            async with self._maintenance_slots:
                payout = await self.client.get_resolution(
                    snapshot.condition_id, snapshot.outcome, snapshot.token_id
                )
            if payout is None:
                return
            if not payout.is_finite() or not Decimal(0) <= payout <= Decimal(1):
                raise ValueError("invalid_resolution_payout")
            async with self._ledger_lock.hold(0), SessionLocal() as session:
                # Shares/cost/balance may have changed while resolution was fetched.
                position = await session.get(Position, snapshot.id)
                if not position or position.token_id != snapshot.token_id or position.shares <= 0:
                    return
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
                proceeds = position.shares * payout
                pnl = proceeds - position.cost_basis
                account.paper_balance += proceeds
                account.realized_pnl += pnl
                await session.execute(
                    update(SizingEntry)
                    .where(
                        SizingEntry.token_id == position.token_id,
                    )
                    .values(closed=True)
                )
                await session.execute(
                    update(ExitIntent)
                    .where(
                        ExitIntent.token_id == position.token_id,
                    )
                    .values(
                        remaining=0,
                        generation=ExitIntent.generation + 1,
                        last_reason="market_settled",
                    )
                )
                session.add(
                    PaperOrder(
                        token_id=position.token_id,
                        side="SELL",
                        requested_shares=position.shares,
                        filled_shares=position.shares,
                        average_fill_price=payout,
                        fee=Decimal(0),
                        status="settled",
                        reason="resolution:won"
                        if payout == 1
                        else "resolution:lost"
                        if payout == 0
                        else "resolution:split",
                    )
                )
                rule = await get_risk(session, position.token_id)
                if rule:
                    rule.enabled = False
                await session.delete(position)
                await session.commit()
        except Exception as exc:
            log.warning(
                "settlement_check_failed", condition_id=snapshot.condition_id, error=str(exc)
            )
