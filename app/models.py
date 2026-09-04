from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_balance: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    starting_balance: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0, nullable=False)
    trade_size: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("5"), nullable=False
    )
    max_trade_size: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("10"), nullable=False
    )
    slippage_bps: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    fee_rate: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.05"), nullable=False
    )
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Leader(Base):
    __tablename__ = "leaders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    initialized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_timestamp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[str] = mapped_column(String(100), index=True)
    condition_id: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(120))
    shares: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=0, nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=0, nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class CopyTrade(Base):
    __tablename__ = "copy_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leader_id: Mapped[int] = mapped_column(ForeignKey("leaders.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(400), unique=True, index=True)
    timestamp: Mapped[int] = mapped_column(Integer, index=True)
    token_id: Mapped[str] = mapped_column(String(100))
    condition_id: Mapped[str] = mapped_column(String(100))
    side: Mapped[str] = mapped_column(String(4))
    leader_size: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    leader_price: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    status: Mapped[str] = mapped_column(String(24), default="detected")
    skip_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    copy_trade_id: Mapped[int | None] = mapped_column(ForeignKey("copy_trades.id"), nullable=True)
    token_id: Mapped[str] = mapped_column(String(100), index=True)
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(4), default="FAK")
    requested_shares: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    filled_shares: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=0)
    average_fill_price: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=0)
    fee: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0)
    status: Mapped[str] = mapped_column(String(24), default="submitted")
    reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RiskRule(Base):
    __tablename__ = "risk_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    stop_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    take_profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    trailing_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    high_water_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LeaderPosition(Base):
    __tablename__ = "leader_positions"
    __table_args__ = (UniqueConstraint("leader_id", "token_id", name="uq_leader_token"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leader_id: Mapped[int] = mapped_column(ForeignKey("leaders.id"), index=True)
    token_id: Mapped[str] = mapped_column(String(100), index=True)
    shares: Mapped[Decimal] = mapped_column(Numeric(24, 10), default=0, nullable=False)
