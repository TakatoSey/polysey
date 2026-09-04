from __future__ import annotations

import asyncio
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import SessionLocal
from .models import CopyTrade, Leader, LeaderPosition, PaperOrder, Position
from .paper import execute_buy_fak_by_budget, execute_fak, fee_rate_for_title
from .polymarket import LeaderActivity, PolymarketClient
from .repository import apply_fill, get_or_create_account, get_position, get_risk

log = structlog.get_logger(__name__)


class CopyEngine:
    def __init__(self, settings: Settings, client: PolymarketClient):
        self.settings = settings
        self.client = client
        self.stop_event = asyncio.Event()
        self.notifications: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)

    async def notify(self, message: str) -> None:
        try:
            self.notifications.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("notification_queue_full")

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.poll_once()
                await self.monitor_risk_once()
                await self.settle_once()
            except Exception:
                log.exception("engine_iteration_failed")
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self.stop_event.set()

    async def poll_once(self) -> None:
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
            if account.paused:
                return
            leaders = list(
                (await session.scalars(select(Leader).where(Leader.active.is_(True)))).all()
            )

        for leader in leaders:
            try:
                activities = await self.client.get_activity(leader.address)
            except Exception:
                log.exception("leader_activity_failed", leader=leader.address)
                continue
            if not activities:
                continue
            async with SessionLocal() as session:
                db_leader = await session.scalar(select(Leader).where(Leader.id == leader.id))
                if not db_leader:
                    continue
                if not db_leader.initialized:
                    db_leader.last_timestamp = max(event.timestamp for event in activities)
                    db_leader.initialized = True
                    await session.commit()
                    log.info(
                        "leader_initialized",
                        leader=leader.address,
                        last_timestamp=db_leader.last_timestamp,
                    )
                    continue
                new_events = [
                    event for event in activities if event.timestamp >= db_leader.last_timestamp
                ]
                for event in new_events:
                    if event.timestamp == db_leader.last_timestamp:
                        exists = await session.scalar(
                            select(CopyTrade.id).where(CopyTrade.event_key == event.event_key)
                        )
                        if exists:
                            continue
                    await self.process_event(session, db_leader, event)
                    db_leader.last_timestamp = max(db_leader.last_timestamp, event.timestamp)
                await session.commit()

    async def process_event(self, session, leader: Leader, event: LeaderActivity) -> None:
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
        session.add(copy_trade)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return

        leader_pos = await session.scalar(
            select(LeaderPosition).where(
                LeaderPosition.leader_id == leader.id,
                LeaderPosition.token_id == event.token_id,
            )
        )
        before_leader_shares = leader_pos.shares if leader_pos else Decimal(0)
        account = await get_or_create_account(session, self.settings.paper_initial_balance)
        fee_rate = await self.client.get_fee_rate(event.condition_id, event.title)
        if event.side == "BUY":
            buy_budget = min(account.max_trade_size, account.trade_size, account.paper_balance)
            target_shares = buy_budget / event.price if event.price else Decimal(0)
        else:
            position = await get_position(session, event.token_id)
            if not position or position.shares <= 0 or before_leader_shares <= 0:
                copy_trade.status = "skipped"
                copy_trade.skip_reason = "no_position_to_sell"
                await self.update_leader_position(session, leader.id, event, leader_pos)
                await self.notify(f"⏭️ Продажа пропущена\n{event.title}\nПричина: нет нашей позиции")
                return
            sell_ratio = min(Decimal(1), event.size / before_leader_shares)
            target_shares = position.shares * sell_ratio

        await self.update_leader_position(session, leader.id, event, leader_pos)
        if target_shares <= 0:
            copy_trade.status = "skipped"
            copy_trade.skip_reason = "invalid_size_or_price"
            return
        await asyncio.sleep(self.settings.copy_latency_seconds)
        try:
            book = await self.client.get_book(event.token_id)
            if event.side == "BUY":
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
            copy_trade.skip_reason = f"book_error:{type(exc).__name__}"
            await self.notify(f"❌ Ошибка книги заявок\n{event.title}\n{type(exc).__name__}")
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
            await self.notify(f"⏭️ Нет исполнения\n{event.title}\nПричина: {fill.reason}")
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
            await self.notify(f"⏭️ Сделка пропущена\n{event.title}\nПричина: {str(exc)}")
            return
        position = await session.scalar(select(Position).where(Position.token_id == event.token_id))
        if position:
            position.fee_rate = fee_rate
        copy_trade.status = "executed"
        await self.notify(
            f"✅ Скопировано {event.side}\n{event.title}\n"
            f"Исполнено: {fill.shares:.4f} @ ${fill.average_price:.4f}\n"
            f"Комиссия: ${fill.fee:.5f}"
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
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            if account.paused:
                return
            positions = list(
                (await session.scalars(select(Position).where(Position.shares > 0))).all()
            )
            for position in positions:
                rule = await get_risk(session, position.token_id)
                if not rule or not rule.enabled:
                    continue
                try:
                    book = await self.client.get_book(position.token_id)
                except Exception:
                    continue
                if not book.bids:
                    continue
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
                    continue
                fill = execute_fak(
                    book,
                    "SELL",
                    position.shares,
                    position.fee_rate or fee_rate_for_title(position.title),
                )
                if fill.shares <= 0:
                    continue
                try:
                    await apply_fill(
                        session,
                        fill,
                        position.token_id,
                        "SELL",
                        position.title,
                        position.outcome,
                        position.condition_id,
                        account,
                    )
                except ValueError:
                    continue
                rule.enabled = False
                session.add(
                    PaperOrder(
                        token_id=position.token_id,
                        side="SELL",
                        requested_shares=position.shares,
                        filled_shares=fill.shares,
                        average_fill_price=fill.average_price,
                        fee=fill.fee,
                        status=fill.status,
                        reason=trigger,
                    )
                )
                await self.notify(
                    f"🛡️ {trigger} сработал\n{position.title}\nЦена: ${fill.average_price:.4f}"
                )
            await session.commit()

    async def settle_once(self) -> None:
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            positions = list(
                (await session.scalars(select(Position).where(Position.shares > 0))).all()
            )
            for position in positions:
                try:
                    payout = await self.client.get_resolution(
                        position.condition_id, position.outcome
                    )
                except Exception:
                    continue
                if payout is None:
                    continue
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
                        reason="resolution:won" if payout == 1 else "resolution:lost",
                    )
                )
                rule = await get_risk(session, position.token_id)
                if rule:
                    rule.enabled = False
                await self.notify(
                    f"🏁 Рынок разрешён\n{position.title}\n"
                    f"Результат: {'WIN' if payout == 1 else 'LOSS'}\nPNL: ${pnl:.4f}"
                )
                await session.delete(position)
            await session.commit()
