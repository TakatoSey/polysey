from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import structlog
import websockets

from .polymarket import Book

log = structlog.get_logger(__name__)


class LiveBookCache:
    """Best-effort public CLOB stream; REST remains the authoritative fallback."""

    URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(self, rest_client):
        self.rest_client = rest_client
        self.books: dict[str, tuple[Book, float]] = {}
        self.meta: dict[str, tuple[Decimal, Decimal, bool]] = {}
        self.assets: set[str] = set()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run(), name="clob-book-stream")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def subscribe(self, asset_id: str) -> None:
        async with self._lock:
            self.assets.add(asset_id)
        self._wake.set()

    async def get(self, asset_id: str, max_age: float = 3.0) -> Book | None:
        async with self._lock:
            cached = self.books.get(asset_id)
        if cached and time.monotonic() - cached[1] <= max_age:
            return cached[0]
        return None

    async def update_meta(self, asset_id: str, book: Book) -> None:
        async with self._lock:
            self.meta[asset_id] = (book.tick_size, book.min_order_size, book.neg_risk)

    async def _run(self) -> None:
        while not self._stop.is_set():
            if not self.assets:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2)
                except TimeoutError:
                    continue
                self._wake.clear()
                continue
            try:
                async with websockets.connect(
                    self.URL, ping_interval=None, open_timeout=8
                ) as socket:
                    async with self._lock:
                        assets = list(self.assets)
                    await socket.send(json.dumps({"assets_ids": assets, "type": "market"}))
                    ping_task = asyncio.create_task(self._ping(socket))
                    try:
                        while not self._stop.is_set():
                            try:
                                raw = await asyncio.wait_for(socket.recv(), timeout=15)
                            except TimeoutError:
                                await socket.send("PING")
                            else:
                                if raw != "PONG":
                                    await self._handle_message(raw)
                            async with self._lock:
                                current_assets = list(self.assets)
                            if set(current_assets) != set(assets):
                                new_assets = list(set(current_assets) - set(assets))
                                if new_assets:
                                    await socket.send(
                                        json.dumps(
                                            {"operation": "subscribe", "assets_ids": new_assets}
                                        )
                                    )
                                assets = current_assets
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("book_stream_reconnecting", error=type(exc).__name__)
                await asyncio.sleep(2)

    async def _ping(self, socket) -> None:
        while True:
            await asyncio.sleep(10)
            await socket.send("PING")

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(payload, list):
            messages = payload
        else:
            messages = [payload]
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("event_type") != "book":
                continue
            asset_id = str(message.get("asset_id") or "")
            if not asset_id:
                continue
            bids = sorted(
                [
                    (Decimal(str(row["price"])), Decimal(str(row["size"])))
                    for row in message.get("bids", [])
                ],
                key=lambda item: item[0],
                reverse=True,
            )
            asks = sorted(
                [
                    (Decimal(str(row["price"])), Decimal(str(row["size"])))
                    for row in message.get("asks", [])
                ],
                key=lambda item: item[0],
            )
            async with self._lock:
                tick_size, min_order_size, neg_risk = self.meta.get(
                    asset_id, (Decimal("0.01"), Decimal("1"), False)
                )
            book = Book(
                bids=bids,
                asks=asks,
                tick_size=tick_size,
                min_order_size=min_order_size,
                neg_risk=neg_risk,
            )
            async with self._lock:
                self.books[asset_id] = (book, time.monotonic())
