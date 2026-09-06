from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        default=2, ge=1, le=10, alias="SMART_SIZING_BURST_SECONDS"
    )
    smart_sizing_min_samples: int = Field(default=3, ge=1, le=100, alias="SMART_SIZING_MIN_SAMPLES")
    smart_sizing_stats_refresh_seconds: int = Field(
        default=86400, ge=60, alias="SMART_SIZING_STATS_REFRESH_SECONDS"
    )
    min_copy_notional: Decimal = Field(default=Decimal("1.10"), alias="MIN_COPY_NOTIONAL")
    max_outcome_exposure: Decimal = Field(default=Decimal("50"), alias="MAX_OUTCOME_EXPOSURE")
    # Human units: 5 cents = $0.05; stored separately from legacy percentage bps.
    default_slippage_cents: Decimal = Field(
        default=Decimal("5"), ge=0, lt=100, alias="DEFAULT_SLIPPAGE_CENTS"
    )
    # Read-only compatibility with old .env files; new configurations should
    # use DEFAULT_SLIPPAGE_CENTS.
    default_slippage_bps: int | None = Field(default=None, alias="DEFAULT_SLIPPAGE_BPS")
    data_api: str = Field(default="https://data-api.polymarket.com", alias="POLYMARKET_DATA_API")
    clob_api: str = Field(default="https://clob.polymarket.com", alias="POLYMARKET_CLOB")
    gamma_api: str = Field(default="https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA")
    default_leader_address: str | None = Field(default=None, alias="DEFAULT_LEADER_ADDRESS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
