from __future__ import annotations

import asyncio
import hashlib
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
    trader_name: str = ""
    received_at: float = 0.0
    received_monotonic: float = 0.0
    trader_address: str = ""
    source: str = "rest"


def copy_event_key(key: str, address: str) -> str:
    """Canonical identity, including wallet, compatible with legacy stored keys."""
    if key.startswith("v2:"):
        return key
    parts = key.split(":")
    if len(parts) != 7:
        return key  # synthetic events, not network trades
    tx, stamp, condition, asset, side, size, price = parts

    def number(value):
        result = format(Decimal(value), "f")
        return result.rstrip("0").rstrip(".") if "." in result else result

    normalized = "|".join((address.lower(), tx.lower(), str(int(stamp)), condition.lower(),
                           asset, side.upper(), number(size), number(price)))
    return "v2:" + hashlib.sha256(normalized.encode()).hexdigest()


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
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        self._value_cache: dict[str, tuple[float, Decimal]] = {}
        self.book_stream = None

    async def close(self) -> None:
        if self.book_stream:
            await self.book_stream.stop()
        pending = list(self._inflight.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.http.aclose()

    async def _shared_request(self, key, request):
        """Coalesce simultaneous metadata reads without caching market status."""
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(request())
            self._inflight[key] = task

            def done(completed):
                if self._inflight.get(key) is completed:
                    self._inflight.pop(key, None)
                if not completed.cancelled():
                    completed.exception()  # retrieve errors even if all waiters cancel

            task.add_done_callback(done)
        return await asyncio.shield(task)

    async def start(self) -> None:
        # The old cache is NOT used for execution (it does not reconcile all
        # deltas). Avoid maintaining unused subscriptions and parsing traffic.
        # RTDS wallet discovery is a separate stream and remains enabled.
        pass

    async def get_activity(self, address: str, limit: int = 500) -> list[LeaderActivity]:
        response = await self.http.get(
            f"{self.settings.data_api}/activity",
            params={"user": address, "limit": limit, "type": "TRADE", "sortDirection": "DESC"},
        )
        response.raise_for_status()
        received_at, received_monotonic = time.time(), time.monotonic()
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
            address = str(item.get("proxyWallet") or item.get("proxy_wallet") or "").lower()
            events.append(
                LeaderActivity(
                    event_key=copy_event_key(key, address) if address else key,
                    timestamp=int(item.get("timestamp") or 0),
                    condition_id=item.get("conditionId") or "",
                    token_id=str(item.get("asset")),
                    side=str(item.get("side")).upper(),
                    size=Decimal(str(item.get("size") or "0")),
                    price=Decimal(str(item.get("price") or "0")),
                    title=item.get("title") or item.get("slug") or "Unknown market",
                    outcome=item.get("outcome") or "",
                    slug=item.get("slug") or "",
                    trader_name=(item.get("name") or item.get("pseudonym") or "").strip(),
                    received_at=received_at,
                    received_monotonic=received_monotonic,
                    trader_address=address,
                )
            )
        return sorted(events, key=lambda event: (event.timestamp, event.event_key))

    async def get_user_position_value(self, address: str) -> Decimal:
        """Current marked value used as a public, conservative leader-capital proxy."""
        cached = self._value_cache.get(address.lower())
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1]
        response = await self.http.get(
            f"{self.settings.data_api}/value", params={"user": address}
        )
        response.raise_for_status()
        raw = response.json()
        rows = raw if isinstance(raw, list) else [raw]
        value = Decimal(0)
        for row in rows:
            if isinstance(row, dict) and str(row.get("user", address)).lower() == address.lower():
                value = max(value, Decimal(str(row.get("value") or 0)))
        if value <= 0:
            raise ValueError("leader_position_value_unavailable")
        self._value_cache[address.lower()] = (time.monotonic(), value)
        return value

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
        return await self._shared_request(
            ("market", condition_id), lambda: self._get_market(condition_id)
        )

    async def _get_market(self, condition_id: str) -> dict:
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
        return await self._shared_request(
            ("fee", condition_id), lambda: self._get_fee_rate(condition_id, title)
        )

    async def _get_fee_rate(self, condition_id: str, title: str = "") -> Decimal:
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
