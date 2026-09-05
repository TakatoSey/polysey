from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import structlog

from .config import Settings

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class LeaderActivity:
    event_key: str
    timestamp: int
    condition_id: str
    token_id: str
    side: str
    size: Decimal
    price: Decimal
    title: str
    outcome: str
    slug: str


@dataclass(slots=True)
class Book:
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    tick_size: Decimal
    min_order_size: Decimal
    neg_risk: bool


class PolymarketClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._fee_cache: dict[str, Decimal] = {}
        self._fee_cache_time: dict[str, float] = {}
        self._resolution_cache: dict[tuple[str, str], tuple[float, Decimal | None]] = {}
        self.book_stream = None

    async def close(self) -> None:
        if self.book_stream:
            await self.book_stream.stop()
        await self.http.aclose()

    async def start(self) -> None:
        from .orderbook import LiveBookCache

        if not self.book_stream:
            self.book_stream = LiveBookCache(self)
            await self.book_stream.start()

    async def get_activity(self, address: str, limit: int = 500) -> list[LeaderActivity]:
        response = await self.http.get(
            f"{self.settings.data_api}/activity", params={"user": address, "limit": limit}
        )
        response.raise_for_status()
        raw = response.json()
        if isinstance(raw, dict):
            raw = raw.get("data") or raw.get("value") or []
        events: list[LeaderActivity] = []
        for item in raw:
            if item.get("type") != "TRADE" or not item.get("asset") or not item.get("side"):
                continue
            tx = item.get("transactionHash") or ""
            key = ":".join(
                str(value)
                for value in (
                    tx,
                    item.get("timestamp"),
                    item.get("conditionId"),
                    item.get("asset"),
                    item.get("side"),
                    item.get("size"),
                    item.get("price"),
                )
            )
            events.append(
                LeaderActivity(
                    event_key=key,
                    timestamp=int(item.get("timestamp") or 0),
                    condition_id=item.get("conditionId") or "",
                    token_id=str(item.get("asset")),
                    side=str(item.get("side")).upper(),
                    size=Decimal(str(item.get("size") or "0")),
                    price=Decimal(str(item.get("price") or "0")),
                    title=item.get("title") or item.get("slug") or "Unknown market",
                    outcome=item.get("outcome") or "",
                    slug=item.get("slug") or "",
                )
            )
        return sorted(events, key=lambda event: (event.timestamp, event.event_key))

    async def get_book(self, token_id: str) -> Book:
        if self.book_stream:
            await self.book_stream.subscribe(token_id)
            cached = await self.book_stream.get(token_id)
            if cached:
                return cached
        response = await self.http.get(
            f"{self.settings.clob_api}/book", params={"token_id": token_id}
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        bids = sorted(
            [
                (Decimal(str(row["price"])), Decimal(str(row["size"])))
                for row in data.get("bids", [])
            ],
            key=lambda x: x[0],
            reverse=True,
        )
        asks = sorted(
            [
                (Decimal(str(row["price"])), Decimal(str(row["size"])))
                for row in data.get("asks", [])
            ],
            key=lambda x: x[0],
        )
        book = Book(
            bids=bids,
            asks=asks,
            tick_size=Decimal(str(data.get("tick_size") or "0.01")),
            min_order_size=Decimal(str(data.get("min_order_size") or "1")),
            neg_risk=bool(data.get("neg_risk", False)),
        )
        if self.book_stream:
            await self.book_stream.update_meta(token_id, book)
        return book

    async def get_fee_rate(self, condition_id: str, title: str = "") -> Decimal:
        if (
            condition_id in self._fee_cache
            and time.monotonic() - self._fee_cache_time.get(condition_id, 0) < 300
        ):
            return self._fee_cache[condition_id]
        try:
            response = await self.http.get(
                f"{self.settings.gamma_api}/markets",
                # Gamma's REST filter uses the camelCase conditionId name.
                # Snake_case is ignored and returns an unrelated first market.
                params={"conditionId": condition_id, "limit": 1},
            )
            response.raise_for_status()
            raw = response.json()
            market = raw[0] if isinstance(raw, list) and raw else raw.get("data", [{}])[0]
            schedule = market.get("feeSchedule") or {}
            rate = (
                Decimal(str(schedule.get("rate")))
                if schedule.get("rate") is not None
                else self._fallback_fee_rate(title)
            )
        except Exception:
            rate = self._fallback_fee_rate(title)
        self._fee_cache[condition_id] = rate
        self._fee_cache_time[condition_id] = time.monotonic()
        return rate

    async def get_resolution(self, condition_id: str, outcome: str) -> Decimal | None:
        cache_key = (condition_id, outcome.lower())
        cached = self._resolution_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 15:
            return cached[1]
        response = await self.http.get(
            f"{self.settings.gamma_api}/markets",
            params={"conditionId": condition_id, "limit": 1},
        )
        response.raise_for_status()
        raw = response.json()
        market = raw[0] if isinstance(raw, list) and raw else None
        if not market:
            self._resolution_cache[cache_key] = (time.monotonic(), None)
            return None
        closed_value = market.get("closed")
        resolved_value = market.get("resolved")
        is_closed = str(closed_value).lower() in {"true", "1"} or str(resolved_value).lower() in {
            "true",
            "1",
        }
        if not is_closed:
            log.info(
                "market_not_resolved",
                condition_id=condition_id,
                closed=closed_value,
                resolved=resolved_value,
                outcome_prices=market.get("outcomePrices"),
            )
            self._resolution_cache[cache_key] = (time.monotonic(), None)
            return None
        try:
            outcomes_raw = market.get("outcomes") or []
            prices_raw = market.get("outcomePrices") or []
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            index = next(
                i for i, value in enumerate(outcomes) if str(value).lower() == outcome.lower()
            )
            payout = Decimal(str(prices[index]))
        except (ValueError, TypeError, StopIteration, IndexError, json.JSONDecodeError):
            log.warning(
                "market_resolution_unparseable",
                condition_id=condition_id,
                outcome=outcome,
                closed=closed_value,
                resolved=resolved_value,
                outcomes=market.get("outcomes"),
                outcome_prices=market.get("outcomePrices"),
            )
            self._resolution_cache[cache_key] = (time.monotonic(), None)
            return None
        result = payout if payout in {Decimal(0), Decimal(1)} else None
        self._resolution_cache[cache_key] = (time.monotonic(), result)
        return result

    @staticmethod
    def _fallback_fee_rate(title: str) -> Decimal:
        text = title.lower()
        if "crypto" in text or "bitcoin" in text or "ethereum" in text:
            return Decimal("0.07")
        if "sport" in text or "nfl" in text or "nba" in text:
            return Decimal("0.05")
        if "politic" in text or "election" in text:
            return Decimal("0.04")
        return Decimal("0.05")
