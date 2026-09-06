"""Reconstruct holdings by token AND owner; never consume another market's cost."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from .models import CopyTrade, PaperOrder

ZERO = Decimal(0)
EPS = Decimal("0.000001")


@dataclass
class Holding:
    shares: Decimal = ZERO
    cost: Decimal = ZERO
    realized: Decimal = ZERO


def replay(rows):
    holdings = defaultdict(Holding)
    warnings = []
    for order, owner in rows:
        if order.status not in {"filled", "partial", "settled"} or order.filled_shares <= 0:
            continue
        qty = order.filled_shares
        notional = qty * order.average_fill_price
        if order.side == "BUY":
            holding = holdings[order.token_id, owner]
            holding.shares += qty
            holding.cost += notional + order.fee
            continue
        candidates = [
            (key, h) for key, h in holdings.items() if key[0] == order.token_id and h.shares > 0
        ]
        if owner is not None and order.status != "settled":
            primary = [(key, h) for key, h in candidates if key[1] == owner]
            owned = sum((h.shares for _, h in primary), ZERO)
            if qty > owned + EPS:
                warnings.append(f"historical_cross_owner_sell:{order.id}")
            # Historical releases beyond the initiating owner's inventory must
            # still reconcile; mark ambiguity rather than hiding old cash flows.
            pools = [primary, [(key, h) for key, h in candidates if key[1] != owner]]
        else:
            pools = [candidates]  # settlement/risk exit distributed by held shares
        left = qty
        for pool in pools:
            total = sum((h.shares for _, h in pool), ZERO)
            if total <= 0 or left <= 0:
                continue
            take = min(total, left)
            for _, h in pool:
                sold = take * h.shares / total
                cost = h.cost * sold / h.shares
                h.realized += (notional - order.fee) * sold / qty - cost
                h.shares -= sold
                h.cost -= cost
            left -= take
        if left > EPS:
            warnings.append(f"unattributed_sell:{order.id}")
    return dict(holdings), warnings


async def inventory(session, token_id=None):
    stmt = (
        select(PaperOrder, CopyTrade.leader_id)
        .outerjoin(CopyTrade, CopyTrade.id == PaperOrder.copy_trade_id)
        .where(
            PaperOrder.status.in_(["filled", "partial", "settled"]), PaperOrder.filled_shares > 0
        )
    )
    if token_id is not None:
        stmt = stmt.where(PaperOrder.token_id == token_id)
    rows = (await session.execute(stmt.order_by(PaperOrder.created_at, PaperOrder.id))).all()
    return replay(rows)
