from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import SIZING_REASONS, TelegramApp


def panel(*, smart=True, profiles=None):
    app = object.__new__(TelegramApp)
    app.settings = SimpleNamespace(
        smart_sizing_enabled=smart,
        copy_balance_pct=Decimal("0.05"),
        leader_order_scale=Decimal("0.1"),
        smart_sizing_max_multiplier=Decimal(3),
        smart_sizing_burst_seconds=2,
        smart_sizing_min_samples=3,
        telegram_allowed_user_id=7,
    )
    app.engine = SimpleNamespace(_leader_sizing_profiles=profiles or {})
    return app


ACCOUNT = SimpleNamespace(
    paper_balance=Decimal(150), trade_size=Decimal(5), max_trade_size=Decimal(30)
)


def test_smart_settings_explain_percentage_and_do_not_present_five_dollars_as_base():
    text = panel()._sizing_summary(ACCOUNT)
    assert "5.0% свободных денег" in text
    assert "сейчас $7.50" in text
    assert "$30.00, включая комиссию" in text
    assert "$5.00" not in text
    assert "/setsize не влияет" in text


def test_classic_settings_keep_fixed_size_and_leader_scale():
    text = panel(smart=False)._sizing_summary(ACCOUNT)
    assert "классический" in text
    assert "$5.00" in text
    assert "10.0%" in text


def test_profile_warming_up_is_visible_until_minimum_samples():
    app = panel(profiles={
        1: SimpleNamespace(reference_notional=Decimal(20), sample_count=2),
    })
    assert not app._sizing_profile_ready(1)
    text = app._leader_sizing_text(1)
    assert "собираем статистику" in text
    assert "2; нужно минимум 3" in text
    assert "Новые BUY пока пропускаются" in text
    assert "собираем статистику" in app._leader_sizing_text(2)


@pytest.mark.parametrize("stamp", [
    datetime(2026, 9, 6, 12, 30),
    datetime(2026, 9, 6, 12, 30, tzinfo=UTC),
    datetime(2026, 9, 6, 15, 30, tzinfo=timezone(timedelta(hours=3))),
])
def test_profile_shows_grouped_entry_sample_count_and_utc_time(stamp):
    app = panel(profiles={
        1: SimpleNamespace(
            reference_notional=Decimal("14.60"), sample_count=42, refreshed_at=stamp,
        ),
    })
    text = app._leader_sizing_text(1)
    assert "Типичная серия входа" in text
    assert "$14.60" in text
    assert "Серий в выборке: 42" in text
    assert "06.09 12:30 UTC" in text
    assert "не баланс трейдера" in text


def test_sizing_help_discloses_bucket_boundaries_and_residual_accounting():
    text = panel()._sizing_help()
    assert "уже потраченное" in text
    assert "включая комиссии" in text
    assert "На границе окна" in text
    assert "не ждёт окончания окна" in text
    assert "Лучшая цена бюджет не увеличивает" in text


def test_new_rejections_have_human_readable_descriptions():
    assert set(SIZING_REASONS) >= {
        "sizing_profile_unavailable", "sizing_entry_closed", "sizing_below_minimum",
        "sizing_entry_budget_used", "sizing_exposure_limit", "stale_signal",
    }
    assert all("_" not in value for value in SIZING_REASONS.values())


@pytest.mark.asyncio
async def test_non_finite_maximum_is_rejected_without_database_write():
    app = panel()
    app._edit_panel = AsyncMock()
    message = SimpleNamespace(
        text="/setmax NaN", from_user=SimpleNamespace(id=7),
        chat=SimpleNamespace(id=7), delete=AsyncMock(),
    )
    await app.setmax(message)
    assert app._edit_panel.await_args.args[0] == "Формат: /setmax 5"
