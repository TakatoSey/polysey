from __future__ import annotations

import asyncio
import contextlib
import html
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select

from .accounting import inventory
from .config import Settings
from .db import SessionLocal
from .engine import CopyEngine
from .formatting import percent, plain
from .links import market_link
from .models import (
    BuyIntent,
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
    SourceObservation,
    SourceReceipt,
    utc_now,
)
from .price_limits import DEFAULT_RANGE, MAX_BUY_PRICE, MIN_BUY_PRICE, leader_price_range
from .repository import (
    add_leader,
    get_execution_policy,
    get_leaders,
    get_or_create_account,
    orders_page,
    positions,
    remove_leader,
)
from .sizing import MAX_LEADER_PERCENT, leader_percent

log = structlog.get_logger(__name__)
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")


def signed_money(value: Decimal) -> str:
    return f"+${value:.2f}" if value >= 0 else f"-${abs(value):.2f}"


SIZING_REASONS = {
    # The range is per leader now, so these must not name one set of bounds.
    "leader_price_out_of_range": "цена лидера вне диапазона трейдера",
    "buy_price_out_of_range": "цена в стакане вне диапазона трейдера",
    "sizing_profile_unavailable": "сбор статистики трейдера",
    "sizing_entry_closed": "серия закрыта",
    "sizing_below_minimum": "минимум рынка выше нашего размера",
    "sizing_entry_budget_used": "бюджет серии израсходован",
    "sizing_exposure_limit": "лимит на исход",
    "stale_signal": "устаревший сигнал",
    "entry_price_drop": "цена упала после входа лидера",
    "buy_superseded_by_sell": "лидер уже продал",
    "invalid_signal_timestamp": "некорректное время сигнала",
    "exit_pending": "ожидается выход из позиции",
    "out_of_order_exit": "продажа вне очереди",
    "ambiguous_inventory": "нужна сверка истории",
    "exit_market_data_unavailable": "нет данных рынка",
    "exit_book_expired": "стакан устарел",
}

# Everything a copy can be rejected for, in one table shared by the history and
# statistics screens.
SKIP_REASONS = {
    **SIZING_REASONS,
    "no_liquidity_within_slippage": "цена вне slippage",
    "no_liquidity": "нет ликвидности",
    "below_min_order_size": "меньше минимума биржи",
    "below_min_copy_notional": "ниже нашего минимума",
    "insufficient_balance": "недостаточно средств",
    "no_position_to_sell": "нет позиции для продажи",
    "invalid_size_or_price": "некорректный размер или цена",
    "market_data:market_not_accepting_orders": "рынок закрыт для ордеров",
    "market_data:unsupported_fee_exponent": "устаревший расчёт комиссии",
}


class LeaderForm(StatesGroup):
    address = State()
    fixed_size = State()
    fixed_percent = State()
    price_range = State()


