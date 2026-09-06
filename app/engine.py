from __future__ import annotations

import asyncio
import html
import time
from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import SessionLocal
from .models import CopyTrade, Leader, LeaderPosition, PaperOrder, Position, RiskRule
from .paper import execute_buy_fak_by_budget, execute_fak
from .polymarket import Book, LeaderActivity, PolymarketClient, copy_event_key
from .repository import apply_fill, get_or_create_account, get_position, get_risk

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
    leader_value: Decimal | None = None


class CopyEngine:
    def __init__(self, settings: Settings, client: PolymarketClient):
        self.settings = settings
        self.client = client
        self.stop_event = asyncio.Event()
        self.notifications: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        # Network preparation is concurrent; all portfolio mutations remain serialized.
        self._ledger_lock = asyncio.Lock()
        self._prepare_slots = asyncio.Semaphore(settings.copy_prepare_concurrency)
        self._poll_slots = asyncio.Semaphore(8)
        self._maintenance_slots = asyncio.Semaphore(4)
        self._pending: dict[str, asyncio.Task] = {}
        self._leader_polls: dict[int, asyncio.Task] = {}
        self._token_tails: dict[str, asyncio.Task] = {}
        self._leader_floors: dict[int, int] = {}
        self._leader_avg_trade_size: dict[int, Decimal] = {}
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

    @classmethod
    def calculate_smart_buy_budget(
        cls, account, settings: Settings, leader_notional: Decimal,
        leader_value: Decimal | None, fee_rate: Decimal,
        leader_avg_trade_size: Decimal | None = None,
    ) -> Decimal:
        """Match the leader's capital risk while never exceeding our cash guards."""
        if leader_avg_trade_size and leader_avg_trade_size > 0:
            # Risk follows the leader's observed trade distribution, while the
            # cash percentage remains our own base risk budget.
            base = cls.calculate_own_buy_capacity(account, settings, fee_rate)
            return min(
                base * leader_notional / leader_avg_trade_size,
                account.max_trade_size,
                account.paper_balance / (Decimal(1) + fee_rate),
            )
        if not settings.smart_sizing_enabled or not leader_value or leader_value <= 0:
            return cls.calculate_buy_budget(account, settings, leader_notional, fee_rate)
        own_equity = max(Decimal(0), account.paper_balance)
        ratio = min(settings.smart_sizing_max_ratio, own_equity / leader_value)
        proportional = leader_notional * ratio
        # Smart mode already scales by our equity/leader equity. Do not apply
        # the legacy fixed 5% cash cap a second time; max_trade_size and cash
        # remain hard safety limits.
        cash_budget = account.paper_balance / (Decimal(1) + fee_rate)
        own_capacity = min(cash_budget, account.max_trade_size, account.trade_size)
        return min(own_capacity, proportional)

    @staticmethod
    def ensure_book_minimum_budget(
        budget: Decimal,
        own_capacity: Decimal,
        book,
        reference_price: Decimal,
        slippage_bps: int,
    ) -> Decimal:
        """Raise a valid small copy to the exchange's share minimum when affordable."""
        if not book.asks or reference_price <= 0:
            return budget
        best_ask = book.asks[0][0]
        max_price = reference_price * (Decimal(1) + Decimal(slippage_bps) / Decimal(10000))
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
        ]
        try:
            await asyncio.gather(*loops)
        finally:
            tasks = loops + list(self._leader_polls.values()) + list(self._pending.values())
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
            leader = await session.scalar(select(Leader).where(Leader.address == event.trader_address))
            if not leader or not leader.active or not leader.initialized:
                return "untracked"
            if await session.scalar(select(CopyTrade.id).where(CopyTrade.event_key == event.event_key)):
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
                activities = await self.client.get_activity(leader.address)
            except Exception:
                log.exception("leader_activity_failed", leader=leader.address)
                return
            if not activities:
                return
            async with SessionLocal() as session:
                db_leader = await session.scalar(select(Leader).where(Leader.id == leader.id))
                if not db_leader or not db_leader.active:
                    return
                notionals = [
                    event.size * event.price
                    for event in activities
                    if event.size > 0 and event.price > 0
                ]
                if notionals:
                    self._leader_avg_trade_size[leader.id] = sum(notionals, Decimal(0)) / len(notionals)
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

    def _schedule_copy(self, leader_id: int, event: LeaderActivity) -> None:
        if event.event_key in self._pending:
            return
        if len(self._pending) >= self.settings.copy_queue_limit:
            log.warning("copy_queue_full", pending=len(self._pending), event_key=event.event_key)
            return  # REST retries it; no checkpoint or fake rejection.
        if not event.received_at:
            event.received_at, event.received_monotonic = time.time(), time.monotonic()
        predecessor = self._token_tails.get(event.token_id)
        task = asyncio.create_task(self._execute_queued(leader_id, event, predecessor))
        self._pending[event.event_key] = task
        self._token_tails[event.token_id] = task

        def done(completed):
            self._pending.pop(event.event_key, None)
            if self._token_tails.get(event.token_id) is completed:
                self._token_tails.pop(event.token_id, None)
            if not completed.cancelled() and completed.exception():
                log.error(
                    "copy_worker_failed",
                    event_key=event.event_key,
                    error=str(completed.exception()),
                )

        task.add_done_callback(done)

    async def prepare_copy(self, event: LeaderActivity) -> PreparedCopy:
        prepared = PreparedCopy()
        started = time.monotonic()
        try:
            async with self._prepare_slots:
                market_task = asyncio.create_task(self.client.get_market(event.condition_id))
                fee_task = asyncio.create_task(self.client.get_fee_rate(event.condition_id, event.title))
                book_task = asyncio.create_task(self.client.get_book(event.token_id))
                value_task = (
                    asyncio.create_task(self.client.get_user_position_value(event.trader_address))
                    if event.trader_address else None
                )
                results = await asyncio.gather(market_task, fee_task, return_exceptions=True)
                market, fee_rate = results
                if value_task:
                    try:
                        prepared.leader_value = await value_task
                    except Exception:
                        log.info("leader_value_unavailable", address=event.trader_address)
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
            prepared.book = await book_task
            prepared.book_at = time.monotonic()
        except Exception as exc:
            prepared.error = exc
        prepared.ready_at = time.monotonic()
        return prepared

    async def _execute_queued(self, leader_id, event, predecessor) -> None:
        prepared = await self.prepare_copy(event)
        if predecessor:
            await asyncio.shield(predecessor)
        # Obtain the execution snapshot after the prior fill on this token,
        # but without blocking unrelated tokens behind a slow HTTP request.
        if not prepared.error and prepared.book is None:
            try:
                async with self._prepare_slots:
                    prepared.book = await self.client.get_book(event.token_id)
                prepared.book_at = time.monotonic()
            except Exception as exc:
                prepared.book_error = exc
        async with self._ledger_lock:
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
            source_age_seconds=round(max(0, event.received_at - event.timestamp), 3),
            prepare_ms=round((prepared.ready_at - event.received_monotonic) * 1000, 1),
            after_prepare_ms=round((time.monotonic() - prepared.ready_at) * 1000, 1),
            bot_ms=round((time.monotonic() - event.received_monotonic) * 1000, 1),
            exchange_delay_seconds=prepared.exchange_delay,
        )

    async def process_event(
        self, session, leader: Leader, event: LeaderActivity, prepared: PreparedCopy | None = None
    ) -> None:
        detection_lag = max(0, (event.received_at or time.time()) - event.timestamp)
        if event.trader_name and not leader.label:
            leader.label = event.trader_name[:120]
        copy_trade = CopyTrade(
            leader_id=leader.id,
            event_key=event.event_key,
            timestamp=event.timestamp,
            token_id=event.token_id,
            condition_id=event.condition_id,
            side=event.side,
            leader_size=event.size,
            leader_price=event.price,
            status="detected",
        )
        try:
            async with session.begin_nested():
                session.add(copy_trade)
                await session.flush()
        except IntegrityError:
            return

        leader_pos = await session.scalar(
            select(LeaderPosition).where(
                LeaderPosition.leader_id == leader.id,
                LeaderPosition.token_id == event.token_id,
            )
        )
        before_leader_shares = leader_pos.shares if leader_pos else Decimal(0)
        account = await get_or_create_account(session, self.settings.paper_initial_balance)
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
            await self.update_leader_position(session, leader.id, event, leader_pos)
            log.warning("copy_data_rejected", condition_id=event.condition_id, error=str(exc))
            return
        if event.side == "BUY":
            # The configured copy size is a total cash budget. Reserve room for
            # the taker fee so apply_fill cannot reject a fill after consuming
            # the whole available balance on notional alone.
            leader_notional = event.size * event.price
            buy_budget = self.calculate_smart_buy_budget(
                account, self.settings, leader_notional, prepared.leader_value, fee_rate,
                self._leader_avg_trade_size.get(leader.id),
            )
            existing = await get_position(session, event.token_id)
            existing_exposure = existing.cost_basis if existing else Decimal(0)
            if self.settings.smart_sizing_enabled and prepared.leader_value:
                cash_capacity = account.paper_balance / (Decimal(1) + fee_rate)
                base_capacity = min(cash_capacity, account.max_trade_size, account.trade_size)
            else:
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
                    reason=copy_trade.skip_reason,
                    detection_lag_seconds=round(detection_lag, 3),
                    paper_balance=str(account.paper_balance),
                    leader_notional=str(leader_notional),
                    calculated_budget=str(buy_budget),
                )
                await self.update_leader_position(session, leader.id, event, leader_pos)
                return
        else:
            position = await get_position(session, event.token_id)
            if not position or position.shares <= 0 or before_leader_shares <= 0:
                copy_trade.status = "skipped"
                copy_trade.skip_reason = "no_position_to_sell"
                self.record_rejection(session, copy_trade, event, copy_trade.skip_reason)
                await self.update_leader_position(session, leader.id, event, leader_pos)
                return
            sell_ratio = min(Decimal(1), event.size / before_leader_shares)
            target_shares = position.shares * sell_ratio

        await self.update_leader_position(session, leader.id, event, leader_pos)
        if target_shares <= 0:
            copy_trade.status = "skipped"
            copy_trade.skip_reason = "invalid_size_or_price"
            self.record_rejection(session, copy_trade, event, copy_trade.skip_reason)
            return
        try:
            if prepared.book_error:
                raise prepared.book_error
            book = prepared.book
            if book is None or time.monotonic() - prepared.book_at > 0.25:
                # Do not execute against a snapshot that aged in the ledger queue.
                book = await self.client.get_book(event.token_id)
            if event.side == "BUY":
                buy_budget = self.ensure_book_minimum_budget(
                    buy_budget,
                    own_capacity,
                    book,
                    event.price,
                    account.slippage_bps,
                )
                if book.asks:
                    target_shares = buy_budget / book.asks[0][0]
                fill = execute_buy_fak_by_budget(
                    book,
                    buy_budget,
                    fee_rate,
                    reference_price=event.price,
                    slippage_bps=account.slippage_bps,
                )
            else:
                fill = execute_fak(
                    book,
                    event.side,
                    target_shares,
                    fee_rate,
                    reference_price=event.price,
                    slippage_bps=account.slippage_bps,
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
            session.info.setdefault("notifications", []).append(
                self.build_buy_notification(leader, event, fill)
            )

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
            async with self._ledger_lock, SessionLocal() as session:
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
            async with self._ledger_lock, SessionLocal() as session:
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
                book = await self.client.get_book(token_id)
                fill = execute_fak(book, "SELL", requested_shares, prepared.fee_rate)
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
            async with self._ledger_lock, SessionLocal() as session:
                # Shares/cost/balance may have changed while resolution was fetched.
                position = await session.get(Position, snapshot.id)
                if not position or position.token_id != snapshot.token_id or position.shares <= 0:
                    return
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
                proceeds = position.shares * payout
                pnl = proceeds - position.cost_basis
                account.paper_balance += proceeds
                account.realized_pnl += pnl
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
