from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import structlog
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from .config import Settings
from .db import SessionLocal
from .engine import CopyEngine
from .models import Leader, RiskRule
from .repository import add_leader, get_leaders, get_or_create_account, orders, positions

log = structlog.get_logger(__name__)


class TelegramApp:
    def __init__(self, settings: Settings, engine: CopyEngine):
        self.settings = settings
        self.engine = engine
        self.bot = Bot(settings.telegram_bot_token)
        self.dp = Dispatcher()
        self._register()

    def _allowed(self, message: Message) -> bool:
        return bool(
            message.from_user and message.from_user.id == self.settings.telegram_allowed_user_id
        )

    def _menu(self):
        builder = InlineKeyboardBuilder()
        for text, data in [
            ("📊 Портфель", "portfolio"),
            ("👥 Лидеры", "leaders"),
            ("📋 Ордера", "orders"),
            ("⏯ Пауза/старт", "toggle"),
        ]:
            builder.button(text=text, callback_data=data)
        builder.adjust(2, 2)
        return builder.as_markup()

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
        self.dp.callback_query.register(self.callback)

    async def start(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self.status(message)

    async def help(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await message.answer(
            "Команды:\n"
            "/status — баланс и состояние\n"
            "/leaders — список лидеров\n"
            "/addleader 0x... — добавить лидера\n"
            "/removeleader 0x... — выключить лидера\n"
            "/portfolio — позиции и PNL\n"
            "/orders — последние paper-ордера\n"
            "/risk TOKEN sl=0.2 tp=0.25 trail=0.1 — risk-exit\n"
            "/settings — текущие параметры\n"
            "/setsize 5 — размер копии в USD\n"
            "/setmax 10 — максимум одной сделки\n"
            "/setslippage 5 — допуск slippage в процентах\n"
            "/pause и /resume — аварийная остановка/запуск\n\n"
            "Paper-режим использует live-книгу заявок, но не отправляет реальные ордера.",
        )

    async def settings_cmd(self, message: Message) -> None:
        if not self._allowed(message):
            return
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
        await message.answer(
            "<b>Настройки paper-копирования</b>\n"
            f"Размер сделки: ${account.trade_size:.2f}\n"
            f"Максимум сделки: ${account.max_trade_size:.2f}\n"
            f"Slippage: {account.slippage_bps / 100:.2f}%\n"
            "Дневной лимит: отсутствует\nЛидеры: без пользовательского лимита",
            parse_mode="HTML",
        )

    async def _set_decimal_setting(
        self, message: Message, field: str, command: str, label: str
    ) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer(f"Формат: /{command} 5")
            return
        try:
            value = Decimal(parts[1])
        except InvalidOperation:
            await message.answer("Нужно указать число.")
            return
        if value <= 0:
            await message.answer("Значение должно быть больше нуля.")
            return
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            setattr(account, field, value)
            await session.commit()
        await message.answer(f"✅ {label}: ${value:.2f}")

    async def setsize(self, message: Message) -> None:
        await self._set_decimal_setting(message, "trade_size", "setsize", "Размер сделки")

    async def setmax(self, message: Message) -> None:
        await self._set_decimal_setting(message, "max_trade_size", "setmax", "Максимум сделки")

    async def setslippage(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Формат: /setslippage 5\n5 означает 5% допустимого отклонения.")
            return
        try:
            percent = Decimal(parts[1])
        except InvalidOperation:
            await message.answer("Нужно указать число.")
            return
        if percent < 0 or percent > 100:
            await message.answer("Slippage должен быть от 0 до 100%.")
            return
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            account.slippage_bps = int(percent * 100)
            await session.commit()
        await message.answer(f"✅ Slippage: {percent:.2f}%")

    async def status(self, message: Message) -> None:
        if not self._allowed(message):
            return
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            leaders = await get_leaders(session)
            await session.commit()
        state = "⏸ остановлен" if account.paused else "▶️ работает"
        await message.answer(
            f"<b>PolyCopy Paper</b>\n\nБаланс: <b>${account.paper_balance:.2f}</b>\n"
            f"Старт: ${account.starting_balance:.2f}\nСостояние: {state}\nЛидеров: {sum(1 for x in leaders if x.active)}",
            reply_markup=self._menu(),
            parse_mode="HTML",
        )

    async def leaders(self, message: Message) -> None:
        if not self._allowed(message):
            return
        async with SessionLocal() as session:
            rows = await get_leaders(session)
        if not rows:
            await message.answer("Лидеров пока нет. Добавь: /addleader 0x...")
            return
        lines = ["<b>Лидеры</b>"]
        for row in rows:
            icon = "🟢" if row.active else "⚪"
            lines.append(f"{icon} <code>{row.address}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def addleader(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not re.fullmatch(r"0x[a-fA-F0-9]{40}", parts[1]):
            await message.answer("Формат: /addleader 0x1234... (40 hex-символов)")
            return
        async with SessionLocal() as session:
            row = await add_leader(session, parts[1])
            await session.commit()
        await message.answer(
            f"✅ Лидер добавлен: <code>{row.address}</code>\nНачальные позиции не копируются.",
            parse_mode="HTML",
        )

    async def removeleader(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Формат: /removeleader 0x...")
            return
        async with SessionLocal() as session:
            row = await session.scalar(select(Leader).where(Leader.address == parts[1].lower()))
            if not row:
                await message.answer("Лидер не найден")
                return
            row.active = False
            await session.commit()
        await message.answer("⏸ Лидер отключён. История сохранена.")

    async def portfolio(self, message: Message) -> None:
        if not self._allowed(message):
            return
        await self.bot.send_message(
            self.settings.telegram_allowed_user_id, await self._portfolio_text(), parse_mode="HTML"
        )

    async def _portfolio_text(self) -> str:
        async with SessionLocal() as session:
            rows = await positions(session)
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            await session.commit()
        if not rows:
            return f"<b>Портфель</b>\nБаланс: <b>${account.paper_balance:.2f}</b>\nОткрытых позиций нет."
        marked_value = Decimal(0)
        unrealized = Decimal(0)
        realized = account.realized_pnl
        lines = ["<b>Портфель</b>", f"Баланс: <b>${account.paper_balance:.2f}</b>"]
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
                f"• {row.title}\n  {row.outcome}: {row.shares:.4f} шт.\n"
                f"  вход ${row.average_price:.4f} → выход ${current:.4f}\n"
                f"  unrealized: <b>${pnl:.4f}</b>\n  token: <code>{row.token_id}</code>"
            )
        lines.insert(
            2,
            f"Mark-to-market: <b>${marked_value:.2f}</b>\nUnrealized PNL: <b>${unrealized:.2f}</b>\nRealized PNL: <b>${realized:.2f}</b>",
        )
        return "\n".join(lines)

    async def orders(self, message: Message) -> None:
        if not self._allowed(message):
            return
        async with SessionLocal() as session:
            rows = await orders(session)
        if not rows:
            await message.answer("Ордеров пока нет.")
            return
        lines = ["<b>Последние paper-ордера</b>"]
        for row in rows[:15]:
            lines.append(
                f"{row.side} {row.status}: {row.filled_shares:.4f} @ ${row.average_fill_price:.4f} ({row.reason or 'copy'})"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def risk(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer(
                "Формат: /risk TOKEN sl=0.2 tp=0.25 trail=0.1\nПроценты указываются долями: 0.2 = 20%."
            )
            return
        token = parts[1]
        values: dict[str, Decimal | None] = {"sl": None, "tp": None, "trail": None}
        try:
            for part in parts[2:]:
                key, value = part.split("=", 1)
                if key in values:
                    values[key] = None if value.lower() in {"off", "none"} else Decimal(value)
        except (ValueError, InvalidOperation):
            await message.answer("Не удалось разобрать параметры risk.")
            return
        if any(value is not None and (value <= 0 or value >= 1) for value in values.values()):
            await message.answer(
                "SL/TP/trailing должны быть больше 0 и меньше 1. Например, 0.2 = 20%."
            )
            return
        async with SessionLocal() as session:
            rule = await session.scalar(select(RiskRule).where(RiskRule.token_id == token))
            if not rule:
                rule = RiskRule(token_id=token)
                session.add(rule)
            rule.stop_loss_pct, rule.take_profit_pct, rule.trailing_pct = (
                values["sl"],
                values["tp"],
                values["trail"],
            )
            rule.enabled = any(value is not None for value in values.values())
            await session.commit()
        await message.answer(
            "✅ Risk-настройки сохранены. Автоматический выход не гарантирует исполнение при отсутствии ликвидности."
        )

    async def toggle(self, message: Message) -> None:
        if not self._allowed(message):
            return
        async with SessionLocal() as session:
            account = await get_or_create_account(session, self.settings.paper_initial_balance)
            account.paused = (message.text or "").startswith("/pause")
            await session.commit()
        await message.answer(
            "⏸ Paper-копирование остановлено."
            if account.paused
            else "▶️ Paper-копирование запущено."
        )

    async def callback(self, query: CallbackQuery) -> None:
        if not query.from_user or query.from_user.id != self.settings.telegram_allowed_user_id:
            await query.answer()
            return
        await query.answer()
        chat_id = query.from_user.id
        if query.data == "portfolio":
            await self.bot.send_message(chat_id, await self._portfolio_text(), parse_mode="HTML")
        elif query.data == "leaders":
            async with SessionLocal() as session:
                rows = await get_leaders(session)
            text = (
                "Лидеров пока нет."
                if not rows
                else "<b>Лидеры</b>\n"
                + "\n".join(
                    f"{'🟢' if row.active else '⚪'} <code>{row.address}</code>" for row in rows
                )
            )
            await self.bot.send_message(chat_id, text, parse_mode="HTML")
        elif query.data == "orders":
            async with SessionLocal() as session:
                rows = await orders(session)
            text = (
                "Ордеров пока нет."
                if not rows
                else "<b>Последние ордера</b>\n"
                + "\n".join(
                    f"{row.side} {row.status}: {row.filled_shares:.4f} @ ${row.average_fill_price:.4f}"
                    for row in rows[:15]
                )
            )
            await self.bot.send_message(chat_id, text, parse_mode="HTML")
        elif query.data == "toggle":
            async with SessionLocal() as session:
                account = await get_or_create_account(session, self.settings.paper_initial_balance)
                account.paused = not account.paused
                await session.commit()
            await self.bot.send_message(
                chat_id,
                "⏸ Paper-копирование остановлено."
                if account.paused
                else "▶️ Paper-копирование запущено.",
            )

    async def notify_loop(self) -> None:
        while True:
            message = await self.engine.notifications.get()
            try:
                await self.bot.send_message(self.settings.telegram_allowed_user_id, message)
            except Exception:
                log.exception("telegram_notification_failed")

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)
