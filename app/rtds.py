"""Public RTDS trade feed used as a low-latency supplement to REST polling."""
from __future__ import annotations
import asyncio, json, time
from decimal import Decimal
import structlog, websockets
from .polymarket import LeaderActivity
log = structlog.get_logger(__name__)

class RTDSTradeStream:
    URL = "wss://ws-live-data.polymarket.com"
    def __init__(self, on_trade):
        self.on_trade, self.stop_event, self.task = on_trade, asyncio.Event(), None
    async def start(self):
        if self.task is None: self.task = asyncio.create_task(self._run(), name="rtds-trade-stream")
    async def stop(self):
        self.stop_event.set()
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True); self.task=None
    async def _run(self):
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(self.URL, ping_interval=20, open_timeout=8) as ws:
                    await ws.send(json.dumps({"action":"subscribe","subscriptions":[{"topic":"activity","type":"trades"}]}))
                    log.info("rtds_trade_stream_connected")
                    async for raw in ws:
                        for item in self._messages(raw):
                            event=self._parse(item)
                            if event: await self.on_trade(event)
            except asyncio.CancelledError: raise
            except Exception as exc:
                log.warning("rtds_trade_stream_reconnecting", error=type(exc).__name__)
                try: await asyncio.wait_for(self.stop_event.wait(), timeout=1)
                except asyncio.TimeoutError: pass
    @staticmethod
    def _messages(raw):
        try: value=json.loads(raw)
        except (TypeError,json.JSONDecodeError): return []
        if isinstance(value,dict) and isinstance(value.get("data"),dict): value=value["data"]
        return value if isinstance(value,list) else [value]
    @staticmethod
    def _parse(item):
        if not isinstance(item,dict) or item.get("type") not in (None,"TRADE"): return None
        asset=item.get("asset"); condition=item.get("conditionId") or item.get("condition_id")
        side=item.get("side"); tx=item.get("transactionHash") or item.get("transaction_hash") or ""
        if not asset or not condition or not side or not tx: return None
        timestamp=int(item.get("timestamp") or time.time()); size=Decimal(str(item.get("size") or 0)); price=Decimal(str(item.get("price") or 0))
        key=f"{tx}:{timestamp}:{condition}:{asset}:{str(side).upper()}:{size}:{price}"
        event=LeaderActivity(event_key=key,timestamp=timestamp,condition_id=str(condition),token_id=str(asset),side=str(side).upper(),size=size,price=price,title=item.get("title") or item.get("slug") or "Unknown market",outcome=item.get("outcome") or "",slug=item.get("slug") or "",trader_name=(item.get("name") or item.get("pseudonym") or "").strip(),received_at=time.time(),received_monotonic=time.monotonic())
        event.trader_address=(item.get("proxyWallet") or item.get("proxy_wallet") or "").lower()
        return event
