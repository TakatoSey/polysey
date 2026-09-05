from __future__ import annotations

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
            # Stream currently lacks complete delta reconciliation; never
            # execute/mark from a potentially outdated snapshot.
        response = await self.http.get(
            f"{self.settings.clob_api}/book", params={"token_id": token_id}
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if str(data.get("asset_id")) != token_id:
            raise ValueError("book_token_mismatch")
        if "tick_size" not in data or "min_order_size" not in data:
            raise ValueError("book_constraints_unavailable")
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
        if book.tick_size <= 0 or book.min_order_size <= 0:
            raise ValueError("invalid_book_constraints")
        if any(
            not p.is_finite() or not s.is_finite() or not 0 < p < 1 or s <= 0
            for p, s in book.bids + book.asks
        ):
            raise ValueError("invalid_book_level")
        if self.book_stream:
            await self.book_stream.update_meta(token_id, book)
        return book

    async def get_market(self, condition_id: str) -> dict:
        """Fetch by path and verify identity: a 200 alone is never enough."""
        response = await self.http.get(f"{self.settings.clob_api}/markets/{condition_id}")
        response.raise_for_status()
        market = response.json()
        if (
            not isinstance(market, dict)
            or market.get("condition_id", "").lower() != condition_id.lower()
        ):
            raise ValueError("market_identity_mismatch")
        return market

    async def get_fee_rate(self, condition_id: str, title: str = "") -> Decimal:
        """Use exchange fee data, with a conservative fallback during outages."""
        cached = self._fee_cache.get(condition_id)
        if cached is not None and time.monotonic() - self._fee_cache_time[condition_id] < 60:
            return cached
        try:
            response = await self.http.get(f"{self.settings.clob_api}/clob-markets/{condition_id}")
            response.raise_for_status()
            info = response.json()
            if not isinstance(info, dict) or info.get("c", "").lower() != condition_id.lower():
                raise ValueError("fee_market_identity_mismatch")
            schedule = info.get("fd")
            if not isinstance(schedule, dict) or "r" not in schedule or "e" not in schedule:
                raise LookupError("fee_schedule_unavailable")
            rate = Decimal(str(schedule["r"]))
            exponent = Decimal(str(schedule["e"]))
            if not rate.is_finite() or not 0 <= rate <= 1:
                raise LookupError("invalid_fee_rate")
            # The CLOB schema explicitly allows fee-curve exponents other than
            # one. The current public fee formula uses the returned rate; an
            # exponent is metadata describing the curve and must not disable
            # execution merely because a new category uses a different value.
            if not exponent.is_finite() or exponent <= 0:
                raise LookupError("invalid_fee_exponent")
        except ValueError as exc:
            # A response for a different market is not safe to use.
            if str(exc) == "fee_market_identity_mismatch":
                raise
            rate = self._fallback_fee_rate(title)
            log.warning(
                "invalid_fee_data_using_fallback",
                condition_id=condition_id,
                fallback_rate=str(rate),
                error=str(exc),
            )
        except Exception as exc:
            rate = self._fallback_fee_rate(title)
            log.warning(
                "fee_data_unavailable_using_fallback",
                condition_id=condition_id,
                fallback_rate=str(rate),
                error=type(exc).__name__,
            )
        # A zero rate explicitly returned by the exchange is valid.
        self._fee_cache[condition_id] = rate
        self._fee_cache_time[condition_id] = time.monotonic()
        return rate

    @staticmethod
    def _fallback_fee_rate(title: str) -> Decimal:
        text = title.lower()
        if any(word in text for word in ("geopolitic", "world event")):
            return Decimal(0)
        if any(word in text for word in ("crypto", "bitcoin", "ethereum")):
            return Decimal("0.07")
        if any(
            word in text
            for word in (
                "sport",
                "nfl",
                "nba",
                "mls",
                "soccer",
                " fc ",
                "afc",
            )
        ):
            return Decimal("0.03")
        if any(word in text for word in ("politic", "election", "geopolitic")):
            return Decimal("0.04")
        return Decimal("0.05")

    async def get_resolution(
        self, condition_id: str, outcome: str, token_id: str | None = None
    ) -> Decimal | None:
        cache_key = (condition_id, token_id or outcome.casefold())
        cached = self._resolution_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 15:
            return cached[1]
        market = await self.get_market(condition_id)
        tokens = market.get("tokens") or []
        selected = [
            token
            for token in tokens
            if (
                str(token.get("token_id")) == token_id
                if token_id is not None
                else str(token.get("outcome", "")).casefold() == outcome.casefold()
            )
        ]
        if len(selected) != 1:
            raise ValueError("resolution_token_mismatch")
        if str(selected[0].get("outcome", "")).casefold() != outcome.casefold():
            raise ValueError("resolution_outcome_mismatch")
        result = None
        if market.get("closed") is True:
            winners = [token for token in tokens if token.get("winner") is True]
            if market.get("is_50_50_outcome") is True:
                # Explicit void/split payout, never inferred from a 0.5 quote.
                result = Decimal("0.5")
            elif len(winners) == 1 and all(isinstance(t.get("winner"), bool) for t in tokens):
                result = Decimal(1) if selected[0]["winner"] else Decimal(0)
        log.info(
            "resolution_checked",
            condition_id=condition_id,
            returned_condition_id=market["condition_id"],
            question=market.get("question"),
            token_id=selected[0].get("token_id"),
            outcome=outcome,
            closed=market.get("closed"),
            payout=str(result) if result is not None else None,
        )
        self._resolution_cache[cache_key] = (time.monotonic(), result)
        return result
