"""Real orders on the Polymarket CLOB. The exchange is the source of truth.

Nothing here simulates anything: an order is signed with the configured key and
posted, and what came back is reported as-is. When the exchange does not say how
much matched, we ask it rather than assume.

The official client is synchronous, so every call runs in a worker thread; it
keeps one HTTP/2 connection alive underneath. Order creation inside that client
fetches tick size, neg-risk and the fee rate on first use per token, so
`prewarm` loads those while the engine is already waiting on other metadata:
submitting an order is then a single request.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import structlog

from .config import Settings

log = structlog.get_logger(__name__)

ZERO = Decimal(0)
# Fills are settled in USDC with six decimals; shares carry more.
CASH = Decimal("0.000001")


class LiveTradingUnavailable(RuntimeError):
    """Live mode was requested but cannot be served."""


def build_client(settings: Settings):
    """The signing client, built from the configured wallet.

    Kept separate so the trading logic can be exercised against a stand-in
    exchange: nothing below this line knows how the client was created.
    """
    try:
        from py_clob_client.client import ClobClient
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise LiveTradingUnavailable(
            "py-clob-client is not installed: pip install '.[live]'"
        ) from exc

    client = ClobClient(
        settings.clob_api,
        chain_id=settings.polygon_chain_id,
        key=settings.polymarket_private_key,
        signature_type=settings.polymarket_signature_type,
        funder=settings.polymarket_funder,
    )
    # Level 2 credentials are derived from the key itself, so the same wallet
    # always yields the same API key without a secret to store.
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def _decimal(value, default: Decimal | None = ZERO) -> Decimal | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return number if number.is_finite() else default


@dataclass(frozen=True)
class LiveOrder:
    """One submitted order, described only by what the exchange reported."""

    submitted: bool
    order_id: str = ""
    status: str = ""
    shares: Decimal = ZERO
    notional: Decimal = ZERO
    average_price: Decimal = ZERO
    fee: Decimal = ZERO
    confirmed: bool = False  # the matched amounts came from the exchange
    error: str | None = None
    submit_ms: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LivePosition:
    token_id: str
    shares: Decimal
    average_price: Decimal
    condition_id: str = ""
    title: str = ""
    outcome: str = ""
    redeemable: bool = False


@dataclass(frozen=True)
class LiveAccount:
    """Who we are trading as, and what the exchange says we hold."""

    signer: str
    funder: str
    signature_type: int
    cash: Decimal
    allowance: Decimal


class LiveTrader:
    """Signs and posts orders, and reads cash, shares and fills back."""

    # The exchange is asked this long for the result of an order it accepted.
    CONFIRM_ATTEMPTS = 6
    CONFIRM_DELAY = 0.25

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self._account: LiveAccount | None = None
        # Tick size expires inside the client, so remember when we warmed it.
        self._prewarmed: dict[str, float] = {}
        self._order_type = settings.live_order_type.upper()

    # ------------------------------------------------------------------ setup

    async def start(self) -> LiveAccount:
        """Build the signer, derive API credentials and read the balance once."""
        problems = self.settings.live_problems()
        if problems:
            raise LiveTradingUnavailable("; ".join(problems))
        self._client = await asyncio.to_thread(build_client, self.settings)
        signer = await asyncio.to_thread(self._client.get_address)
        funder = (self.settings.polymarket_funder or signer or "").lower()
        cash, allowance = await self._collateral()
        self._account = LiveAccount(
            signer=(signer or "").lower(),
            funder=funder,
            signature_type=self.settings.polymarket_signature_type,
            cash=cash,
            allowance=allowance,
        )
        log.info(
            "live_trading_ready",
            signer=self._account.signer,
            funder=self._account.funder,
            signature_type=self._account.signature_type,
            usdc=str(cash),
            allowance=str(allowance),
            order_type=self._order_type,
            dry_run=self.settings.live_dry_run,
        )
        if allowance < cash:
            # Without an allowance the exchange cannot move the collateral, so
            # orders are accepted and then fail to settle.
            log.warning(
                "live_allowance_below_balance",
                usdc=str(cash),
                allowance=str(allowance),
                hint="approve USDC for the exchange once from the Polymarket UI",
            )
        return self._account

    @property
    def account(self) -> LiveAccount | None:
        return self._account

    async def close(self) -> None:
        self._client = None

    def _require(self):
        if self._client is None:
            raise LiveTradingUnavailable("live trading is not started")
        return self._client

    # ------------------------------------------------------------- exchange state

    async def _collateral(self) -> tuple[Decimal, Decimal]:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        raw = await asyncio.to_thread(self._require().get_balance_allowance, params)
        return self._usdc(raw, "balance"), self._usdc(raw, "allowance")

    @staticmethod
    def _usdc(raw, key: str) -> Decimal:
        """USDC is reported in its smallest unit; six decimals, not a float."""
        if not isinstance(raw, dict):
            return ZERO
        amount = _decimal(raw.get(key))
        return (amount or ZERO) / Decimal(10**6)

    async def cash(self) -> Decimal:
        """Free USDC according to the exchange."""
        balance, _allowance = await self._collateral()
        return balance

    async def collateral_by_signature_type(self) -> dict[int, str]:
        """What the exchange reports for this key under each wallet arrangement.

        Which signature type applies depends on how the Polymarket account was
        created, and a delegated session key makes that harder to tell from the
        outside. Asking all three and reporting what came back turns a guess
        into an answer; nothing here signs or sends anything.
        """
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        client = self._require()
        found: dict[int, str] = {}
        for signature_type in (0, 1, 2):
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL, signature_type=signature_type
            )
            try:
                raw = await asyncio.to_thread(client.get_balance_allowance, params)
            except Exception as exc:
                found[signature_type] = f"error: {type(exc).__name__}"
                continue
            found[signature_type] = (
                f"balance {self._usdc(raw, 'balance')}, allowance {self._usdc(raw, 'allowance')}"
            )
        return found

    async def token_shares(self, token_id: str) -> Decimal:
        """Shares of one outcome according to the exchange."""
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        raw = await asyncio.to_thread(self._require().get_balance_allowance, params)
        return self._usdc(raw, "balance")

    async def positions(self, http, data_api: str) -> list[LivePosition]:
        """Open positions as Polymarket reports them for the funding wallet."""
        owner = (self._account.funder if self._account else "") or ""
        if not owner:
            return []
        response = await http.get(
            f"{data_api}/positions", params={"user": owner, "sizeThreshold": "0.01"}
        )
        response.raise_for_status()
        rows = response.json()
        rows = rows.get("data", []) if isinstance(rows, dict) else rows
        found = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            token = str(row.get("asset") or row.get("tokenId") or "")
            shares = _decimal(row.get("size"))
            if not token or shares is None or shares <= 0:
                continue
            found.append(
                LivePosition(
                    token_id=token,
                    shares=shares,
                    average_price=_decimal(row.get("avgPrice")) or ZERO,
                    condition_id=str(row.get("conditionId") or ""),
                    title=str(row.get("title") or ""),
                    outcome=str(row.get("outcome") or ""),
                    redeemable=bool(row.get("redeemable")),
                )
            )
        return found

    async def open_orders(self) -> list[dict]:
        orders = await asyncio.to_thread(self._require().get_orders)
        return [row for row in orders or [] if isinstance(row, dict)]

    async def cancel_all(self) -> dict:
        return await asyncio.to_thread(self._require().cancel_all)

    # ------------------------------------------------------------------ orders

    PREWARM_TTL = 120.0

    async def prewarm(self, token_id: str) -> None:
        """Load the per-token values order creation would otherwise fetch.

        Called while the engine is already waiting on market metadata, so the
        order itself costs exactly one request even for a market we have never
        traded. The client caches tick size for five minutes; refreshing well
        inside that window keeps the submit path free of a surprise fetch.
        """
        warmed = self._prewarmed.get(token_id)
        if self._client is None or (warmed and time.monotonic() - warmed < self.PREWARM_TTL):
            return
        client = self._client

        def load():
            client.get_tick_size(token_id)
            client.get_neg_risk(token_id)
            client.get_fee_rate_bps(token_id)

        try:
            await asyncio.to_thread(load)
            self._prewarmed[token_id] = time.monotonic()
        except Exception as exc:
            log.info("live_prewarm_failed", token_id=token_id, error=type(exc).__name__)

    async def buy(
        self,
        token_id: str,
        usdc: Decimal,
        price_limit: Decimal,
        *,
        neg_risk: bool | None = None,
    ) -> LiveOrder:
        """Spend at most `usdc`, never above `price_limit` per share."""
        return await self._submit("BUY", token_id, usdc, price_limit, neg_risk)

    async def sell(
        self,
        token_id: str,
        shares: Decimal,
        price_limit: Decimal,
        *,
        neg_risk: bool | None = None,
    ) -> LiveOrder:
        """Sell at most `shares`, never below `price_limit` per share."""
        return await self._submit("SELL", token_id, shares, price_limit, neg_risk)

    async def _submit(
        self,
        side: str,
        token_id: str,
        amount: Decimal,
        price_limit: Decimal,
        neg_risk: bool | None,
    ) -> LiveOrder:
        from py_clob_client.clob_types import MarketOrderArgs, PartialCreateOrderOptions

        client = self._require()
        if side not in {"BUY", "SELL"}:
            return LiveOrder(submitted=False, error="invalid_order_side")
        if amount <= 0 or price_limit <= 0 or price_limit >= 1:
            return LiveOrder(submitted=False, error="invalid_order_amount_or_price")
        args = MarketOrderArgs(
            token_id=token_id,
            amount=float(amount),
            side=side,
            # An explicit limit keeps the client from fetching the book to
            # compute one, and is the only thing bounding what we pay.
            price=float(price_limit),
            order_type=self._order_type,
        )
        # Tick size is deliberately left to the client's cached value: it is the
        # market's own minimum, and a smaller one is refused outright.
        options = PartialCreateOrderOptions(neg_risk=neg_risk)
        started = time.monotonic()
        try:
            if self.settings.live_dry_run:
                signed = await asyncio.to_thread(client.create_market_order, args, options)
                log.warning(
                    "live_dry_run_order",
                    side=side,
                    token_id=token_id,
                    amount=str(amount),
                    price_limit=str(price_limit),
                    order=str(signed)[:400],
                )
                return LiveOrder(
                    submitted=False,
                    status="dry_run",
                    error="live_dry_run",
                    submit_ms=(time.monotonic() - started) * 1000,
                )

            def sign_and_post():
                signed = client.create_market_order(args, options)
                return client.post_order(signed, self._order_type)

            raw = await asyncio.to_thread(sign_and_post)
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            log.error(
                "live_order_failed",
                side=side,
                token_id=token_id,
                amount=str(amount),
                price_limit=str(price_limit),
                error=detail[:400],
            )
            return LiveOrder(
                submitted=False,
                error=detail[:240],
                submit_ms=(time.monotonic() - started) * 1000,
            )
        submit_ms = (time.monotonic() - started) * 1000
        order = self._parse_submit(raw, side, submit_ms)
        log.info(
            "live_order_submitted",
            side=side,
            token_id=token_id,
            requested=str(amount),
            price_limit=str(price_limit),
            order_id=order.order_id,
            status=order.status,
            matched_shares=str(order.shares),
            confirmed=order.confirmed,
            submit_ms=round(submit_ms, 1),
            error=order.error,
        )
        if order.submitted and not order.confirmed and order.order_id:
            # The response did not say how much matched. Ask, rather than guess:
            # this happens after the order is already placed, so it costs no
            # execution speed, only how fast our own record settles.
            order = await self.confirm(order)
        return order

    def _parse_submit(self, raw, side: str, submit_ms: float) -> LiveOrder:
        if not isinstance(raw, dict):
            return LiveOrder(submitted=False, error="unreadable_exchange_response")
        error = raw.get("errorMsg") or raw.get("error") or None
        success = raw.get("success")
        order_id = str(raw.get("orderID") or raw.get("orderId") or raw.get("id") or "")
        status = str(raw.get("status") or "")
        if success is False or (error and not order_id):
            return LiveOrder(
                submitted=False,
                order_id=order_id,
                status=status,
                error=str(error or "order_rejected")[:240],
                submit_ms=submit_ms,
                raw=raw,
            )
        making = _decimal(raw.get("makingAmount"), None)
        taking = _decimal(raw.get("takingAmount"), None)
        shares = notional = None
        if making is not None and taking is not None:
            # Maker pays, taker receives: for our BUY we pay USDC and receive
            # shares, for our SELL it is the other way round.
            shares, notional = (taking, making) if side == "BUY" else (making, taking)
        if shares is None or notional is None or shares < 0 or notional < 0:
            return LiveOrder(
                submitted=True,
                order_id=order_id,
                status=status or "unconfirmed",
                confirmed=False,
                error=str(error)[:240] if error else None,
                submit_ms=submit_ms,
                raw=raw,
            )
        return LiveOrder(
            submitted=True,
            order_id=order_id,
            status=status or "matched",
            shares=shares,
            notional=notional.quantize(CASH),
            average_price=(notional / shares) if shares > 0 else ZERO,
            confirmed=True,
            error=str(error)[:240] if error else None,
            submit_ms=submit_ms,
            raw=raw,
        )

    async def confirm(self, order: LiveOrder) -> LiveOrder:
        """Read back what an accepted order actually matched."""
        client = self._require()
        for attempt in range(self.CONFIRM_ATTEMPTS):
            try:
                raw = await asyncio.to_thread(client.get_order, order.order_id)
            except Exception as exc:
                log.warning(
                    "live_order_confirm_failed",
                    order_id=order.order_id,
                    error=type(exc).__name__,
                )
                return order
            if isinstance(raw, dict):
                status = str(raw.get("status") or order.status)
                matched = _decimal(raw.get("size_matched"), None)
                if matched is not None and matched > 0:
                    price, fee = await self._traded_price(order.order_id)
                    if price is None:
                        # The order's own price is the limit it matched within.
                        # Better than inventing one, and the cash reconciliation
                        # against the exchange corrects the difference.
                        price = _decimal(raw.get("price"), None)
                        if price is None or price <= 0:
                            log.warning(
                                "live_fill_price_unknown",
                                order_id=order.order_id,
                                matched=str(matched),
                            )
                            return order
                        log.warning(
                            "live_fill_price_from_order_limit",
                            order_id=order.order_id,
                            price=str(price),
                        )
                    return LiveOrder(
                        submitted=True,
                        order_id=order.order_id,
                        status=status,
                        shares=matched,
                        notional=(matched * price).quantize(CASH),
                        average_price=price,
                        fee=fee,
                        confirmed=True,
                        submit_ms=order.submit_ms,
                        raw=raw,
                    )
                if status.upper() in {"CANCELED", "CANCELLED", "UNMATCHED", "EXPIRED"}:
                    return LiveOrder(
                        submitted=True,
                        order_id=order.order_id,
                        status=status,
                        confirmed=True,
                        submit_ms=order.submit_ms,
                        raw=raw,
                    )
            if attempt + 1 < self.CONFIRM_ATTEMPTS:
                await asyncio.sleep(self.CONFIRM_DELAY)
        # Still unknown: reconciliation against the exchange handles it, and an
        # unconfirmed order must never be recorded as a fill.
        log.warning("live_order_unconfirmed", order_id=order.order_id, status=order.status)
        return order

    async def _traded_price(self, order_id: str) -> tuple[Decimal | None, Decimal]:
        """Size-weighted price and fee of the trades behind one order.

        None when the exchange has not reported them: a fill is never recorded
        at a price nobody quoted.
        """
        from py_clob_client.clob_types import TradeParams

        try:
            trades = await asyncio.to_thread(self._require().get_trades, TradeParams(id=order_id))
        except Exception as exc:
            log.info("live_trades_unavailable", order_id=order_id, error=type(exc).__name__)
            return None, ZERO
        shares = notional = fee = ZERO
        for trade in trades or []:
            if not isinstance(trade, dict):
                continue
            size = _decimal(trade.get("size")) or ZERO
            price = _decimal(trade.get("price")) or ZERO
            if size <= 0 or price <= 0:
                continue
            shares += size
            notional += size * price
            fee += _decimal(trade.get("fee")) or ZERO
        if shares <= 0:
            return None, ZERO
        return notional / shares, fee.quantize(CASH)
