from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Account,
    CopyTrade,
    ExecutionPolicy,
    Leader,
    PaperOrder,
    Position,
    RiskRule,
    RuntimeMigration,
    SourceReceipt,
)
from .paper import Fill


async def get_or_create_account(
    session: AsyncSession, starting: Decimal, *, settings=None
) -> Account:
    account = await session.scalar(select(Account).where(Account.id == 1))
    if account:
        return account
    account = Account(id=1, paper_balance=starting, starting_balance=starting)
    if settings is not None:
        account.trade_size = settings.default_trade_size
        account.max_trade_size = settings.max_trade_size
        # Retained for historical compatibility only; execution uses ExecutionPolicy.
        account.slippage_bps = getattr(settings, "default_slippage_bps", None) or 500
    session.add(account)
    await session.flush()
    return account


async def get_execution_policy(session, settings):
    policy = await session.get(ExecutionPolicy, 1)
    if policy is None:
        policy = ExecutionPolicy(id=1, slippage_price=settings.default_slippage_cents / 100)
        session.add(policy)
        await session.flush()
    return policy


async def initialize_execution(session, settings):
    """Add canonical aliases once without rewriting past orders/cash/PnL."""
    from .polymarket import copy_event_key

    await get_execution_policy(session, settings)
    if await session.get(RuntimeMigration, "source_receipts_v1"):
        return
    existing = set(await session.scalars(select(SourceReceipt.event_key)))
    rows = (
        await session.execute(
            select(CopyTrade, Leader.address)
            .join(Leader, Leader.id == CopyTrade.leader_id)
            .order_by(CopyTrade.id)
        )
    ).all()
    for trade, address in rows:
        key = copy_event_key(trade.event_key, address)
        if key not in existing:
            session.add(SourceReceipt(event_key=key, copy_trade_id=trade.id))
            existing.add(key)
    session.add(RuntimeMigration(name="source_receipts_v1"))


async def get_leaders(session: AsyncSession) -> list[Leader]:
    return list((await session.scalars(select(Leader).order_by(Leader.created_at))).all())


async def get_leader(session: AsyncSession, address: str) -> Leader | None:
    return await session.scalar(select(Leader).where(Leader.address == address.lower()))


async def add_leader(session: AsyncSession, address: str, label: str | None = None) -> Leader:
    leader = await get_leader(session, address)
    if leader:
        leader.active = True
        return leader
    leader = Leader(address=address.lower(), label=label)
    session.add(leader)
    await session.flush()
    return leader


async def get_position(session: AsyncSession, token_id: str) -> Position | None:
    return await session.scalar(select(Position).where(Position.token_id == token_id))


async def get_risk(session: AsyncSession, token_id: str) -> RiskRule | None:
    return await session.scalar(select(RiskRule).where(RiskRule.token_id == token_id))


async def apply_fill(
    session: AsyncSession,
    fill: Fill,
    token_id: str,
    side: str,
    title: str,
    outcome: str,
    condition_id: str,
    account: Account,
    *,
    cost_to_release: Decimal | None = None,
) -> None:
    position = await get_position(session, token_id)
    if side == "BUY":
        total_cost = fill.notional + fill.fee
        if account.paper_balance < total_cost:
            raise ValueError("insufficient_balance")
        account.paper_balance -= total_cost
        if not position:
            position = Position(
                token_id=token_id, condition_id=condition_id, title=title, outcome=outcome
            )
            session.add(position)
            await session.flush()
        position.shares += fill.shares
        position.cost_basis += total_cost
        position.average_price = (
            position.cost_basis / position.shares if position.shares else Decimal(0)
        )
    else:
        if not position or position.shares <= 0:
            raise ValueError("no_position")
        if fill.shares > position.shares:
            raise ValueError("insufficient_shares")
        sold = fill.shares
        proceeds = fill.notional - fill.fee
        cost = (
            cost_to_release
            if cost_to_release is not None
            else position.cost_basis * sold / position.shares
        )
        if sold == position.shares:
            # The last fill releases the exact remaining stored cost, including
            # tiny differences from historically rounded average fill prices.
            cost = position.cost_basis
        if cost < 0 or cost > position.cost_basis + Decimal("0.000001"):
            raise ValueError("invalid_cost_allocation")
        cost = min(cost, position.cost_basis)
        account.paper_balance += proceeds
        trade_pnl = proceeds - cost
        position.realized_pnl += trade_pnl
        account.realized_pnl += trade_pnl
        position.shares -= sold
        position.cost_basis -= cost
        position.average_price = (
            position.cost_basis / position.shares if position.shares else Decimal(0)
        )
        if position.shares <= Decimal("0.00000001"):
            await session.delete(position)


async def positions(session: AsyncSession) -> list[Position]:
    return list((await session.scalars(select(Position).where(Position.shares > 0))).all())


async def orders(session: AsyncSession) -> list[PaperOrder]:
    return list(
        (
            await session.scalars(
                select(PaperOrder).order_by(PaperOrder.created_at.desc()).limit(30)
            )
        ).all()
    )
