"""Verified public RTDS envelopes; REST remains an independent recovery source."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from decimal import Decimal, InvalidOperation

import structlog
import websockets

from .polymarket import LeaderActivity
from .polymarket import copy_event_key

log = structlog.get_logger(__name__)


class RTDSTradeStream:
    URL = "wss://ws-live-data.polymarket.com"
    PING_SECONDS = 5
    HEALTH_SECONDS = 30
    RECEIVE_TIMEOUT = 15

    def __init__(self, on_trade, tracked_addresses=None):
        self.on_trade = on_trade
        self.tracked_addresses = tracked_addresses or set()
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.counters: Counter = Counter()
        self.last_trade_at: float | None = None

    async def start(self):
        if self.task is None:
            self.stop_event.clear()
            self.task = asyncio.create_task(self._run(), name="rtds-trade-stream")

    async def stop(self):
        self.stop_event.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self):
        backoff = 1
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(
                    self.URL, ping_interval=None, open_timeout=8, close_timeout=3,
                    max_queue=128, max_size=1024 * 1024,
                ) as ws:
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{"topic": "activity", "type": "trades"}],
                    }))
                    await ws.send("ping")
                    self.last_trade_at = None
                    log.info("rtds_trade_stream_connected", state="awaiting_trade_data")
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    parsed_before = self.counters["parsed"]
                    try:
                        while not self.stop_event.is_set():
                            raw = await asyncio.wait_for(ws.recv(), self.RECEIVE_TIMEOUT)
                            await self.handle_message(raw)
                            if self.counters["parsed"] > parsed_before:
                                backoff = 1
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("rtds_trade_stream_reconnecting", error=type(exc).__name__,
                            retry_seconds=backoff)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 15)

    async def _heartbeat(self, ws):
        report_at = time.monotonic()
        try:
            while True:
                await asyncio.sleep(self.PING_SECONDS)
                await ws.send("ping")  # RTDS application heartbeat, not WS control ping
                if time.monotonic() - report_at >= self.HEALTH_SECONDS:
                    age = None if self.last_trade_at is None else time.monotonic() - self.last_trade_at
                    log.info("rtds_health", **dict(self.counters),
                             state="receiving" if age is not None and age < 60 else "no_recent_trades",
                             last_trade_age_seconds=None if age is None else round(age, 1))
                    report_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            await ws.close()

    async def handle_message(self, raw):
        received_at, received_monotonic = time.time(), time.monotonic()
        self.counters["frames"] += 1
        for item in self._messages(raw):
                            event = self._parse(item, received_at, received_monotonic,
                                                self.tracked_addresses)
            if event is None:
                self.counters["invalid_trades"] += 1
                continue
            self.counters["parsed"] += 1
            if self.last_trade_at is None:
                log.info("rtds_trade_stream_ready", state="trade_payload_verified")
            self.last_trade_at = received_monotonic
            try:
                result = await self.on_trade(event)
                self.counters[result or "delivered"] += 1
            except Exception as exc:
                self.counters["callback_errors"] += 1
                log.warning("rtds_trade_callback_failed", error=type(exc).__name__)

    @staticmethod
    def _messages(raw):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        envelopes = value if isinstance(value, list) else [value]
        payloads = []
        for envelope in envelopes:
            if not isinstance(envelope, dict):
                continue
            if envelope.get("topic") != "activity" or envelope.get("type") != "trades":
                continue
            payload = envelope.get("payload")
            payloads.extend(payload if isinstance(payload, list) else [payload])
        return payloads

    @staticmethod
    def _parse(item, received_at=None, received_monotonic=None, tracked_addresses=None):
        if not isinstance(item, dict):
            return None
        try:
            asset = str(item["asset"])
            condition = str(item["conditionId"]).lower()
            address = str(item["proxyWallet"]).lower()
            tx = str(item["transactionHash"]).lower()
            side = str(item["side"]).upper()
            if tracked_addresses is not None and address not in tracked_addresses:
                return None
            timestamp_value = Decimal(str(item["timestamp"]))
            size, price = Decimal(str(item["size"])), Decimal(str(item["price"]))
            if (
                side not in ("BUY", "SELL") or not asset.isdigit() or len(asset) > 78
                or not re.fullmatch(r"0x[0-9a-f]{64}", condition)
                or not re.fullmatch(r"0x[0-9a-f]{64}", tx)
                or not re.fullmatch(r"0x[0-9a-f]{40}", address)
                or not timestamp_value.is_finite()
                or timestamp_value != timestamp_value.to_integral_value()
                or not 0 < timestamp_value < 10_000_000_000
                or not size.is_finite() or not 0 < size < Decimal("1e14")
                or not price.is_finite() or not 0 < price < 1
            ):
                return None
            timestamp = int(timestamp_value)
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return None
        return LeaderActivity(
            event_key=copy_event_key(f"{tx}:{timestamp}:{condition}:{asset}:{side}:{size}:{price}", address),
            timestamp=timestamp, condition_id=condition, token_id=asset, side=side,
            size=size, price=price,
            title=str(item.get("title") or item.get("slug") or "Unknown market"),
            outcome=str(item.get("outcome") or ""), slug=str(item.get("slug") or ""),
            trader_name=str(item.get("name") or item.get("pseudonym") or "").strip(),
            received_at=time.time() if received_at is None else received_at,
            received_monotonic=time.monotonic() if received_monotonic is None else received_monotonic,
            trader_address=address, source="rtds",
        )
