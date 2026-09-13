import re
from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Typed by hand into .env, so a live bot can never be started by accident.
LIVE_ACKNOWLEDGEMENT = "I_UNDERSTAND_REAL_MONEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_id: int = Field(alias="TELEGRAM_ALLOWED_USER_ID")
    database_url: str = Field(
        default="postgresql+asyncpg://polycopy:polycopy@db:5432/polycopy",
        alias="DATABASE_URL",
    )
    paper_initial_balance: Decimal = Field(default=Decimal("100"), alias="PAPER_INITIAL_BALANCE")
    poll_interval_seconds: float = Field(default=0.5, ge=0.25, alias="POLL_INTERVAL_SECONDS")
    maintenance_interval_seconds: float = Field(
        default=2.0, ge=0.5, alias="MAINTENANCE_INTERVAL_SECONDS"
    )
    exit_retry_enabled: bool = Field(default=True, alias="EXIT_RETRY_ENABLED")
    exit_retry_seconds: float = Field(default=1, ge=0.25, le=30, alias="EXIT_RETRY_SECONDS")
    copy_prepare_concurrency: int = Field(default=8, ge=1, le=32, alias="COPY_PREPARE_CONCURRENCY")
    copy_queue_limit: int = Field(default=256, ge=1, le=2000, alias="COPY_QUEUE_LIMIT")
    rtds_enabled: bool = Field(default=True, alias="RTDS_ENABLED")
    # Extra simulation delay only. Real VPS/API/WebSocket latency is already
    # included naturally; zero avoids adding an artificial one-second lag.
    copy_latency_seconds: float = Field(default=0.0, ge=0, le=60, alias="COPY_LATENCY_SECONDS")
    max_signal_age_rtds_seconds: float = Field(
        default=2.0, ge=0.1, le=60, alias="MAX_SIGNAL_AGE_RTDS_SECONDS"
    )
    max_signal_age_rest_seconds: float = Field(
        default=5.0, ge=0.1, le=300, alias="MAX_SIGNAL_AGE_REST_SECONDS"
    )
    default_trade_size: Decimal = Field(default=Decimal("5"), alias="DEFAULT_TRADE_SIZE")
    max_trade_size: Decimal = Field(default=Decimal("10"), alias="MAX_TRADE_SIZE")
    # Base all-in budget for one entry, from our cash at entry start.
    copy_balance_pct: Decimal = Field(default=Decimal("0.05"), gt=0, le=1, alias="COPY_BALANCE_PCT")
    leader_order_scale: Decimal = Field(default=Decimal("0.10"), alias="LEADER_ORDER_SCALE")
    smart_sizing_enabled: bool = Field(default=True, alias="SMART_SIZING_ENABLED")
    smart_sizing_max_multiplier: Decimal = Field(
        default=Decimal("3"), ge=1, le=10, alias="SMART_SIZING_MAX_MULTIPLIER"
    )
    smart_sizing_burst_seconds: int = Field(
        default=5, ge=1, le=15, alias="SMART_SIZING_BURST_SECONDS"
    )
    smart_sizing_min_samples: int = Field(default=3, ge=1, le=100, alias="SMART_SIZING_MIN_SAMPLES")
    smart_sizing_stats_refresh_seconds: int = Field(
        default=86400, ge=60, alias="SMART_SIZING_STATS_REFRESH_SECONDS"
    )
    # How long a market's unresolved state is reused before asking again. This
    # is the dominant delay in noticing a settlement, not the maintenance loop.
    resolution_cache_seconds: float = Field(
        default=5.0, ge=1, le=120, alias="RESOLUTION_CACHE_SECONDS"
    )
    min_copy_notional: Decimal = Field(default=Decimal("1.10"), alias="MIN_COPY_NOTIONAL")
    max_outcome_exposure: Decimal = Field(default=Decimal("50"), alias="MAX_OUTCOME_EXPOSURE")
    # Cash kept back from equity so a burst of signals cannot deploy everything.
    min_cash_reserve_pct: Decimal = Field(
        default=Decimal("0.25"), ge=0, lt=1, alias="MIN_CASH_RESERVE_PCT"
    )
    # How far the exchange minimum may exceed our own base before we skip the
    # market instead of overspending to reach it.
    sizing_floor_max_multiple: Decimal = Field(
        default=Decimal("2"), ge=1, le=10, alias="SIZING_FLOOR_MAX_MULTIPLE"
    )
    # Above 1 an entry far from the leader's own norm counts for more than the
    # ratio alone; 1 keeps the plain proportional behaviour.
    sizing_conviction_power: Decimal = Field(
        default=Decimal("1.5"), ge=1, le=3, alias="SIZING_CONVICTION_POWER"
    )
    # Share of the contract-price nudge that is applied. 0 ignores odds, 1 is
    # the full 0.60-1.40 swing.
    sizing_odds_weight: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=1, alias="SIZING_ODDS_WEIGHT"
    )
    # Human units: 5 cents = $0.05; stored separately from legacy percentage bps.
    default_slippage_cents: Decimal = Field(
        default=Decimal("5"), ge=0, lt=100, alias="DEFAULT_SLIPPAGE_CENTS"
    )
    # Read-only compatibility with old .env files; new configurations should
    # use DEFAULT_SLIPPAGE_CENTS.
    default_slippage_bps: int | None = Field(default=None, alias="DEFAULT_SLIPPAGE_BPS")
    # --- live trading: real money on the Polymarket CLOB ---
    # "paper" simulates fills locally. "live" signs and submits real orders and
    # takes cash, positions and fills from the exchange.
    trading_mode: str = Field(default="paper", alias="TRADING_MODE")
    # A deliberate, typed acknowledgement. Live mode refuses to start without it.
    live_confirm: str | None = Field(default=None, alias="LIVE_CONFIRM")
    polymarket_private_key: str | None = Field(default=None, alias="POLYMARKET_PRIVATE_KEY")
    # The wallet that holds pUSD. For a Polymarket proxy wallet this is the
    # proxy address, not the address of the signing key.
    polymarket_funder: str | None = Field(default=None, alias="POLYMARKET_FUNDER")
    # 0 = EOA, 1 = legacy POLY_PROXY, 2 = Gnosis Safe.
    # Login method alone does not identify the funding wallet type.
    # 3 = Deposit Wallet (POLY_1271), using an authorized signer.
    polymarket_signature_type: int = Field(default=1, ge=0, le=3, alias="POLYMARKET_SIGNATURE_TYPE")
    polygon_chain_id: int = Field(default=137, alias="POLYGON_CHAIN_ID")
    # FAK keeps paper's behaviour: take what is available now, cancel the rest.
    live_order_type: str = Field(default="FAK", alias="LIVE_ORDER_TYPE")
    # Signs orders and logs them without sending anything to the exchange.
    live_dry_run: bool = Field(default=False, alias="LIVE_DRY_RUN")
    # Hard ceiling per submitted order, checked after every other sizing limit.
    live_max_order_usdc: Decimal = Field(default=Decimal("25"), gt=0, alias="LIVE_MAX_ORDER_USDC")
    # Realized loss since UTC midnight that stops new entries for the day.
    live_max_daily_loss_usdc: Decimal = Field(
        default=Decimal("25"), gt=0, alias="LIVE_MAX_DAILY_LOSS_USDC"
    )
    live_state_refresh_seconds: float = Field(
        default=5.0, ge=1, le=120, alias="LIVE_STATE_REFRESH_SECONDS"
    )
    # Divergence between our ledger and the exchange that stops new entries
    # rather than sizing them from numbers we no longer trust.
    live_drift_tolerance_usdc: Decimal = Field(
        default=Decimal("1"), ge=0, alias="LIVE_DRIFT_TOLERANCE_USDC"
    )
    clob_ws: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com", alias="POLYMARKET_CLOB_WS"
    )
    data_api: str = Field(default="https://data-api.polymarket.com", alias="POLYMARKET_DATA_API")
    clob_api: str = Field(default="https://clob.polymarket.com", alias="POLYMARKET_CLOB")
    gamma_api: str = Field(default="https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA")
    default_leader_address: str | None = Field(default=None, alias="DEFAULT_LEADER_ADDRESS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def live(self) -> bool:
        return self.trading_mode.strip().lower() == "live"

    def live_problems(self) -> list[str]:
        """Everything missing before real orders may be signed, in one place."""
        if not self.live:
            return []
        problems = []
        if self.live_confirm != LIVE_ACKNOWLEDGEMENT:
            problems.append(f"LIVE_CONFIRM must be exactly {LIVE_ACKNOWLEDGEMENT}")
        key = (self.polymarket_private_key or "").strip()
        if not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", key):
            problems.append("POLYMARKET_PRIVATE_KEY must be a 32-byte hex key")
        funder = (self.polymarket_funder or "").strip()
        if self.polymarket_signature_type in (1, 2, 3) and not re.fullmatch(
            r"0x[0-9a-fA-F]{40}", funder
        ):
            problems.append(
                "POLYMARKET_FUNDER must be the funding wallet address for "
                f"POLYMARKET_SIGNATURE_TYPE={self.polymarket_signature_type}"
            )
        if self.live_order_type.upper() not in {"FAK", "FOK"}:
            problems.append("LIVE_ORDER_TYPE must be FAK or FOK")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