class TelegramApp:
    """Single-message Russian Telegram control panel; trade notifications are separate."""

    HELP_TEXT = (
        "<b>❓ Помощь</b>\n\n"
        "Управление — кнопками панели.\n\n"
        "<b>Команды</b>\n"
        "/setmax 30 — максимум серии\n"
        "/setslippage 5 — отклонение цены в центах\n"
        "/setsize 5 — размер сделки (классический режим)\n"
        "/risk TOKEN sl=0.2 tp=0.25 trail=0.1\n"
        "/addbalance 50 — пополнить paper-баланс\n"
        "/reset — стереть сделки и начать тест заново\n"
        "/pause · /resume"
    )

    def __init__(self, settings: Settings, engine: CopyEngine):
        self.settings = settings
        self.engine = engine
        self.bot = Bot(settings.telegram_bot_token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.panel_message_id: int | None = None
        self._slugs: dict[str, tuple[str, str]] = {}
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
            ("📈 Статистика", "stats"),
            ("⚙️ Настройки", "settings"),
            ("⏸ Приостановить" if not paused else "▶️ Возобновить", "toggle"),
            ("🔄 Обновить", "home"),
            ("❓ Помощь", "help"),
        ]:
            builder.button(text=text, callback_data=data)
        builder.adjust(2, 2, 2, 2)
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
                account.paper_balance
                * self.settings.copy_balance_pct
                * self.settings.smart_sizing_max_multiplier,
            )
        else:
            max_next_buy = min(
                account.trade_size,
                account.max_trade_size,
                account.paper_balance * self.settings.copy_balance_pct,
            )
        fixed_candidates = [
            min(row.fixed_trade_size, account.max_trade_size, account.paper_balance)
            for row in leaders
            if row.active and row.fixed_trade_size is not None
        ]
        # A percent-of-leader budget has no ceiling of its own: what limits the
        # next buy is our series maximum and the cash actually available.
        percent_candidates = [
            min(account.max_trade_size, account.paper_balance)
            for row in leaders
            if row.active and row.fixed_trade_percent is not None
        ]
        max_next_buy = max([max_next_buy, *fixed_candidates, *percent_candidates])
        cash_warning = (
            f"\n\n⚠️ Бюджет ${max_next_buy:.2f} ниже минимума "
            f"${self.settings.min_copy_notional:.2f} — покупки недоступны."
            if not account.paused and max_next_buy < self.settings.min_copy_notional
            else ""
        )
        active_count = sum(1 for row in leaders if row.active)
        sizing_status = ""
        if self.settings.smart_sizing_enabled:
            ready_count = sum(
                1
                for row in leaders
                if row.active
                and (
                    row.fixed_trade_size is not None
                    or row.fixed_trade_percent is not None
                    or self._sizing_profile_ready(row.id)
                )
            )
            sizing_status = (
                f"Режим: адаптивный · {self.settings.copy_balance_pct * 100:.1f}% базы\n"
                f"Статистика: {ready_count}/{active_count}\n"
            )
        text = (
            "<b>POLYSEY</b> · paper\n\n"
            f"Баланс: <b>${account.paper_balance:.2f}</b> · старт ${account.starting_balance:.2f}\n"
            f"PNL: <b>{signed_money(account.realized_pnl)}</b>\n"
            f"Позиции: <b>{len(open_positions)}</b>\n"
            f"Трейдеры: <b>{active_count}</b> из {len(leaders)}\n"
            f"{sizing_status}"
            f"Статус: {state}"
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

    def _leader_sizing_text(
        self,
        leader_id: int,
        fixed_size: Decimal | None = None,
        fixed_percent: Decimal | None = None,
    ) -> str:
        profile = self.engine._leader_sizing_profiles.get(leader_id)
        if fixed_size is not None or fixed_percent is not None:
            return (
                f"Типичная серия: ${profile.reference_notional:.2f} · "
                f"{profile.sample_count} в выборке\n"
                if self._sizing_profile_ready(leader_id)
                else ""
            )
        if not self.settings.smart_sizing_enabled:
            return "Режим: классический\n"
        if not self._sizing_profile_ready(leader_id):
            count = profile.sample_count if profile else 0
            return (
                f"Статистика: <b>⏳ {count} из "
                f"{self.settings.smart_sizing_min_samples} серий</b> · BUY пропускаются\n"
            )
        refreshed = profile.refreshed_at
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
        return (
            f"Типичная серия: <b>${profile.reference_notional:.2f}</b> · "
            f"{profile.sample_count} в выборке\n"
            f"Обновлено: {refreshed.astimezone(UTC):%d.%m %H:%M} UTC\n"
        )

    async def _leaders_panel(self, page: int = 0, chat_id: int | None = None) -> None:
        async with SessionLocal() as session:
            rows = await get_leaders(session)
        per_page = 6
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        current = rows[page * per_page : (page + 1) * per_page]
        active = sum(1 for row in rows if row.active)
        lines = [f"<b>👥 Копирование</b>\n{active} из {len(rows)} активны"]
        if not rows:
            lines = ["<b>👥 Копирование</b>\nСписок пуст"]
        builder = InlineKeyboardBuilder()
        for row in current:
            icon = "🟢" if row.active else "⚪"
            name = row.label or f"{row.address[:6]}…{row.address[-4:]}"
            mode = (
                f" · ${row.fixed_trade_size:.2f} фикс."
                if row.fixed_trade_size
                else f" · {percent(row.fixed_trade_percent)}% лидера"
                if row.fixed_trade_percent
                else ""
            )
            builder.button(
                text=f"{icon} {name[:22]}{mode}", callback_data=f"leader_view:{row.id}:{page}"
            )
            builder.button(
                text="⏸" if row.active else "▶️", callback_data=f"leader_toggle:{row.id}:{page}"
            )
        builder.button(text="➕ Добавить трейдера", callback_data="leader_add")
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
            holdings, warnings = await inventory(session)
            all_orders = list(
                await session.scalars(
                    select(PaperOrder)
                    .join(CopyTrade)
                    .where(
                        CopyTrade.leader_id == leader_id,
                        PaperOrder.filled_shares > 0,
                        PaperOrder.status.in_(["filled", "partial"]),
                    )
                )
            )
        if not row:
            await self._leaders_panel(page, chat_id)
            return
        if not row.initialized:
            # Until the first poll lands, RTDS events for this leader are dropped.
            status = "⏳ инициализация"
        else:
            status = "🟢 активен" if row.active else "⚪ на паузе"
        label = html.escape(row.label or f"{row.address[:6]}…{row.address[-4:]}")
        sizing_mode = (
            f"фикс. ${row.fixed_trade_size:.2f} на серию"
            if row.fixed_trade_size is not None
            else f"{percent(row.fixed_trade_percent)}% от суммы серии лидера"
            if row.fixed_trade_percent is not None
            else "адаптивный"
        )
        executed = sum(1 for trade in recent if trade.status == "executed")
        rejected = sum(1 for trade in recent if trade.status in {"skipped", "failed"})
        last_result = "событий пока нет"
        if recent:
            last = recent[0]
            last_result = (
                "скопировано"
                if last.status == "executed"
                else html.escape(
                    SIZING_REASONS.get(last.skip_reason, last.skip_reason or last.status)
                )
            )
        owned = [h for (_, owner), h in holdings.items() if owner == leader_id]
        realized_pnl = sum((h.realized for h in owned), Decimal(0))
        open_cost = sum((h.cost for h in owned), Decimal(0))
        buys = sum(o.side == "BUY" for o in all_orders)
        sells = sum(o.side == "SELL" for o in all_orders)
        pnl_label = "нужна сверка истории" if warnings else signed_money(realized_pnl)
        price_range = leader_price_range(row)
        range_text = price_range.label + ("" if price_range.overridden else " (по умолчанию)")
        text = (
            f"<b>👤 {label}</b>\n<code>{row.address}</code>\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Размер: <b>{sizing_mode}</b>\n"
            f"Цена BUY: <b>{range_text}</b>\n"
            + self._leader_sizing_text(leader_id, row.fixed_trade_size, row.fixed_trade_percent)
            + f"PNL: <b>{pnl_label}</b> · открыто ${open_cost:.2f}\n"
            f"Сделки: {buys} BUY · {sells} SELL\n"
            f"Последние 20: {executed} скопировано · {rejected} пропущено\n"
            f"Последнее: {last_result}"
        )
        builder = InlineKeyboardBuilder()
        builder.button(
            text="⏸ Приостановить" if row.active else "▶️ Возобновить",
            callback_data=f"leader_toggle:{row.id}:{page}",
        )
        builder.button(
            text="💵 Фиксированная сумма",
            callback_data=f"leader_fixed:{row.id}:{page}",
        )
        builder.button(
            text="📊 Процент от суммы лидера",
            callback_data=f"leader_percent:{row.id}:{page}",
        )
        if row.fixed_trade_size is not None or row.fixed_trade_percent is not None:
            builder.button(
                text="🧠 Вернуть адаптивный размер",
                callback_data=f"leader_fixed_clear:{row.id}:{page}",
            )
        builder.button(text="🎯 Диапазон цены BUY", callback_data=f"leader_range:{row.id}:{page}")
        if price_range.overridden:
            builder.button(
                text=f"↩️ Вернуть {DEFAULT_RANGE.label}",
                callback_data=f"leader_range_clear:{row.id}:{page}",
            )
        builder.button(text="🗑 Удалить", callback_data=f"leader_remove:{row.id}:{page}")
        builder.button(text="⬅️ К списку", callback_data=f"leaders:{page}")
        builder.adjust(1)
        await self._edit_panel(text, builder.as_markup(), chat_id)

    def _linked_title(self, row, limit: int = 95) -> str:
        slug, event_slug = getattr(self, "_slugs", {}).get(row.token_id, ("", ""))
        return market_link(row.title[:limit], slug, event_slug)

    @staticmethod
    async def _slug_map(session, token_ids) -> dict[str, tuple[str, str]]:
        """Latest known market page per token; positions do not store slugs."""
        if not token_ids:
            return {}
        rows = await session.execute(
            select(
                SourceObservation.token_id,
                SourceObservation.slug,
                SourceObservation.event_slug,
            )
            .where(SourceObservation.token_id.in_(token_ids), SourceObservation.slug != "")
            .order_by(SourceObservation.timestamp.desc())
        )
        found: dict[str, tuple[str, str]] = {}
        for token_id, slug, event_slug in rows:
            found.setdefault(token_id, (slug, event_slug))
        return found

    async def _portfolio_data_v2(self):
        async with SessionLocal() as session:
            rows = await positions(session)
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            self._slugs = await self._slug_map(session, [row.token_id for row in rows])
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
            status = "⚠️ Unknown"
        else:
            if payout is not None:
                status = "✅ Won" if payout == 1 else "❌ Lost" if payout == 0 else "◦ Split"
                return payout, status, f"Выплата ${payout:.2f}/share"
            status = "🟢 Open"
        # An unavailable resolution endpoint must not hide a usable quote.
        try:
            book = await self.engine.client.get_book(row.token_id)
        except Exception as exc:
            log.warning("portfolio_book_failed", token_id=row.token_id, error=str(exc))
            return None, status, "Стакан недоступен"
        if not book.bids:
            try:
                last = await self.engine.client.get_last_trade_price(row.token_id)
            except Exception as exc:
                log.warning("portfolio_last_trade_failed", token_id=row.token_id, error=str(exc))
                return None, status, "Нет bid, last trade недоступен"
            if last is None or not isinstance(last, Decimal):
                # An empty book with no trades is usually a market that stopped
                # trading, not one we lost track of. Say which, rather than
                # leaving the position looking open and unpriced forever.
                if await self._market_closed(row):
                    return None, "⏳ Settling", "Рынок закрыт, итог ещё не опубликован"
                return None, status, "Нет bid и last trade"
            return last, status, "Mark по последней сделке"
        return book.bids[0][0], status, "Оценка по лучшему bid"

    def _min_order_note(self, token_id: str, quote: Decimal | None = None) -> str:
        """The exchange minimum we actually observed for this token, if any.

        It differs per market and is what blocks a small entry or strands a
        tail on exit, so it belongs on screen rather than only in the logs.
        Never invented: without an observed value the line is omitted.
        """
        limits = self.engine.client.market_limits(token_id)
        shares = getattr(limits, "min_order_size", None)
        if not isinstance(shares, Decimal) or not shares.is_finite() or shares <= 0:
            return ""
        note = f"{plain(shares)} shares"
        if isinstance(quote, Decimal) and quote.is_finite() and quote > 0:
            note += f" ≈ ${shares * quote:.2f}"
        return note

    async def _market_closed(self, row) -> bool:
        try:
            market = await self.engine.client.get_market(row.condition_id)
        except Exception as exc:
            log.warning("portfolio_market_failed", token_id=row.token_id, error=str(exc))
            return False
        return isinstance(market, dict) and market.get("closed") is True

    async def _portfolio_screen(self, page: int = 0):
        """Text and keyboard from ONE read, so item 3 and button 3 agree."""
        rows, _account = await self._portfolio_data_v2()
        return await self._portfolio_text_v2(page, rows), self._portfolio_keyboard_v2(rows, page)

    async def _portfolio_text_v2(self, page: int = 0, rows=None) -> str:
        if rows is None:
            rows, _account = await self._portfolio_data_v2()
        per_page = 5
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        lines = ["🗂️ <b>All Positions</b>"]
        if not rows:
            lines.append("\nNo open positions.")
            return "\n".join(lines)

        quote_slots = asyncio.Semaphore(8)

        async def quote_position(row):
            async with quote_slots:
                return await self._position_quote(row)

        quote_results = await asyncio.gather(*(quote_position(row) for row in rows))
        quotes = [
            (row, quote, status)
            for row, (quote, status, _note) in zip(rows, quote_results, strict=True)
        ]

        # An unpriced position must not blank the total for every other one.
        priced = [(row, quote) for row, quote, _ in quotes if quote is not None]
        total_cost = sum((row.cost_basis for row, _ in priced), Decimal(0))
        known_value = sum((row.shares * quote for row, quote in priced), Decimal(0))
        unknown_count = len(quotes) - len(priced)

        for index, (row, quote, status) in enumerate(
            quotes[page * per_page : (page + 1) * per_page],
            start=page * per_page + 1,
        ):
            value = row.shares * quote if quote is not None else None
            pnl_value = value - row.cost_basis if value is not None else None
            pnl_pct = (
                pnl_value / row.cost_basis * 100
                if pnl_value is not None and row.cost_basis
                else None
            )
            now = f"{quote * 100:.2f}¢" if quote is not None else "—"
            value_text = f"${value:.2f}" if value is not None else "—"
            pnl_text = (
                f"{signed_money(pnl_value)} ({pnl_pct:+.1f}%)" if pnl_pct is not None else "—"
            )
            lines.append(
                f"\n{index}. <b>{self._linked_title(row)}</b>\n"
                f"  ├ Position: {row.shares:.2f} {html.escape(row.outcome)}\n"
                f"  ├ Avg/Now: {row.average_price * 100:.2f}¢ → {now}\n"
                f"  ├ Cost/Value: ${row.cost_basis:.2f} → {value_text}\n"
                f"  ├ PnL: {pnl_text}\n"
                f"  ├ To Win: ${row.shares:.2f}\n"
                f"  └ Status: {status}"
            )

        if priced:
            total_pnl = known_value - total_cost
            total_pct = total_pnl / total_cost * 100 if total_cost else Decimal(0)
            icon = "📈" if total_pnl >= 0 else "📉"
            lines.append(
                f"\n<b>{icon} Total PnL: {signed_money(total_pnl)} ({total_pct:+.1f}%)</b>"
            )
        else:
            lines.append("\n<b>Total PnL: —</b>")
        if unknown_count:
            lines.append(f"без оценки: {unknown_count} из {len(quotes)}")
        if total_pages > 1:
            lines.append(f"\nPage {page + 1}/{total_pages}")
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
            if row:
                self._slugs.update(await self._slug_map(session, [row.token_id]))
        if not row:
            return "<b>Позиция закрыта</b>"
        quote, status, note = await self._position_quote(row)
        value = row.shares * quote if quote is not None else None
        pnl = value - row.cost_basis if value is not None else None
        pnl_pct = pnl / row.cost_basis * 100 if pnl is not None and row.cost_basis else None
        now = f"{quote * 100:.2f}¢" if quote is not None else "—"
        value_text = f"${value:.2f}" if value is not None else "—"
        pnl_text = f"{signed_money(pnl)} ({pnl_pct:+.1f}%)" if pnl_pct is not None else "—"
        return "\n".join(
            [
                f"📌 <b>{self._linked_title(row, 200)}</b>",
                "",
                f"Position: {row.shares:.2f} {html.escape(row.outcome)}",
                f"Avg/Now: {row.average_price * 100:.2f}¢ → {now}",
                f"Cost/Value: ${row.cost_basis:.2f} → {value_text}",
                f"PnL: {pnl_text}",
                f"To Win: ${row.shares:.2f}",
                f"Status: {status}",
                *(
                    [f"Минимум ордера: {minimum}"]
                    if (minimum := self._min_order_note(row.token_id, quote))
                    else []
                ),
                "",
                f"<i>{note} · {datetime.now(UTC):%H:%M} UTC</i>",
            ]
        )

    async def _orders_screen(self, page: int = 0, status_filter: str = "all"):
        """Text and keyboard from one read, over the whole history."""
        async with SessionLocal() as session:
            history = await orders_page(session, status_filter=status_filter, page=page)
        return (
            await self._orders_text_v2(history.page, status_filter, history),
            self._orders_keyboard_v2(history.page, status_filter, history.pages),
        )

    async def _orders_text_v2(self, page: int = 0, status_filter: str = "all", history=None) -> str:
        async with SessionLocal() as session:
            if history is None:
                history = await orders_page(session, status_filter=status_filter, page=page)
            page, total_pages, rows = history.page, history.pages, history.rows
            pending = list(
                (
                    await session.execute(
                        select(ExitIntent, Leader.label, Position.title)
                        .join(Leader, Leader.id == ExitIntent.leader_id)
                        .outerjoin(Position, Position.id == ExitIntent.position_id)
                        .where(ExitIntent.remaining > 0)
                        .limit(5)
                    )
                ).all()
            )
            # One bounded lookup; source metadata survives settlement/deletion.
            metadata = {}
            token_ids = {row.token_id for row in rows}
            if token_ids:
                latest = (
                    select(
                        SourceObservation.event_key,
                        func.row_number()
                        .over(
                            partition_by=SourceObservation.token_id,
                            order_by=(
                                SourceObservation.timestamp.desc(),
                                SourceObservation.event_key,
                            ),
                        )
                        .label("rank"),
                    )
                    .where(SourceObservation.token_id.in_(token_ids))
                    .subquery()
                )
                found = await session.scalars(
                    select(SourceObservation)
                    .join(latest, latest.c.event_key == SourceObservation.event_key)
                    .where(latest.c.rank == 1)
                )
                metadata = {o.token_id: o for o in found}
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
        current = rows
        labels = {
            "filled": "исполнен",
            "partial": "частично",
            "rejected": "пропущен",
            "settled": "выплата",
            "submitted": "ожидает",
        }
        reasons = SKIP_REASONS
        lines = ["<b>🧾 История</b>"]
        if pending:
            lines.append("\n<b>⏳ Незавершённые выходы</b>")
            for intent, name, title in pending:
                minimum = self._min_order_note(intent.token_id)
                lines.append(
                    f"{html.escape(name or 'Лидер')} · "
                    f"{html.escape((title or intent.token_id)[:65])}"
                    f"\n{intent.remaining:.2f} shares · мин. цена {intent.min_price * 100:.1f}¢"
                    + (f" · лот от {minimum}" if minimum else "")
                )
            if not self.settings.exit_retry_enabled:
                lines.append("Автоповторы выключены")
        if not current:
            lines.append("\nЗаписей нет.")
        for row in current:
            when = row.created_at.strftime("%d.%m %H:%M") if row.created_at else "—"
            trade = trades_by_id.get(row.copy_trade_id)
            raw_reason = row.reason or ""
            reason = reasons.get(raw_reason, raw_reason)
            if raw_reason.startswith("book_error:"):
                reason = "стакан недоступен"
            elif raw_reason.startswith("market_data:") and raw_reason not in reasons:
                reason = "нет параметров рынка"
            header = f"\n{when} · <b>{row.side} {labels.get(row.status, row.status)}</b>"
            market = metadata.get(row.token_id)
            if market:
                linked = market_link(market.title[:60], market.slug, market.event_slug)
                header += f"\n{linked} · {html.escape(market.outcome[:24])}"
            if row.status == "rejected":
                details = []
                if trade:
                    details.append(
                        f"Лидер: {trade.leader_size:.2f} shares @ {trade.leader_price * 100:.1f}¢"
                    )
                if row.requested_shares > 0:
                    details.append(f"Заявка: {row.requested_shares:.2f} shares")
                details.append(f"Причина: {html.escape(reason or 'нет исполнения')}")
                if trade and trade.status == "retry_pending":
                    details.append("⏳ ждём возврата цены")
                elif (
                    trade
                    and trade.status == "executed"
                    and raw_reason in CopyEngine.RETRYABLE_BUY_REASONS
                ):
                    details.append("✅ исполнен позже")
                elif trade:
                    terminal = {
                        "market_settled": "закрыто выплатой",
                        "exit_completed_in_series": "выход исполнен",
                        "position_closed": "позиция закрыта",
                        "leader_sold": "лидер продал",
                        "risk_exit": "сработало правило риска",
                    }
                    if trade.skip_reason in terminal:
                        details.append(terminal[trade.skip_reason])
                lines.append(header + "\n" + "\n".join(details))
            elif row.status == "settled":
                proceeds = row.filled_shares * row.average_fill_price
                lines.append(header + f"\n{row.filled_shares:.2f} shares · выплата ${proceeds:.2f}")
            else:
                lines.append(
                    header
                    + f"\n{row.filled_shares:.2f} shares @ {row.average_fill_price * 100:.1f}¢"
                    + (" · комиссия по оценке" if row.fee_estimated else "")
                )
        if total_pages > 1:
            lines.append(f"\nСтраница {page + 1}/{total_pages} · всего {history.total}")
        return "\n".join(lines)

    async def _stats_text(self, hours: int = 24) -> str:
        """Copy rate and why the rest was missed, so limits are tuned from data."""
        since = utc_now() - timedelta(hours=hours)
        async with SessionLocal() as session:
            rows = list(
                await session.execute(
                    select(CopyTrade.status, CopyTrade.skip_reason, func.count())
                    .where(CopyTrade.side == "BUY", CopyTrade.created_at >= since)
                    .group_by(CopyTrade.status, CopyTrade.skip_reason)
                )
            )
            fills = list(
                await session.execute(
                    select(PaperOrder.fee_estimated, func.count())
                    .where(
                        PaperOrder.status.in_(["filled", "partial"]),
                        PaperOrder.created_at >= since,
                    )
                    .group_by(PaperOrder.fee_estimated)
                )
            )
        estimated_fees = sum(count for flag, count in fills if flag)
        total_fills = sum(count for _flag, count in fills)
        executed = sum(count for status, _, count in rows if status == "executed")
        waiting = sum(count for status, _, count in rows if status in {"detected", "retry_pending"})
        missed = Counter()
        for status, reason, count in rows:
            if status not in {"executed", "detected", "retry_pending"}:
                missed[SKIP_REASONS.get(reason, reason or status)] += count
        total = executed + waiting + sum(missed.values())
        lines = [f"<b>📈 Статистика</b> · {hours} ч\n"]
        if not total:
            lines.append("Сигналов BUY не было.")
            return "\n".join(lines)
        lines.append(f"Сигналов BUY: <b>{total}</b>")
        lines.append(f"Исполнено: <b>{executed}</b> ({executed / total * 100:.0f}%)")
        if waiting:
            lines.append(f"Ждут цену: {waiting}")
        if missed:
            lines.append(f"Пропущено: <b>{sum(missed.values())}</b>\n")
            lines.append("<b>Причины</b>")
            for reason, count in missed.most_common(8):
                lines.append(f"{count} · {html.escape(str(reason))}")
        if estimated_fees:
            # Fee accuracy is not guaranteed on a fallback rate, so PNL from
            # these fills must not read as exact.
            lines.append(
                f"\n⚠️ Комиссия по оценке: {estimated_fees} из {total_fills} исполнений — "
                "расписание биржи было недоступно, точность PNL по ним не гарантирована."
            )
        return "\n".join(lines)

    def _orders_keyboard_v2(self, page: int = 0, status_filter: str = "all", total_pages: int = 1):
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
            policy = await get_execution_policy(session, self.settings)
            await session.commit()
        return (
            "<b>⚙️ Настройки</b>\n\n" + self._sizing_summary(account) + "\n\n"
            "<b>Исполнение</b>\n"
            f"Цена BUY: <b>{DEFAULT_RANGE.label}</b> · у трейдера может быть свой\n"
            f"Минимум BUY: <b>${self.settings.min_copy_notional:.2f}</b>\n"
            f"Лимит на исход: <b>${self.settings.max_outcome_exposure:.2f}</b>\n"
            f"Резерв кэша: <b>{self.settings.min_cash_reserve_pct * 100:.0f}%</b> капитала\n"
            f"Slippage: <b>{policy.slippage_price * 100:.2f}¢</b>\n"
            f"Уведомления о покупках: <b>{'включены' if account.notify_buys else 'выключены'}</b>"
        )

    def _sizing_summary(self, account) -> str:
        if not self.settings.smart_sizing_enabled:
            return (
                "<b>Размер · классический</b>\n"
                f"Размер сделки: <b>${account.trade_size:.2f}</b> · "
                f"максимум ${account.max_trade_size:.2f}\n"
                f"От баланса: {self.settings.copy_balance_pct * 100:.1f}% · "
                f"от сделки лидера: {self.settings.leader_order_scale * 100:.1f}%\n"
                "/setsize 5 · /setmax 30"
            )
        base = max(Decimal(0), account.paper_balance) * self.settings.copy_balance_pct
        return (
            "<b>Размер · адаптивный</b>\n"
            f"База: <b>{self.settings.copy_balance_pct * 100:.1f}% свободных денег</b> · "
            f"сейчас ${base:.2f}\n"
            f"Масштаб: до {plain(self.settings.smart_sizing_max_multiplier)}× базы\n"
            f"Максимум серии: <b>${account.max_trade_size:.2f}, включая комиссию</b>\n"
            f"Окно серии: {self.settings.smart_sizing_burst_seconds} с\n"
            "/setsize не влияет на адаптивный режим · /setmax 30"
        )

    def _sizing_help(self) -> str:
        if not self.settings.smart_sizing_enabled:
            return (
                "<b>💵 Размер сделки · классический</b>\n\n"
                "Бюджет = min(свободные деньги, % от баланса, доля сделки лидера, пределы)\n\n"
                "/setsize 5 · /setmax 30"
            )
        return (
            "<b>💵 Расчёт входа</b>\n\n"
            f"База — {self.settings.copy_balance_pct * 100:.1f}% свободных денег на старте серии.\n"
            f"Масштаб — (серия трейдера / его типичная серия) в степени "
            f"{plain(self.settings.sizing_conviction_power)}, до "
            f"{plain(self.settings.smart_sizing_max_multiplier)}×. Вход вдвое крупнее обычного "
            "весит больше, чем вдвое.\n"
            "Цена — если наша хуже средней цены трейдера, бюджет уменьшается. "
            "Лучшая цена бюджет не увеличивает.\n"
            f"Odds — цена контракта меняет размер на "
            f"{self.settings.sizing_odds_weight * 100:.0f}% от полного размаха: "
            "низкая уменьшает, высокая немного увеличивает.\n"
            "Из цели вычитается уже потраченное в серии, включая комиссии.\n\n"
            f"<b>Серия</b> — трейдер + исход + окно {self.settings.smart_sizing_burst_seconds} с "
            "по времени сделки. Пять покупок по $20 внутри окна — одна серия на $100. "
            "Бот не ждёт окончания окна. На границе окна близкие сделки могут разделиться.\n\n"
            "Типичная серия — по последним 500 записям за 7 дней; "
            "при 10+ сериях крайние 10% отбрасываются. Это не баланс трейдера.\n\n"
            "Фиксированный бюджет серии задаётся в карточке трейдера. "
            "Slippage, свободные деньги и лимит на исход действуют всегда."
        )

    def _settings_keyboard_v2(self, notify_buys: bool = True):
        builder = InlineKeyboardBuilder()
        builder.button(text="💵 Размер сделки", callback_data="settings:sizing")
        builder.button(text="📏 Лимиты", callback_data="settings:limits")
        builder.button(text="📉 Slippage", callback_data="settings:slippage")
        builder.button(text="🛡️ Stop-loss / TP", callback_data="settings:risk")
        builder.button(
            text="🔕 Не уведомлять о покупках" if notify_buys else "🔔 Уведомлять о покупках",
            callback_data="notify_buys_toggle",
        )
        builder.button(text="🧪 Сброс базы", callback_data="reset_prompt")
        builder.button(text="⬅️ На главную", callback_data="home")
        builder.adjust(2, 2, 1, 1, 1)
        return builder.as_markup()

    async def _settings_screen(self):
        """Text and keyboard from one read, so the button matches the state."""
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            notify_buys = account.notify_buys
            await session.commit()
        return await self._settings_text_v2(), self._settings_keyboard_v2(notify_buys)

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
        self.dp.message.register(self.addbalance, Command("addbalance"))
        self.dp.message.register(self.reset, Command("reset"))
        self.dp.message.register(self.toggle, Command("pause"))
        self.dp.message.register(self.toggle, Command("resume"))
        self.dp.message.register(self.receive_leader, StateFilter(LeaderForm.address))
        self.dp.message.register(self.receive_leader_fixed, StateFilter(LeaderForm.fixed_size))
        self.dp.message.register(self.receive_leader_percent, StateFilter(LeaderForm.fixed_percent))
        self.dp.message.register(self.receive_leader_range, StateFilter(LeaderForm.price_range))
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
        await self._edit_panel(self.HELP_TEXT, self._back(), message.chat.id)

    async def status(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._home(message.chat.id)

    async def portfolio(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        text, keyboard = await self._portfolio_screen(0)
        await self._edit_panel(text, keyboard, message.chat.id)

    async def leaders(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._leaders_panel(chat_id=message.chat.id)

    async def orders(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        text, keyboard = await self._orders_screen(0, "all")
        await self._edit_panel(text, keyboard, message.chat.id)

    async def settings_cmd(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        text, keyboard = await self._settings_screen()
        await self._edit_panel(text, keyboard, message.chat.id)

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
                "<b>➕ Новый трейдер</b>\nОтправьте адрес кошелька: 0x + 40 hex-символов.",
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
                "❌ Неверный адрес. Нужен формат 0x + 40 hex-символов.",
                self._back(),
                message.chat.id,
            )
            return
        await state.clear()
        await self._save_leader(address, message.chat.id)

    async def receive_leader_fixed(self, message: Message, state: FSMContext) -> None:
        if not self._allowed(message):
            return
        data = await state.get_data()
        leader_id, page = data.get("leader_id"), data.get("page", 0)
        await self._delete_input(message)
        try:
            value = Decimal((message.text or "").strip().replace(",", "."))
        except InvalidOperation:
            value = Decimal(0)
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            leader = await session.get(Leader, leader_id) if leader_id else None
            if (
                not leader
                or not value.is_finite()
                or value < self.settings.min_copy_notional
                or value > account.max_trade_size
            ):
                builder = InlineKeyboardBuilder()
                if leader_id:
                    builder.button(text="⬅️ Назад", callback_data=f"leader_view:{leader_id}:{page}")
                await self._edit_panel(
                    f"Введите сумму от ${self.settings.min_copy_notional:.2f} "
                    f"до ${account.max_trade_size:.2f}.",
                    builder.as_markup(),
                    message.chat.id,
                )
                return
            leader.fixed_trade_size = value
            leader.fixed_trade_percent = None
            await session.commit()
        await state.clear()
        await self._leader_detail(leader_id, page, message.chat.id)

    async def receive_leader_percent(self, message: Message, state: FSMContext) -> None:
        if not self._allowed(message):
            return
        data = await state.get_data()
        leader_id, page = data.get("leader_id"), data.get("page", 0)
        await self._delete_input(message)
        try:
            value = Decimal((message.text or "").strip().replace(",", "."))
        except InvalidOperation:
            value = Decimal(0)
        async with SessionLocal() as session:
            leader = await session.get(Leader, leader_id) if leader_id else None
            if not leader or leader_percent(value) is None:
                builder = InlineKeyboardBuilder()
                if leader_id:
                    builder.button(text="⬅️ Назад", callback_data=f"leader_view:{leader_id}:{page}")
                await self._edit_panel(
                    f"Введите процент от 0 до {plain(MAX_LEADER_PERCENT)}, например "
                    "<code>50</code> — половина суммы лидера, <code>200</code> — вдвое больше.",
                    builder.as_markup(),
                    message.chat.id,
                )
                return
            leader.fixed_trade_percent = value
            leader.fixed_trade_size = None
            await session.commit()
        await state.clear()
        await self._leader_detail(leader_id, page, message.chat.id)

    @staticmethod
    def parse_price_range(text: str) -> tuple[Decimal, Decimal] | None:
        """Two prices in cents: "0 100", "2-98", "0,5 99,5". None if unusable."""
        found = re.findall(r"\d+(?:[.,]\d+)?", text or "")
        if len(found) != 2:
            return None
        try:
            values = [Decimal(item.replace(",", ".")) / 100 for item in found]
        except InvalidOperation:
            return None
        low, high = values
        if not low.is_finite() or not high.is_finite():
            return None
        # 0-100c inclusive; the range must be a range, and a cent has at most
        # two decimals of its own so a typo cannot store an unreachable bound.
        if not Decimal(0) <= low < high <= Decimal(1):
            return None
        if any(value != value.quantize(Decimal("0.0001")) for value in values):
            return None
        return low, high

    async def receive_leader_range(self, message: Message, state: FSMContext) -> None:
        if not self._allowed(message):
            return
        data = await state.get_data()
        leader_id, page = data.get("leader_id"), data.get("page", 0)
        await self._delete_input(message)
        bounds = self.parse_price_range(message.text or "")
        async with SessionLocal() as session:
            leader = await session.get(Leader, leader_id) if leader_id else None
            if not leader or bounds is None:
                builder = InlineKeyboardBuilder()
                if leader_id:
                    builder.button(text="⬅️ Назад", callback_data=f"leader_view:{leader_id}:{page}")
                await self._edit_panel(
                    "Введите два значения в центах: нижнее и верхнее, "
                    "например <code>0 100</code> или <code>2-98</code>.",
                    builder.as_markup(),
                    message.chat.id,
                )
                return
            low, high = bounds
            default = (low, high) == (MIN_BUY_PRICE, MAX_BUY_PRICE)
            # Storing NULL keeps the leader on the default range even if that
            # default later changes, which is what "по умолчанию" should mean.
            leader.min_buy_price = None if default else low
            leader.max_buy_price = None if default else high
            await session.commit()
        await state.clear()
        await self._leader_detail(leader_id, page, message.chat.id)

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
            leader_id = row.id if row else None
            if row:
                await remove_leader(session, row)
            await session.commit()
        if leader_id is not None:
            self.engine.forget_leader(leader_id)
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
            "\n/setsize не используется в адаптивном режиме."
            if field == "trade_size" and self.settings.smart_sizing_enabled
            else ""
        )
        await self._edit_panel(f"✅ {label}: ${value:.2f}{note}", self._back(), message.chat.id)

    async def setsize(self, message: Message):
        await self._set_decimal_setting(message, "trade_size", "setsize", "Размер сделки")

    async def setmax(self, message: Message):
        label = (
            "Максимум серии с комиссией"
            if self.settings.smart_sizing_enabled
            else "Максимум сделки"
        )
        await self._set_decimal_setting(message, "max_trade_size", "setmax", label)

    async def setslippage(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        await self._delete_input(message)
        try:
            cents = Decimal(parts[1]) if len(parts) == 2 else Decimal(-1)
        except (InvalidOperation, IndexError):
            cents = Decimal(-1)
        if (
            not cents.is_finite()
            or cents < 0
            or cents >= 100
            or cents != cents.quantize(Decimal("0.01"))
        ):
            await self._edit_panel(
                "Формат: /setslippage 5 (центов; 5 = $0.05)", self._back(), message.chat.id
            )
            return
        async with SessionLocal() as session:
            policy = await get_execution_policy(session, self.settings)
            policy.slippage_price = cents / 100
            await session.commit()
        await self._edit_panel(
            f"✅ Slippage: {cents:.2f}¢ (${cents / 100:.4f})", self._back(), message.chat.id
        )

    async def addbalance(self, message: Message) -> None:
        """Deposit paper cash. Starting balance moves too, so PNL stays a result."""
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        await self._delete_input(message)
        try:
            amount = Decimal(parts[1].replace(",", ".")) if len(parts) == 2 else Decimal(0)
        except (InvalidOperation, IndexError):
            amount = Decimal(0)
        if not amount.is_finite() or amount <= 0:
            await self._edit_panel("Формат: /addbalance 50", self._back(), message.chat.id)
            return
        async with self.engine._ledger_lock.hold(0), SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            account.paper_balance += amount
            account.starting_balance += amount
            balance = account.paper_balance
            await session.commit()
        await self._edit_panel(
            f"✅ Пополнено на ${amount:.2f}\nБаланс: <b>${balance:.2f}</b>",
            self._back(),
            message.chat.id,
        )

    async def reset(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._reset_prompt(message.chat.id)

    async def _reset_prompt(self, chat_id: int) -> None:
        builder = InlineKeyboardBuilder()
        builder.button(text="Да, стереть", callback_data="reset_confirm")
        builder.button(text="Отмена", callback_data="home")
        await self._edit_panel(
            "<b>Сбросить базу?</b>\n"
            "Удалится всё торговое: сделки, ордера, позиции, серии, незавершённые "
            "выходы, правила риска и собранная статистика трейдеров.\n"
            f"Баланс станет ${self.settings.paper_initial_balance:.2f}.\n\n"
            "Останутся сами трейдеры со своими настройками — размер, процент и "
            "диапазон цены. Статистика наберётся заново при следующем опросе. "
            "Отменить нельзя.",
            builder.as_markup(),
            chat_id,
        )

    async def _reset_database(self) -> Decimal:
        """Wipe trading state for a clean test run; keep leaders and profiles."""
        async with self.engine._ledger_lock.hold(0), SessionLocal() as session:
            for model in (
                SourceReceipt,
                SourceObservation,
                SizingAudit,
                SizingEntry,
                BuyIntent,
                ExitIntent,
                LeaderPosition,
                LeaderSizingProfile,
                RiskRule,
                PaperOrder,
                Position,
                CopyTrade,
            ):
                await session.execute(delete(model))
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            account.paper_balance = self.settings.paper_initial_balance
            account.starting_balance = self.settings.paper_initial_balance
            account.realized_pnl = Decimal(0)
            await session.commit()
        # Sell barriers, poll checkpoints and cached profiles reference rows
        # that no longer exist.
        self.engine.reset_runtime_state()
        return self.settings.paper_initial_balance

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
        try:
            await self._dispatch(data, chat_id)
        except (ValueError, IndexError):
            # A keyboard from an older build can carry callback data this one no
            # longer parses. A dead button is worse than saying so.
            log.warning("callback_unparsed", data=data)
            await self._edit_panel(
                "<b>Кнопка устарела</b>\nОткройте панель заново: /start",
                self._back(),
                chat_id,
            )

    async def _dispatch(self, data: str, chat_id: int) -> None:
        if data == "home":
            await self.dp.fsm.get_context(self.bot, chat_id, chat_id).clear()
            await self._home(chat_id)
        elif data == "portfolio" or data.startswith("portfolio:"):
            page = int(data.split(":")[1]) if ":" in data else 0
            text, keyboard = await self._portfolio_screen(page)
            await self._edit_panel(text, keyboard, chat_id)
        elif data == "orders" or data.startswith("orders:"):
            parts = data.split(":")
            page = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            status_filter = parts[2] if len(parts) > 2 else "all"
            text, keyboard = await self._orders_screen(page, status_filter)
            await self._edit_panel(text, keyboard, chat_id)
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
            text, keyboard = await self._settings_screen()
            await self._edit_panel(text, keyboard, chat_id)
        elif data == "notify_buys_toggle":
            async with SessionLocal() as session:
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
                account.notify_buys = not account.notify_buys
                await session.commit()
            text, keyboard = await self._settings_screen()
            await self._edit_panel(text, keyboard, chat_id)
        elif data.startswith("settings:"):
            section = data.split(":", 1)[1]
            details = {
                "sizing": self._sizing_help(),
                "limits": (
                    "<b>📏 Лимиты</b>\n\n"
                    "Лимиты серии и исхода учитывают комиссии.\n"
                    "Максимум серии хранится в базе и не перезаписывается из .env.\n\n"
                    "/setmax 30"
                ),
                "slippage": (
                    "<b>📉 Slippage</b>\n\n"
                    "Абсолютное отклонение цены: 5¢ = $0.05.\n\n"
                    "/setslippage 5"
                ),
                "risk": (
                    "<b>🛡️ Stop-loss / Take-profit</b>\n\n"
                    "Задаются для конкретного token_id.\n\n"
                    "/risk TOKEN sl=0.2 tp=0.25 trail=0.1"
                ),
            }
            _text, keyboard = await self._settings_screen()
            await self._edit_panel(details.get(section, "Раздел не найден"), keyboard, chat_id)
        elif data == "stats":
            await self._edit_panel(await self._stats_text(), self._back(), chat_id)
        elif data == "reset_prompt":
            await self._reset_prompt(chat_id)
        elif data == "reset_confirm":
            balance = await self._reset_database()
            await self._edit_panel(
                f"✅ База очищена. Баланс: <b>${balance:.2f}</b>", self._back(), chat_id
            )
        elif data == "help":
            await self._edit_panel(self.HELP_TEXT, self._back(), chat_id)
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
            leader_id, page = int(raw_id), int(raw_page)
            await self.dp.fsm.get_context(self.bot, chat_id, chat_id).clear()
            await self._leader_detail(leader_id, page, chat_id)
        elif data == "leader_add":
            await self.dp.fsm.get_context(self.bot, chat_id, chat_id).set_state(LeaderForm.address)
            await self._edit_panel(
                "<b>➕ Новый трейдер</b>\nОтправьте адрес кошелька: 0x + 40 hex-символов.",
                self._back(),
                chat_id,
            )
        elif data.startswith("leader_range_clear:"):
            _, raw_id, raw_page = data.split(":")
            leader_id, page = int(raw_id), int(raw_page)
            async with SessionLocal() as session:
                row = await session.get(Leader, leader_id)
                if row:
                    row.min_buy_price = row.max_buy_price = None
                await session.commit()
            await self.dp.fsm.get_context(self.bot, chat_id, chat_id).clear()
            await self._leader_detail(leader_id, page, chat_id)
        elif data.startswith("leader_range:"):
            _, raw_id, raw_page = data.split(":")
            leader_id, page = int(raw_id), int(raw_page)
            context = self.dp.fsm.get_context(self.bot, chat_id, chat_id)
            await context.set_state(LeaderForm.price_range)
            await context.update_data(leader_id=leader_id, page=page)
            builder = InlineKeyboardBuilder()
            builder.button(text="⬅️ Назад", callback_data=f"leader_view:{leader_id}:{page}")
            await self._edit_panel(
                "<b>🎯 Диапазон цены BUY</b>\n"
                "Вход этого трейдера копируется только в этом коридоре цены "
                f"(по умолчанию {DEFAULT_RANGE.label}). Продажи, стопы и выплаты "
                "он не ограничивает.\n"
                "<code>0 100</code> снимает ограничение: границы 0 и 1 всё равно "
                "недостижимы — ни источник, ни стакан таких цен не дают.\n\n"
                "Введите два значения в центах, например <code>0 100</code> "
                f"или <code>{DEFAULT_RANGE.label.replace('¢', '')}</code>.",
                builder.as_markup(),
                chat_id,
            )
        elif data.startswith("leader_fixed_clear:"):
            _, raw_id, raw_page = data.split(":")
            leader_id, page = int(raw_id), int(raw_page)
            async with SessionLocal() as session:
                row = await session.get(Leader, leader_id)
                if row:
                    row.fixed_trade_size = None
                    row.fixed_trade_percent = None
                await session.commit()
            await self.dp.fsm.get_context(self.bot, chat_id, chat_id).clear()
            await self._leader_detail(leader_id, page, chat_id)
        elif data.startswith("leader_fixed:"):
            _, raw_id, raw_page = data.split(":")
            leader_id, page = int(raw_id), int(raw_page)
            context = self.dp.fsm.get_context(self.bot, chat_id, chat_id)
            await context.set_state(LeaderForm.fixed_size)
            await context.update_data(leader_id=leader_id, page=page)
            async with SessionLocal() as session:
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
            builder = InlineKeyboardBuilder()
            builder.button(text="⬅️ Назад", callback_data=f"leader_view:{leader_id}:{page}")
            await self._edit_panel(
                "<b>💵 Фиксированная сумма</b>\n"
                "Бюджет одной серии покупок, включая комиссию.\n\n"
                f"Допустимо: ${self.settings.min_copy_notional:.2f}–${account.max_trade_size:.2f}",
                builder.as_markup(),
                chat_id,
            )
        elif data.startswith("leader_percent:"):
            _, raw_id, raw_page = data.split(":")
            leader_id, page = int(raw_id), int(raw_page)
            context = self.dp.fsm.get_context(self.bot, chat_id, chat_id)
            await context.set_state(LeaderForm.fixed_percent)
            await context.update_data(leader_id=leader_id, page=page)
            builder = InlineKeyboardBuilder()
            builder.button(text="⬅️ Назад", callback_data=f"leader_view:{leader_id}:{page}")
            await self._edit_panel(
                "<b>📊 Процент от суммы лидера</b>\n"
                "Бюджет серии BUY считается от того, сколько вложил сам лидер в эту "
                "серию, включая нашу комиссию: 100% — столько же, 50% — половина, "
                "200% — вдвое больше. Цель растёт вместе с его докупками.\n"
                "Если бюджета не хватает на минимальный ордер рынка, сделка "
                "пропускается. Максимум серии, лимит на исход и резерв кэша "
                "продолжают действовать.\n\n"
                f"Введите от 0 до {plain(MAX_LEADER_PERCENT)}, например <code>50</code>.",
                builder.as_markup(),
                chat_id,
            )
        elif data.startswith("leader_remove_confirm:"):
            _, raw_id, raw_page = data.split(":")
            async with SessionLocal() as session:
                row = await session.get(Leader, int(raw_id))
                if row:
                    await remove_leader(session, row)
                await session.commit()
            self.engine.forget_leader(int(raw_id))
            await self._leaders_panel(int(raw_page), chat_id)
        elif data.startswith("leader_remove:"):
            _, raw_id, raw_page = data.split(":")
            builder = InlineKeyboardBuilder()
            builder.button(
                text="Да, удалить", callback_data=f"leader_remove_confirm:{raw_id}:{raw_page}"
            )
            builder.button(text="Отмена", callback_data=f"leader_view:{raw_id}:{raw_page}")
            await self._edit_panel(
                "<b>Удалить трейдера?</b>\n"
                "Он исчезнет из списка, копирование остановится.\n"
                "Если по нему уже есть сделки, его ордера, PNL и открытые позиции "
                "останутся в учёте — иначе история стала бы ничьей.",
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

    MESSAGE_LIMIT = 3500  # Telegram caps a message at 4096 characters.
    SEND_ATTEMPTS = 4

    def _drain_notifications(self, first: str) -> str:
        """Send one message per burst instead of one per copied trade.

        Telegram rate-limits per chat, so a burst of copies is exactly when a
        notification gets refused and lost. Joining what is already queued
        keeps the content and makes far fewer requests.
        """
        batch = [first]
        length = len(first)
        while not self.engine.notifications.empty():
            queued = self.engine.notifications.get_nowait()
            if length + len(queued) + 16 > self.MESSAGE_LIMIT:
                # Does not fit: hand it back rather than truncate a trade.
                self.engine.notifications.put_nowait(queued)
                break
            batch.append(queued)
            length += len(queued) + 16
        return "\n\n➖➖➖\n\n".join(batch)

    async def _send_notification(self, message: str) -> bool:
        """Deliver a trade notification, honouring Telegram's own backoff.

        A dropped one cannot be reconstructed from anywhere: the order history
        holds the trade, not the message.
        """
        for attempt in range(self.SEND_ATTEMPTS):
            try:
                await self.bot.send_message(
                    self.settings.telegram_allowed_user_id,
                    message,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return True
            except TelegramRetryAfter as exc:
                # Telegram states the wait; anything shorter is refused again.
                wait = min(float(exc.retry_after) + 0.5, 60.0)
                log.warning("telegram_rate_limited", retry_after=wait, attempt=attempt + 1)
            except (TelegramNetworkError, TelegramServerError) as exc:
                wait = min(2.0**attempt, 15.0)
                log.warning(
                    "telegram_send_retrying",
                    error=type(exc).__name__,
                    retry_seconds=wait,
                    attempt=attempt + 1,
                )
            except Exception:
                # A malformed or rejected message will not become valid later.
                log.exception("telegram_notification_failed")
                return False
            if attempt + 1 < self.SEND_ATTEMPTS:
                await asyncio.sleep(wait)
        log.error("telegram_notification_dropped", characters=len(message))
        return False

    async def notify_loop(self) -> None:
        while True:
            first = await self.engine.notifications.get()
            await self._send_notification(self._drain_notifications(first))

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)

    async def close(self) -> None:
        """Release the Telegram HTTP session on shutdown."""
        with contextlib.suppress(Exception):
            await self.bot.session.close()
