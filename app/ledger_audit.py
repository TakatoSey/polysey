"""Read-only ledger reconciliation: python -m app.ledger_audit (no API calls)."""

import asyncio
import json
from decimal import Decimal

from sqlalchemy import select, text

from .accounting import replay
from .db import SessionLocal
from .models import Account, CopyTrade, ExitIntent, Leader, PaperOrder, Position


def summarize(account, rows, positions, leaders, intents):
    zero = Decimal(0)
    holdings, warnings = replay(rows)
    cash = account.starting_balance
    for order, _ in rows:
        if order.status not in {"filled", "partial", "settled"} or order.filled_shares <= 0:
            continue
        notional = order.filled_shares * order.average_fill_price
        cash += -notional - order.fee if order.side == "BUY" else notional - order.fee
    tokens = {key[0] for key in holdings} | {p.token_id for p in positions}
    mismatches = []
    for token in sorted(tokens):
        recorded = sum((p.shares for p in positions if p.token_id == token), zero)
        reconstructed = sum((h.shares for (t, _), h in holdings.items() if t == token), zero)
        if abs(recorded - reconstructed) > Decimal("0.000001"):
            mismatches.append(
                {"token_id": token, "positions_shares": recorded, "orders_shares": reconstructed}
            )
    by_leader = []
    for leader in leaders:
        owned = [h for (_, owner), h in holdings.items() if owner == leader.id]
        by_leader.append(
            {
                "id": leader.id,
                "name": leader.label or leader.address,
                "realized_pnl": sum((h.realized for h in owned), zero),
                "open_cost": sum((h.cost for h in owned), zero),
            }
        )
    realized = sum((h.realized for h in holdings.values()), zero)
    gaps = {
        "cash_gap": account.paper_balance - cash,
        "realized_gap": account.realized_pnl - realized,
        "balance_identity_gap": account.paper_balance
        + sum((p.cost_basis for p in positions), zero)
        - account.starting_balance
        - account.realized_pnl,
    }
    return {
        "consistent": not warnings
        and not mismatches
        and all(abs(v) <= Decimal("0.0001") for v in gaps.values()),
        "paper_balance": account.paper_balance,
        "cash_from_orders": cash,
        **gaps,
        "share_mismatches": mismatches,
        "warnings": warnings,
        "leaders": by_leader,
        "pending_exits": [
            {
                "leader_id": i.leader_id,
                "token_id": i.token_id,
                "remaining": i.remaining,
                "min_price": i.min_price,
                "attempts": i.attempts,
                "reason": i.last_reason,
            }
            for i in intents
        ],
        "note": "Read-only. Rounded average fill prices may create small reconstruction gaps. "
        "This checks bookkeeping, not live-fill realism or profitability.",
    }


async def main():
    async with SessionLocal() as session:
        if session.bind.dialect.name == "postgresql":
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await session.execute(text("SET TRANSACTION READ ONLY"))
        account = await session.get(Account, 1)
        if account is None:
            raise RuntimeError("account_not_initialized")
        rows = (
            await session.execute(
                select(PaperOrder, CopyTrade.leader_id)
                .outerjoin(CopyTrade, CopyTrade.id == PaperOrder.copy_trade_id)
                .order_by(PaperOrder.created_at, PaperOrder.id)
            )
        ).all()
        positions = list(await session.scalars(select(Position)))
        leaders = list(await session.scalars(select(Leader)))
        intents = list(await session.scalars(select(ExitIntent).where(ExitIntent.remaining > 0)))
        report = summarize(account, rows, positions, leaders, intents)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
