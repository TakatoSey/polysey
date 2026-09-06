from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from .config import Settings
from .db import SessionLocal
from .engine import CopyEngine
from .models import CopyTrade, Leader, PaperOrder, Position, RiskRule
from .repository import add_leader, get_leaders, get_or_create_account, orders, positions

log = structlog.get_logger(__name__)
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
SIZING_REASONS = {
    "sizing_profile_unavailable": "собираем статистику входов трейдера; пока недостаточно данных",
    "sizing_entry_closed": "серия уже завершена; позднюю покупку не догоняем",
    "sizing_below_minimum": "добавка к серии ниже минимума; учитывается при следующей покупке в том же окне",
    "sizing_entry_budget_used": "бюджет этой серии уже использован",
    "sizing_exposure_limit": "достигнут лимит на исход с учётом комиссии",
}


class LeaderForm(StatesGroup):
    address = State()


class TelegramApp:
    """Single-message Russian Telegram control panel; trade notifications are separate."""

    def __init__(self, settings: Settings, engine: CopyEngine):
        self.settings = settings
        self.engine = engine
        self.bot = Bot(settings.telegram_bot_token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.panel_message_id: int | None = None
        self._register()

    def _allowed(self, obj: Message | CallbackQuery) -> bool:
        return bool(obj.from_user and obj.from_user.id == self.settings.telegram_allowed_user_id)

    async def _delete_input(self, message: Message) -> None:
        try:
            await message.delete()
        except Exception:
            pass

    async def _reset_panel(self, chat_id: int) -> None:
        """Move the control panel to the bottom without touching notifications."""
        if self.panel_message_id is not None:
            try:
                await self.bot.delete_message(chat_id, self.panel_message_id)
            except Exception:
                # It may already have been deleted manually or by Telegram.
                pass
        self.panel_message_id = None

    async def _edit_panel(self, text: str, reply_markup=None, chat_id: int | None = None) -> None:
        chat_id = chat_id or self.settings.telegram_allowed_user_id
        if self.panel_message_id is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=self.panel_message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
            except Exception:
                log.exception("panel_edit_failed")
        try:
            sent = await self.bot.send_message(
                chat_id, text, reply_markup=reply_markup, parse_mode="HTML"
            )
            self.panel_message_id = sent.message_id
        except Exception:
            log.exception("panel_send_failed")

    def _menu(self, paused: bool = False):
        builder = InlineKeyboardBuilder()
        for text, data in [
            ("📊 Портфель", "portfolio:0"),
            ("👥 Копирование", "leaders:0"),
            ("🧾 История", "orders:0"),
            ("⚙️ Настройки", "settings"),
            ("⏸ Приостановить" if not paused else "▶️ Возобновить", "toggle"),
            ("🔄 Обновить", "home"),
            ("❓ Помощь", "help"),
        ]:
            builder.button(text=text, callback_data=data)
        builder.adjust(2, 2, 2, 1)
        return builder.as_markup()

    def _back(self):
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="home")
        return builder.as_markup()

    async def _home(self, chat_id: int | None = None) -> None:
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            leaders = await get_leaders(session)
            open_positions = await positions(session)
            await session.commit()
        state = "⏸ приостановлено" if account.paused else "▶️ активно"
        if self.settings.smart_sizing_enabled:
            max_next_buy = min(
                account.paper_balance,
                account.max_trade_size,
                account.paper_balance * self.settings.copy_balance_pct
                * self.settings.smart_sizing_max_multiplier,
            )
        else:
            max_next_buy = min(
                account.trade_size,
                account.max_trade_size,
                account.paper_balance * self.settings.copy_balance_pct,
            )
        cash_warning = (
            "\n\n⚠️ Новые покупки сейчас невозможны: верхний предел бюджета "
            f"${max_next_buy:.2f} ниже минимума ${self.settings.min_copy_notional:.2f}."
            if not account.paused and max_next_buy < self.settings.min_copy_notional
            else ""
        )
        sizing_status = ""
        if self.settings.smart_sizing_enabled:
            ready_count = sum(
                1 for row in leaders
                if row.active and self._sizing_profile_ready(row.id)
            )
            active_count = sum(1 for row in leaders if row.active)
            sizing_status = (
                f"Размер входа: <b>адаптивный · {self.settings.copy_balance_pct * 100:.1f}% базы</b>\n"
                f"Статистика входов: {ready_count}/{active_count} готовы\n"
            )
        text = (
            "<b>POLYSEY · PAPER</b>\n\n"
            f"Свободно: <b>${account.paper_balance:.2f}</b>\n"
            f"Стартовый капитал: ${account.starting_balance:.2f}\n"
            f"Состояние копирования: {state}\n"
            f"Активных трейдеров: <b>{sum(1 for row in leaders if row.active)}</b>\n"
            f"{sizing_status}"
            f"Открытых позиций: <b>{len(open_positions)}</b>\n"
            f"Зафиксированный PNL: <b>${account.realized_pnl:+.2f}</b>\n\n"
            "Стоимость открытых позиций доступна в разделе «Портфель»."
            f"{cash_warning}"
        )
        await self._edit_panel(text, self._menu(account.paused), chat_id)

    def _sizing_profile_ready(self, leader_id: int) -> bool:
        profile = self.engine._leader_sizing_profiles.get(leader_id)
        return bool(
            profile
            and profile.reference_notional > 0
            and profile.sample_count >= self.settings.smart_sizing_min_samples
        )

    def _leader_sizing_text(self, leader_id: int) -> str:
        if not self.settings.smart_sizing_enabled:
            return "Размер входа: <b>классический режим</b>\n"
        profile = self.engine._leader_sizing_profiles.get(leader_id)
        if not self._sizing_profile_ready(leader_id):
            count = profile.sample_count if profile else 0
            return (
                "Размер входа: <b>⏳ собираем статистику</b>\n"
                f"Серий в выборке: {count}; нужно минимум {self.settings.smart_sizing_min_samples}.\n"
                "Новые BUY пока пропускаются; это не пауза копирования.\n"
            )
        refreshed = profile.refreshed_at
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
        return (
            f"Типичная серия входа: <b>${profile.reference_notional:.2f}</b>\n"
            f"Серий в выборке: {profile.sample_count} · окно {self.settings.smart_sizing_burst_seconds} с\n"
            f"Статистика: {refreshed.astimezone(UTC):%d.%m %H:%M} UTC\n"
            "<i>При 10+ сериях убираем крайние 10% с каждой стороны; это не баланс трейдера.</i>\n"
        )

    async def _leaders_panel(self, page: int = 0, chat_id: int | None = None) -> None:
        async with SessionLocal() as session:
            rows = await get_leaders(session)
        per_page = 6
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        current = rows[page * per_page : (page + 1) * per_page]
        lines = [
            f"<b>👥 Копирование</b> · {len(rows)} трейдеров",
            "Выбери трейдера для просмотра и управления.",
        ]
        builder = InlineKeyboardBuilder()
        for row in current:
            icon = "🟢" if row.active else "⚪"
            name = row.label or f"Трейдер #{row.id}"
            lines.append(f"{icon} <b>{html.escape(name)}</b>")
            builder.button(text=f"{icon} {name[:28]}", callback_data=f"leader_view:{row.id}:{page}")
            builder.button(
                text="⏸" if row.active else "▶️", callback_data=f"leader_toggle:{row.id}:{page}"
            )
        builder.button(text="➕ Добавить лидера", callback_data="leader_add")
        if page > 0:
            builder.button(text="◀️", callback_data=f"leaders:{page - 1}")
        if page + 1 < total_pages:
            builder.button(text="▶️", callback_data=f"leaders:{page + 1}")
        builder.button(text="⬅️ На главную", callback_data="home")
        builder.adjust(2)
        await self._edit_panel("\n".join(lines), builder.as_markup(), chat_id)

    async def _leader_detail(self, leader_id: int, page: int, chat_id: int) -> None:
        async with SessionLocal() as session:
            row = await session.get(Leader, leader_id)
            recent = list(
                (
                    await session.scalars(
                        select(CopyTrade)
                        .where(CopyTrade.leader_id == leader_id)
                        .order_by(CopyTrade.created_at.desc())
                        .limit(20)
                    )
                ).all()
            )
            all_trades = list(
                (
                    await session.scalars(
                        select(CopyTrade)
                        .where(CopyTrade.leader_id == leader_id)
                    )
                ).all()
            )
            trade_ids = [trade.id for trade in all_trades]
            all_orders = list(
                (
                    await session.scalars(
                        select(PaperOrder).where(PaperOrder.copy_trade_id.in_(trade_ids))
                    )
                ).all()
            ) if trade_ids else []
            token_ids = {trade.token_id for trade in all_trades}
            if token_ids:
                settled_orders = list(
                    (
                        await session.scalars(
                            select(PaperOrder).where(
                                PaperOrder.status == "settled",
                                PaperOrder.token_id.in_(token_ids),
                            )
                        )
                    ).all()
                )
                known_order_ids = {order.id for order in all_orders}
                all_orders.extend(
                    order for order in settled_orders if order.id not in known_order_ids
                )
        if not row:
            await self._leaders_panel(page, chat_id)
            return
        status = "🟢 активно" if row.active else "⚪ приостановлено"
        label = html.escape(row.label or f"Трейдер #{row.id}")
        executed = sum(1 for trade in recent if trade.status == "executed")
        rejected = sum(1 for trade in recent if trade.status in {"skipped", "failed"})
        last_result = "событий пока нет"
        if recent:
            last = recent[0]
            last_result = (
                "скопировано"
                if last.status == "executed"
                else html.escape(SIZING_REASONS.get(last.skip_reason, last.skip_reason or last.status))
            )
        realized_pnl = Decimal(0)
        open_cost = Decimal(0)
        lots: list[list[Decimal]] = []  # shares, total cost including fee
        buys = sells = 0
        for order in sorted(all_orders, key=lambda value: value.created_at or datetime.min):
            if order.status not in {"filled", "partial", "settled"} or order.filled_shares <= 0:
                continue
            notional = order.filled_shares * order.average_fill_price
            if order.side == "BUY":
                lots.append([order.filled_shares, notional + (order.fee or Decimal(0))])
                buys += 1
            elif order.side == "SELL":
                remaining = order.filled_shares
                proceeds = notional - (order.fee or Decimal(0))
                while remaining > 0 and lots:
                    lot_shares, lot_cost = lots[0]
                    consumed = min(remaining, lot_shares)
                    realized_pnl += proceeds * consumed / order.filled_shares - lot_cost * consumed / lot_shares
                    lot_shares -= consumed
                    lot_cost -= lot_cost * consumed / (lot_shares + consumed)
                    remaining -= consumed
                    if lot_shares <= Decimal("0.00000001"):
                        lots.pop(0)
                    else:
                        lots[0] = [lot_shares, lot_cost]
                sells += 1
            elif order.status == "settled":
                remaining = order.filled_shares
                proceeds = notional
                while remaining > 0 and lots:
                    lot_shares, lot_cost = lots[0]
                    consumed = min(remaining, lot_shares)
                    realized_pnl += proceeds * consumed / order.filled_shares - lot_cost * consumed / lot_shares
                    lot_shares -= consumed
                    lot_cost -= lot_cost * consumed / (lot_shares + consumed)
                    remaining -= consumed
                    if lot_shares <= Decimal("0.00000001"):
                        lots.pop(0)
                    else:
                        lots[0] = [lot_shares, lot_cost]
        open_cost = sum((cost for _, cost in lots), Decimal(0))
        text = (
            f"<b>👤 {label}</b>\n\nАдрес:\n<code>{row.address}</code>\n\n"
            f"Статус: <b>{status}</b>\nИнициализация: {'готово' if row.initialized else 'в процессе'}\n"
            f"Последние 20 событий: {executed} скопировано · {rejected} пропущено\n"
            f"Сделки: {buys} покупок · {sells} продаж\n"
            + self._leader_sizing_text(leader_id)
            + f"Реализованный PNL: <b>${realized_pnl:+.4f}</b>\n"
            f"Открытая себестоимость: <b>${open_cost:.4f}</b>\n"
            "<i>FIFO · комиссии учтены; открытые shares не считаются убытком</i>\n"
            f"Последний результат: <b>{last_result}</b>\n\n"
            "Все новые сделки этого адреса обрабатываются по общим настройкам."
        )
        builder = InlineKeyboardBuilder()
        builder.button(
            text="⏸ Приостановить" if row.active else "▶️ Возобновить",
            callback_data=f"leader_toggle:{row.id}:{page}",
        )
        builder.button(text="🗑 Удалить", callback_data=f"leader_remove:{row.id}:{page}")
        builder.button(text="⬅️ К списку", callback_data=f"leaders:{page}")
        builder.adjust(1, 1, 1)
        await self._edit_panel(text, builder.as_markup(), chat_id)

    async def _portfolio_data_v2(self):
        async with SessionLocal() as session:
            rows = await positions(session)
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
        return rows, account

    async def _position_quote(self, row) -> tuple[Decimal | None, str, str]:
        """Keep resolution, quote availability and API failures distinct."""
        try:
            payout = await self.engine.client.get_resolution(
                row.condition_id, row.outcome, row.token_id
            )
        except Exception as exc:
            log.warning("portfolio_resolution_failed", token_id=row.token_id, error=str(exc))
            status = "⚠️ Не удалось проверить результат рынка"
        else:
            if payout is not None:
                status = (
                    "✅ Победа"
                    if payout == 1
                    else "Проигрыш"
                    if payout == 0
                    else "Разделённая выплата"
                )
                return payout, status, f"Выплата ${payout:.4f}/share · ожидает зачисления"
            status = "⏳ Результат ещё не подтверждён"
        # An unavailable resolution endpoint must not hide a usable quote.
        try:
            book = await self.engine.client.get_book(row.token_id)
        except Exception as exc:
            log.warning("portfolio_book_failed", token_id=row.token_id, error=str(exc))
            return None, status, "Стакан недоступен · оценка неизвестна"
        if not book.bids:
            return None, status, "Нет заявок на покупку · оценка неизвестна"
        return (
            book.bids[0][0],
            status,
            "Оценка по лучшему bid до комиссии; весь объём может стоить меньше",
        )

    async def _portfolio_text_v2(self, page: int = 0) -> str:
        rows, account = await self._portfolio_data_v2()
        per_page = 5
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        current = rows[page * per_page : (page + 1) * per_page]
        lines = [
            "<b>📊 ПОРТФЕЛЬ · PAPER</b>",
            f"Свободно: <b>${account.paper_balance:.2f}</b>",
            f"Зафиксированный PNL: ${account.realized_pnl:+.2f}",
        ]
        if not rows:
            lines.append("\nОткрытых позиций нет.")
            return "\n".join(lines)
        for row in current:
            quote, status, note = await self._position_quote(row)
            value = row.shares * quote if quote is not None else None
            mark = f"${value:.2f}" if value is not None else "—"
            pnl = f" · PNL ${value - row.cost_basis:+.2f}" if value is not None else ""
            lines.append(
                f"\n<b>{html.escape(row.title[:95])}</b>\n{html.escape(row.outcome)} · {row.shares:.3f} shares · вход ${row.average_price:.4f}\nОценка: <b>{mark}</b>{pnl}\n{status}\n{note}"
            )
        lines.append(f"\nСтраница {page + 1}/{total_pages}. Нажмите на позицию для деталей.")
        lines.append(f"Обновлено: {datetime.now(UTC):%H:%M:%S} UTC")
        return "\n".join(lines)

    def _portfolio_keyboard_v2(self, rows, page: int = 0):
        per_page = 5
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        builder = InlineKeyboardBuilder()
        for row in rows[page * per_page : (page + 1) * per_page]:
            builder.button(text=f"📌 {row.outcome[:18]}", callback_data=f"position:{row.id}:{page}")
        if page > 0:
            builder.button(text="◀️", callback_data=f"portfolio:{page - 1}")
        if page + 1 < total_pages:
            builder.button(text="▶️", callback_data=f"portfolio:{page + 1}")
        builder.button(text="🔄 Обновить", callback_data=f"portfolio:{page}")
        builder.button(text="⬅️ На главную", callback_data="home")
        builder.adjust(1, 2, 1)
        return builder.as_markup()

    async def _position_detail_v2(self, position_id: int) -> str:
        async with SessionLocal() as session:
            row = await session.get(Position, position_id)
        if not row:
            return "<b>Позиция больше не открыта</b>\nПроверьте продажу или выплату в разделе «История»."
        quote, status, note = await self._position_quote(row)
        value = row.shares * quote if quote is not None else None
        lines = [
            f"<b>📌 {html.escape(row.title)}</b>",
            f"Исход: <b>{html.escape(row.outcome)}</b>",
            f"Количество: {row.shares:.6f} shares",
            f"Средняя себестоимость с комиссией: ${row.average_price:.6f}",
            f"Затрачено: ${row.cost_basis:.4f}",
            f"Статус: {status}",
        ]
        lines.append(
            f"Текущая оценка: ${value:.4f}" if value is not None else "Текущая оценка: неизвестна"
        )
        lines.append(note)
        lines.append(f"\nОбновлено: {datetime.now(UTC):%H:%M:%S} UTC")
        return "\n".join(lines)

    async def _portfolio_text(self) -> str:
        async with SessionLocal() as session:
            rows = await positions(session)
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
        lines = [
            "<b>📊 Портфель · PAPER</b>",
            f"Свободно: <b>${account.paper_balance:.2f}</b>",
            f"Зафиксированный PNL: ${account.realized_pnl:+.2f}",
        ]
        marked_value = Decimal(0)
        unknown = 0
        for row in rows:
            current = None
            label = "лучшая цена продажи"
            try:
                payout = await self.engine.client.get_resolution(
                    row.condition_id, row.outcome, row.token_id
                )
                if payout is not None:
                    current = payout
                    label = "выплата за share · ожидает зачисления"
                else:
                    book = await self.engine.client.get_book(row.token_id)
                    if book.bids:
                        current = book.bids[0][0]
            except Exception as exc:
                log.warning("portfolio_quote_unavailable", token_id=row.token_id, error=str(exc))
            title = html.escape(row.title[:180])
            outcome = html.escape(row.outcome)
            entry = f"\n<b>{title}</b>\n{outcome} · {row.shares:.4f} shares\nЗатрачено: ${row.cost_basis:.2f}"
            if current is None:
                unknown += 1
                entry += "\nЦена и PNL: <b>нет подтверждённых данных</b>"
            else:
                value = row.shares * current
                marked_value += value
                entry += f"\n{label}: ${current:.4f}\nОценка: ${value:.2f} · PNL ${value - row.cost_basis:+.2f}"
            lines.append(entry)
        if unknown:
            summary = f"Общая стоимость и PNL: <b>недоступны</b>\nНет цены у позиций: {unknown}"
        else:
            equity = account.paper_balance + marked_value
            summary = (
                f"Оценка позиций: ${marked_value:.2f}\n"
                f"Общая оценка: <b>${equity:.2f}</b> · PNL ${equity - account.starting_balance:+.2f}\n"
                "Оценка по лучшему bid до комиссии; продажа всего объёма может дать меньше."
            )
        lines.insert(2, summary)
        if not rows:
            lines.append("\nОткрытых позиций нет.")
        # Keep one Telegram screen under the platform text limit.
        rendered = []
        length = 0
        for line in lines:
            if length + len(line) > 3600:
                rendered.append("\nОстальные позиции скрыты: превышен размер экрана.")
                break
            rendered.append(line)
            length += len(line) + 1
        return "\n".join(rendered)

    async def _orders_text_v2(self, page: int = 0, status_filter: str = "all") -> str:
        async with SessionLocal() as session:
            rows: list[PaperOrder] = await orders(session)
            copy_trade_ids = [row.copy_trade_id for row in rows if row.copy_trade_id]
            trades = (
                list(
                    (
                        await session.scalars(
                            select(CopyTrade).where(CopyTrade.id.in_(copy_trade_ids))
                        )
                    ).all()
                )
                if copy_trade_ids
                else []
            )
        trades_by_id = {trade.id: trade for trade in trades}
        mapping = {"done": {"filled", "partial"}, "skip": {"rejected"}, "settled": {"settled"}}
        filtered = [
            r
            for r in rows
            if status_filter == "all" or r.status in mapping.get(status_filter, set())
        ]
        per_page = 8
        total_pages = max(1, (len(filtered) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        current = filtered[page * per_page : (page + 1) * per_page]
        labels = {
            "filled": "исполнен",
            "partial": "частично",
            "rejected": "пропущен",
            "settled": "выплата",
            "submitted": "ожидает",
        }
        reasons = {
            **SIZING_REASONS,
            "no_liquidity_within_slippage": "цена вышла за slippage",
            "no_liquidity": "нет ликвидности",
            "below_min_order_size": "меньше минимума",
            "below_min_copy_notional": "расчётная сумма ниже нашего минимума",
            "insufficient_balance": "недостаточно средств",
            "no_position_to_sell": "у нас нет позиции для повторения продажи",
            "invalid_size_or_price": "некорректный размер или цена",
            "market_data:market_not_accepting_orders": "рынок уже не принимает ордера",
            "market_data:unsupported_fee_exponent": "устаревший расчёт комиссии",
        }
        lines = [
            "<b>🧾 ИСТОРИЯ · PAPER</b>",
            "Сделки и пропуски копирования — без технического шума.",
        ]
        if not current:
            lines.append("\nЗаписей в этом фильтре нет.")
        for row in current:
            when = row.created_at.strftime("%d.%m %H:%M") if row.created_at else "—"
            trade = trades_by_id.get(row.copy_trade_id)
            raw_reason = row.reason or ""
            reason = reasons.get(raw_reason, raw_reason)
            if raw_reason.startswith("book_error:"):
                reason = "не удалось получить актуальный стакан"
            elif raw_reason.startswith("market_data:") and raw_reason not in reasons:
                reason = "не удалось подтвердить параметры рынка"
            header = f"\n{when} · <b>{row.side} {labels.get(row.status, row.status)}</b>"
            if row.status == "rejected":
                details = []
                if trade:
                    details.append(
                        f"Лидер: {trade.leader_size:.4f} shares @ ${trade.leader_price:.4f}"
                    )
                if row.requested_shares > 0:
                    details.append(f"Наша заявка: {row.requested_shares:.4f} shares")
                details.append(f"Причина: {html.escape(reason or 'нет исполнения')}")
                lines.append(header + "\n" + "\n".join(details))
            elif row.status == "settled":
                proceeds = row.filled_shares * row.average_fill_price
                lines.append(
                    header + f"\nЗакрыто: {row.filled_shares:.4f} shares · выплата ${proceeds:.4f}"
                )
            else:
                lines.append(
                    header
                    + f"\nИсполнено: {row.filled_shares:.4f} shares @ ${row.average_fill_price:.4f}"
                    + (f" · комиссия ${row.fee:.5f}" if row.fee else "")
                )
        lines.append(f"\nСтраница {page + 1}/{total_pages}")
        return "\n".join(lines)

    async def _orders_keyboard_v2(self, page: int = 0, status_filter: str = "all"):
        async with SessionLocal() as session:
            rows = await orders(session)
        mapping = {"done": {"filled", "partial"}, "skip": {"rejected"}, "settled": {"settled"}}
        filtered_count = sum(
            1
            for r in rows
            if status_filter == "all" or r.status in mapping.get(status_filter, set())
        )
        total_pages = max(1, (filtered_count + 7) // 8)
        builder = InlineKeyboardBuilder()
        for label, key in [
            ("Все", "all"),
            ("Исполнены", "done"),
            ("Пропуски", "skip"),
            ("Выплаты", "settled"),
        ]:
            builder.button(
                text=("· " if key == status_filter else "") + label, callback_data=f"orders:0:{key}"
            )
        if page > 0:
            builder.button(text="◀️", callback_data=f"orders:{page - 1}:{status_filter}")
        if page + 1 < total_pages:
            builder.button(text="▶️", callback_data=f"orders:{page + 1}:{status_filter}")
        builder.button(text="⬅️ На главную", callback_data="home")
        builder.adjust(2, 2, 2)
        return builder.as_markup()

    async def _settings_text_v2(self) -> str:
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
        return (
            "<b>⚙️ НАСТРОЙКИ</b>\n\n"
            + self._sizing_summary(account)
            + "\n\n"
            "<b>Исполнение и риск</b>\n"
            f"Минимум BUY: ${self.settings.min_copy_notional:.2f} · лимит на исход: ${self.settings.max_outcome_exposure:.2f}\n"
            "Также действует минимум shares из стакана.\n"
            f"Допустимое отклонение цены: <b>{account.slippage_bps / 100:.2f}%</b>\n"
            "Дневной лимит: выключен · Buy-only: выключен\n\n"
            "Подробности — кнопками ниже. Команды: /setmax, /setslippage, /risk."
        )

    def _sizing_summary(self, account) -> str:
        if not self.settings.smart_sizing_enabled:
            return (
                "<b>Размер копирования · классический</b>\n"
                f"Фиксированный предел: <b>${account.trade_size:.2f}</b> · максимум ${account.max_trade_size:.2f}\n"
                f"От свободного баланса: {self.settings.copy_balance_pct * 100:.1f}%\n"
                f"Масштаб отдельной сделки лидера: {self.settings.leader_order_scale * 100:.1f}%\n"
                "Изменить предел: /setsize 5 · максимум: /setmax 30"
            )
        base = max(Decimal(0), account.paper_balance) * self.settings.copy_balance_pct
        return (
            "<b>Размер копирования · адаптивный</b>\n"
            f"База: <b>{self.settings.copy_balance_pct * 100:.1f}% свободных денег</b> · сейчас ${base:.2f}\n"
            f"Размер серии лидера / его типичный вход · до {self.settings.smart_sizing_max_multiplier:g}× базы\n"
            "Цена хуже средней в серии → бюджет уменьшается.\n"
            f"Максимум на серию: <b>${account.max_trade_size:.2f}, включая комиссию</b>\n"
            f"Покупки одного исхода за {self.settings.smart_sizing_burst_seconds} с делят один бюджет.\n"
            "/setsize не влияет на адаптивный режим. Максимум: /setmax 30"
        )

    def _sizing_help(self) -> str:
        if not self.settings.smart_sizing_enabled:
            return (
                "<b>💵 Размер сделки · классический</b>\n"
                "Бюджет ограничен свободными деньгами, их процентом, размером сделки лидера "
                "и установленными пределами.\n\n/setsize 5 · /setmax 30"
            )
        return (
            "<b>💵 Как рассчитывается вход</b>\n\n"
            f"1. База — {self.settings.copy_balance_pct * 100:.1f}% свободных денег в начале серии.\n"
            "2. Масштаб — сумма BUY трейдера в серии / его типичная серия.\n"
            f"3. Масштаб ограничен {self.settings.smart_sizing_max_multiplier:g}×. "
            "Если наша цена выше средней цены трейдера в серии, бюджет уменьшается "
            "в отношении этих цен. Лучшая цена бюджет не увеличивает.\n"
            "4. Из целевого бюджета вычитаем уже потраченное в этой серии, включая комиссии.\n\n"
            f"Серия — трейдер + исход + фиксированное окно {self.settings.smart_sizing_burst_seconds} с "
            "по времени сделки. На границе окна близкие сделки могут разделиться. "
            "Бот не ждёт окончания окна. Мелкие добавки накапливаются только внутри него.\n\n"
            "Это модель размера позиции, не оценка вероятности победы или капитала трейдера. "
            "Slippage, доступные деньги и лимит на исход остаются обязательными.\n\n"
            "<b>/setmax 30</b> — потолок серии с комиссиями. "
            "/setsize действует только в классическом режиме. "
            "Процент базы меняется через COPY_BALANCE_PCT в .env; "
            "после изменения контейнер нужно пересоздать."
        )

    async def _orders_text(self) -> str:
        async with SessionLocal() as session:
            rows = await orders(session)
        if not rows:
            return "<b>📋 Ордера</b>\nОрдеров пока нет."
        lines = ["<b>📋 Последние paper-ордера</b>"]
        for row in rows[:15]:
            lines.append(
                f"{row.side} {row.status}: {row.filled_shares:.4f} @ ${row.average_fill_price:.4f} ({html.escape(row.reason or 'copy')})"
            )
        return "\n".join(lines)

    def _settings_keyboard_v2(self):
        builder = InlineKeyboardBuilder()
        builder.button(text="💵 Размер сделки", callback_data="settings:sizing")
        builder.button(text="📏 Лимиты", callback_data="settings:limits")
        builder.button(text="📉 Slippage", callback_data="settings:slippage")
        builder.button(text="🛡️ Stop-loss / TP", callback_data="settings:risk")
        builder.button(text="⬅️ На главную", callback_data="home")
        builder.adjust(2, 2, 1)
        return builder.as_markup()

    async def _settings_text(self) -> str:
        return await self._settings_text_v2()

    def _register(self) -> None:
        self.dp.message.register(self.start, Command("start"))
        self.dp.message.register(self.help, Command("help"))
        self.dp.message.register(self.status, Command("status"))
        self.dp.message.register(self.portfolio, Command("portfolio"))
        self.dp.message.register(self.leaders, Command("leaders"))
        self.dp.message.register(self.orders, Command("orders"))
        self.dp.message.register(self.addleader, Command("addleader"))
        self.dp.message.register(self.removeleader, Command("removeleader"))
        self.dp.message.register(self.risk, Command("risk"))
        self.dp.message.register(self.settings_cmd, Command("settings"))
        self.dp.message.register(self.setsize, Command("setsize"))
        self.dp.message.register(self.setmax, Command("setmax"))
        self.dp.message.register(self.setslippage, Command("setslippage"))
        self.dp.message.register(self.toggle, Command("pause"))
        self.dp.message.register(self.toggle, Command("resume"))
        self.dp.message.register(self.receive_leader, StateFilter(LeaderForm.address))
        self.dp.callback_query.register(self.callback)

    async def start(self, message: Message, state: FSMContext | None = None) -> None:
        if not self._allowed(message):
            return
        if state:
            await state.clear()
        await self._delete_input(message)
        await self._reset_panel(message.chat.id)
        await self._home(message.chat.id)

    async def help(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._edit_panel(
            "<b>❓ Помощь</b>\nУправляй ботом кнопками ниже. Добавление лидеров: 👥 Лидеры → ➕ Добавить лидера.\n\nКоманды /setsize, /setmax, /setslippage и /risk остаются доступны.",
            self._back(),
            message.chat.id,
        )

    async def status(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._home(message.chat.id)

    async def portfolio(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        rows, _ = await self._portfolio_data_v2()
        await self._edit_panel(
            await self._portfolio_text_v2(0), self._portfolio_keyboard_v2(rows, 0), message.chat.id
        )

    async def leaders(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._leaders_panel(chat_id=message.chat.id)

    async def orders(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._edit_panel(
            await self._orders_text_v2(0, "all"),
            await self._orders_keyboard_v2(0, "all"),
            message.chat.id,
        )

    async def settings_cmd(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._edit_panel(
            await self._settings_text_v2(), self._settings_keyboard_v2(), message.chat.id
        )

    async def addleader(self, message: Message, state: FSMContext) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        if len(parts) == 2:
            await self._delete_input(message)
            await self._save_leader(parts[1], message.chat.id)
        else:
            await state.set_state(LeaderForm.address)
            await self._delete_input(message)
            await self._edit_panel(
                "<b>➕ Добавление лидера</b>\nОтправь EVM-адрес кошелька (0x + 40 hex-символов). Это сообщение будет удалено после обработки.",
                self._back(),
                message.chat.id,
            )

    async def receive_leader(self, message: Message, state: FSMContext) -> None:
        if not self._allowed(message):
            return
        address = (message.text or "").strip()
        await self._delete_input(message)
        if not ADDRESS_RE.fullmatch(address):
            await self._edit_panel(
                "❌ Неверный адрес. Нужен формат 0x + 40 hex-символов. Попробуй ещё раз.",
                self._back(),
                message.chat.id,
            )
            return
        await state.clear()
        await self._save_leader(address, message.chat.id)

    async def _save_leader(self, address: str, chat_id: int) -> None:
        if not ADDRESS_RE.fullmatch(address):
            await self._edit_panel("❌ Неверный адрес лидера.", self._back(), chat_id)
            return
        async with SessionLocal() as session:
            await add_leader(session, address.lower())
            await session.commit()
        await self._leaders_panel(chat_id=chat_id)

    async def removeleader(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        await self._delete_input(message)
        if len(parts) != 2:
            await self._edit_panel("Формат: /removeleader 0x...", self._back(), message.chat.id)
            return
        async with SessionLocal() as session:
            row = await session.scalar(select(Leader).where(Leader.address == parts[1].lower()))
            if row:
                row.active = False
            await session.commit()
        await self._leaders_panel(chat_id=message.chat.id)

    async def _set_decimal_setting(
        self, message: Message, field: str, command: str, label: str
    ) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        await self._delete_input(message)
        try:
            value = Decimal(parts[1]) if len(parts) == 2 else Decimal(0)
        except (InvalidOperation, IndexError):
            value = Decimal(0)
        if not value.is_finite() or value <= 0:
            await self._edit_panel(f"Формат: /{command} 5", self._back(), message.chat.id)
            return
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            setattr(account, field, value)
            await session.commit()
        note = (
            "\nСохранено для классического режима; в адаптивном режиме /setsize не используется."
            if field == "trade_size" and self.settings.smart_sizing_enabled else ""
        )
        await self._edit_panel(f"✅ {label}: ${value:.2f}{note}", self._back(), message.chat.id)

    async def setsize(self, message: Message):
        await self._set_decimal_setting(message, "trade_size", "setsize", "Размер сделки")

    async def setmax(self, message: Message):
        label = "Максимум серии с комиссией" if self.settings.smart_sizing_enabled else "Максимум сделки"
        await self._set_decimal_setting(message, "max_trade_size", "setmax", label)

    async def setslippage(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        await self._delete_input(message)
        try:
            percent = Decimal(parts[1]) if len(parts) == 2 else Decimal(-1)
        except (InvalidOperation, IndexError):
            percent = Decimal(-1)
        if percent < 0 or percent > 100:
            await self._edit_panel(
                "Формат: /setslippage 5 (проценты от 0 до 100)", self._back(), message.chat.id
            )
            return
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            account.slippage_bps = int(percent * 100)
            await session.commit()
        await self._edit_panel(f"✅ Slippage: {percent:.2f}%", self._back(), message.chat.id)

    async def risk(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        await self._delete_input(message)
        if len(parts) < 2:
            await self._edit_panel(
                "Формат: /risk TOKEN sl=0.2 tp=0.25 trail=0.1", self._back(), message.chat.id
            )
            return
        values: dict[str, Decimal | None] = {"sl": None, "tp": None, "trail": None}
        try:
            for part in parts[2:]:
                key, value = part.split("=", 1)
                if key in values:
                    values[key] = None if value.lower() in {"off", "none"} else Decimal(value)
        except (ValueError, InvalidOperation):
            await self._edit_panel("Не удалось разобрать risk.", self._back(), message.chat.id)
            return
        if any(v is not None and (v <= 0 or v >= 1) for v in values.values()):
            await self._edit_panel(
                "SL/TP/trailing должны быть между 0 и 1 (0.2 = 20%).", self._back(), message.chat.id
            )
            return
        async with SessionLocal() as session:
            rule = await session.scalar(select(RiskRule).where(RiskRule.token_id == parts[1]))
            if not rule:
                rule = RiskRule(token_id=parts[1])
                session.add(rule)
            rule.stop_loss_pct, rule.take_profit_pct, rule.trailing_pct = (
                values["sl"],
                values["tp"],
                values["trail"],
            )
            rule.enabled = any(v is not None for v in values.values())
            await session.commit()
        await self._edit_panel("✅ Risk-настройки сохранены.", self._back(), message.chat.id)

    async def toggle(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            account.paused = (message.text or "").startswith("/pause")
            await session.commit()
        await self._home(message.chat.id)

    async def callback(self, query: CallbackQuery) -> None:
        if not self._allowed(query):
            await query.answer()
            return
        await query.answer()
        self.panel_message_id = query.message.message_id if query.message else self.panel_message_id
        data = query.data or "home"
        chat_id = query.from_user.id
        if data == "home":
            await self.dp.fsm.get_context(self.bot, chat_id, chat_id).clear()
            await self._home(chat_id)
        elif data == "portfolio" or data.startswith("portfolio:"):
            page = int(data.split(":")[1]) if ":" in data else 0
            rows, _ = await self._portfolio_data_v2()
            await self._edit_panel(
                await self._portfolio_text_v2(page),
                self._portfolio_keyboard_v2(rows, page),
                chat_id,
            )
        elif data == "orders" or data.startswith("orders:"):
            parts = data.split(":")
            page = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            status_filter = parts[2] if len(parts) > 2 else "all"
            await self._edit_panel(
                await self._orders_text_v2(page, status_filter),
                await self._orders_keyboard_v2(page, status_filter),
                chat_id,
            )
        elif data.startswith("position:"):
            _, raw_id, raw_page = data.split(":")
            builder = InlineKeyboardBuilder()
            builder.button(text="🔄 Обновить", callback_data=data)
            builder.button(text="⬅️ К портфелю", callback_data=f"portfolio:{raw_page}")
            builder.button(text="🏠 На главную", callback_data="home")
            builder.adjust(2)
            await self._edit_panel(
                await self._position_detail_v2(int(raw_id)), builder.as_markup(), chat_id
            )
        elif data == "settings":
            await self._edit_panel(
                await self._settings_text_v2(), self._settings_keyboard_v2(), chat_id
            )
        elif data.startswith("settings:"):
            section = data.split(":", 1)[1]
            details = {
                "sizing": self._sizing_help(),
                "limits": "📏 <b>Лимиты</b>\nДневной лимит выключен. В адаптивном режиме лимиты серии и исхода учитывают комиссии. Максимум серии меняется через /setmax; он хранится в базе и не перезаписывается из .env.",
                "slippage": "📉 <b>Slippage</b>\nМаксимальное отклонение цены при копировании. Изменить: /setslippage 5",
                "risk": "🛡️ <b>Stop-loss / Take-profit</b>\nНастраиваются для конкретного token_id: /risk TOKEN sl=0.2 tp=0.25 trail=0.1",
            }
            await self._edit_panel(
                details.get(section, "Раздел не найден"), self._settings_keyboard_v2(), chat_id
            )
        elif data == "help":
            await self._edit_panel(
                "<b>❓ Помощь</b>\nИспользуй кнопки панели. Команды настройки доступны из меню.",
                self._back(),
                chat_id,
            )
        elif data == "toggle":
            async with SessionLocal() as session:
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
                account.paused = not account.paused
                await session.commit()
            await self._home(chat_id)
        elif data.startswith("leaders:"):
            await self._leaders_panel(int(data.split(":", 1)[1]), chat_id)
        elif data.startswith("leader_view:"):
            _, raw_id, raw_page = data.split(":")
            await self._leader_detail(int(raw_id), int(raw_page), chat_id)
        elif data == "leader_add":
            await self.dp.fsm.get_context(self.bot, chat_id, chat_id).set_state(LeaderForm.address)
            await self._edit_panel(
                "<b>➕ Добавление лидера</b>\nОтправь EVM-адрес кошелька (0x + 40 hex-символов).",
                self._back(),
                chat_id,
            )
        elif data.startswith("leader_remove_confirm:"):
            _, raw_id, raw_page = data.split(":")
            async with SessionLocal() as session:
                row = await session.get(Leader, int(raw_id))
                if row:
                    row.active = False
                await session.commit()
            await self._leaders_panel(int(raw_page), chat_id)
        elif data.startswith("leader_remove:"):
            _, raw_id, raw_page = data.split(":")
            builder = InlineKeyboardBuilder()
            builder.button(
                text="Да, удалить", callback_data=f"leader_remove_confirm:{raw_id}:{raw_page}"
            )
            builder.button(text="Отмена", callback_data=f"leader_view:{raw_id}:{raw_page}")
            await self._edit_panel(
                "<b>Удалить трейдера?</b>\nКопирование новых сделок будет остановлено. История и позиции сохранятся.",
                builder.as_markup(),
                chat_id,
            )
        elif data.startswith("leader_toggle:"):
            action, raw_id, raw_page = data.split(":")
            leader_id, page = int(raw_id), int(raw_page)
            async with SessionLocal() as session:
                row = await session.get(Leader, leader_id)
                if row:
                    row.active = (not row.active) if action == "leader_toggle" else False
                await session.commit()
            await self._leaders_panel(page, chat_id)

    async def notify_loop(self) -> None:
        while True:
            message = await self.engine.notifications.get()
            try:
                await self.bot.send_message(
                    self.settings.telegram_allowed_user_id,
                    message,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception:
                log.exception("telegram_notification_failed")

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)
