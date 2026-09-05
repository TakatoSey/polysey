from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Account, Leader, PaperOrder, Position, RiskRule
from .paper import Fill


async def get_or_create_account(session: AsyncSession, starting: Decimal) -> Account:
    account = await session.scalar(select(Account).where(Account.id == 1))
    if account:
        return account
    account = Account(id=1, paper_balance=starting, starting_balance=starting)
    session.add(account)
    await session.flush()
    return account


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
        account.paper_balance += proceeds
        trade_pnl = proceeds - (position.average_price * sold)
        position.realized_pnl += trade_pnl
        account.realized_pnl += trade_pnl
        position.shares -= sold
        position.cost_basis = position.average_price * position.shares
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
