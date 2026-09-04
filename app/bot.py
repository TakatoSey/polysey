from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from .config import Settings
from .db import SessionLocal
from .engine import CopyEngine
from .models import Leader, RiskRule
from .repository import add_leader, get_leaders, get_or_create_account, orders, positions

log = structlog.get_logger(__name__)
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")


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
            ("📊 Портфель", "portfolio"),
            ("👥 Лидеры", "leaders:0"),
            ("📋 Ордера", "orders"),
            ("⚙️ Настройки", "settings"),
            ("⏸ Пауза" if not paused else "▶️ Старт", "toggle"),
            ("❓ Помощь", "help"),
        ]:
            builder.button(text=text, callback_data=data)
        builder.adjust(2, 2, 2)
        return builder.as_markup()

    def _back(self):
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="home")
        return builder.as_markup()

    async def _home(self, chat_id: int | None = None) -> None:
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            leaders = await get_leaders(session)
            await session.commit()
        state = "⏸ остановлен" if account.paused else "▶️ работает"
        text = (
            "<b>PolyCopy Paper</b>\n\n"
            f"Баланс: <b>${account.paper_balance:.2f}</b>\n"
            f"Старт: ${account.starting_balance:.2f}\n"
            f"Состояние: {state}\n"
            f"Активных лидеров: {sum(1 for row in leaders if row.active)}\n"
            f"Realized PNL: <b>${account.realized_pnl:.2f}</b>"
        )
        await self._edit_panel(text, self._menu(account.paused), chat_id)

    async def _leaders_panel(self, page: int = 0, chat_id: int | None = None) -> None:
        async with SessionLocal() as session:
            rows = await get_leaders(session)
        per_page = 6
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        current = rows[page * per_page : (page + 1) * per_page]
        lines = [
            f"<b>👥 Лидеры</b> ({len(rows)})",
            "Нажми ▶/⏸ чтобы включить/выключить копирование.",
        ]
        builder = InlineKeyboardBuilder()
        for row in current:
            icon = "🟢" if row.active else "⚪"
            short = f"{row.address[:8]}…{row.address[-6:]}"
            lines.append(f"{icon} <code>{row.address}</code>")
            builder.button(text=f"{icon} {short}", callback_data=f"leader_toggle:{row.id}:{page}")
            builder.button(text="🗑 Удалить", callback_data=f"leader_remove:{row.id}:{page}")
        builder.button(text="➕ Добавить лидера", callback_data="leader_add")
        if page > 0:
            builder.button(text="◀️", callback_data=f"leaders:{page - 1}")
        if page + 1 < total_pages:
            builder.button(text="▶️", callback_data=f"leaders:{page + 1}")
        builder.button(text="⬅️ Назад", callback_data="home")
        builder.adjust(2)
        await self._edit_panel("\n".join(lines), builder.as_markup(), chat_id)

    async def _portfolio_text(self) -> str:
        async with SessionLocal() as session:
            rows = await positions(session)
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
        if not rows:
            return f"<b>📊 Портфель</b>\nБаланс: <b>${account.paper_balance:.2f}</b>\nОткрытых позиций нет."
        marked_value = Decimal(0)
        unrealized = Decimal(0)
        lines = ["<b>📊 Портфель</b>", f"Баланс: <b>${account.paper_balance:.2f}</b>"]
        for row in rows:
            current = row.average_price
            try:
                book = await self.engine.client.get_book(row.token_id)
                if book.bids:
                    current = book.bids[0][0]
            except Exception:
                pass
            value = row.shares * current
            pnl = value - row.cost_basis
            marked_value += value
            unrealized += pnl
            lines.append(
                f"• {html.escape(row.title)}\n  {html.escape(row.outcome)}: {row.shares:.4f} шт.\n"
                f"  вход ${row.average_price:.4f} → выход ${current:.4f}\n  PNL: <b>${pnl:.4f}</b>"
            )
        lines.insert(
            2,
            f"Mark-to-market: <b>${marked_value:.2f}</b>\nUnrealized PNL: <b>${unrealized:.2f}</b>\nRealized PNL: <b>${account.realized_pnl:.2f}</b>",
        )
        return "\n".join(lines)

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

    async def _settings_text(self) -> str:
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
        return (
            "<b>⚙️ Настройки</b>\n"
            f"Размер сделки: ${account.trade_size:.2f}\n"
            f"Максимум сделки: ${account.max_trade_size:.2f}\n"
            f"Slippage: {account.slippage_bps / 100:.2f}%\n"
            "Дневной лимит: нет\nЛидеры: без пользовательского лимита\n\n"
            "Команды: /setsize, /setmax, /setslippage, /risk TOKEN sl=0.2 tp=0.25 trail=0.1"
        )

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
        await self._edit_panel(await self._portfolio_text(), self._back(), message.chat.id)

    async def leaders(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._leaders_panel(chat_id=message.chat.id)

    async def orders(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._edit_panel(await self._orders_text(), self._back(), message.chat.id)

    async def settings_cmd(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self._delete_input(message)
        await self._edit_panel(await self._settings_text(), self._back(), message.chat.id)

    async def addleader(self, message: Message, state: FSMContext) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        if len(parts) == 2:
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
        if value <= 0:
            await self._edit_panel(f"Формат: /{command} 5", self._back(), message.chat.id)
            return
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            setattr(account, field, value)
            await session.commit()
        await self._edit_panel(f"✅ {label}: ${value:.2f}", self._back(), message.chat.id)

    async def setsize(self, message: Message):
        await self._set_decimal_setting(message, "trade_size", "setsize", "Размер сделки")

    async def setmax(self, message: Message):
        await self._set_decimal_setting(message, "max_trade_size", "setmax", "Максимум сделки")

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
            await self._home(chat_id)
        elif data == "portfolio":
            await self._edit_panel(await self._portfolio_text(), self._back(), chat_id)
        elif data == "orders":
            await self._edit_panel(await self._orders_text(), self._back(), chat_id)
        elif data == "settings":
            await self._edit_panel(await self._settings_text(), self._back(), chat_id)
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
        elif data == "leader_add":
            await self.dp.fsm.get_context(self.bot, chat_id, chat_id).set_state(LeaderForm.address)
            await self._edit_panel(
                "<b>➕ Добавление лидера</b>\nОтправь EVM-адрес кошелька (0x + 40 hex-символов).",
                self._back(),
                chat_id,
            )
        elif data.startswith("leader_toggle:") or data.startswith("leader_remove:"):
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
                await self.bot.send_message(self.settings.telegram_allowed_user_id, message)
            except Exception:
                log.exception("telegram_notification_failed")

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)
