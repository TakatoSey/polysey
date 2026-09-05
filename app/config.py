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
    copy_prepare_concurrency: int = Field(default=8, ge=1, le=32, alias="COPY_PREPARE_CONCURRENCY")
    copy_queue_limit: int = Field(default=256, ge=1, le=2000, alias="COPY_QUEUE_LIMIT")
    rtds_enabled: bool = Field(default=True, alias="RTDS_ENABLED")
    # Extra simulation delay only. Real VPS/API/WebSocket latency is already
    # included naturally; zero avoids adding an artificial one-second lag.
    copy_latency_seconds: float = Field(default=0.0, ge=0, le=60, alias="COPY_LATENCY_SECONDS")
    default_trade_size: Decimal = Field(default=Decimal("5"), alias="DEFAULT_TRADE_SIZE")
    max_trade_size: Decimal = Field(default=Decimal("10"), alias="MAX_TRADE_SIZE")
    # Buy budget is primarily a percentage of our own free cash. The leader
    # order notional is an additional proportional ceiling.
    copy_balance_pct: Decimal = Field(default=Decimal("0.05"), alias="COPY_BALANCE_PCT")
    leader_order_scale: Decimal = Field(default=Decimal("0.10"), alias="LEADER_ORDER_SCALE")
    min_copy_notional: Decimal = Field(default=Decimal("1.10"), alias="MIN_COPY_NOTIONAL")
    max_outcome_exposure: Decimal = Field(default=Decimal("50"), alias="MAX_OUTCOME_EXPOSURE")
    default_slippage_bps: int = Field(default=500, alias="DEFAULT_SLIPPAGE_BPS")
    data_api: str = Field(default="https://data-api.polymarket.com", alias="POLYMARKET_DATA_API")
    clob_api: str = Field(default="https://clob.polymarket.com", alias="POLYMARKET_CLOB")
    gamma_api: str = Field(default="https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA")
    default_leader_address: str | None = Field(default=None, alias="DEFAULT_LEADER_ADDRESS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
