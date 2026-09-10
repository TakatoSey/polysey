"""What other sizing parameters would have targeted, on our own history.

python -m app.sizing_replay --power 2 --odds-weight 0.3

Read-only. This replays the entry TARGET, not the fill: the order book at the
time is not retained, so it cannot say how many shares would have been matched.

Only rows the formula reproduces exactly are projected. A row where it does not
was either clamped by a cap or a market minimum, or was recorded under
different settings; either way its replay would be a guess, so it is counted
and excluded rather than quietly included.
"""

import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, text

from .config import get_settings
from .db import SessionLocal
from .models import SizingAudit
from .sizing import entry_intensity, odds_factor_for

ZERO = Decimal(0)
EXACT = Decimal("0.01")


@dataclass(frozen=True)
class Parameters:
    conviction_power: Decimal
    odds_weight: Decimal
    max_multiplier: Decimal
    balance_pct_scale: Decimal = Decimal(1)


def raw_target(row, params: Parameters) -> Decimal | None:
    """The formula's target for one recorded entry, before cap and floor."""
    reference = Decimal(row.reference_notional or 0)
    if reference <= ZERO:
        return None
    intensity = entry_intensity(
        Decimal(row.leader_notional) / reference,
        params.max_multiplier,
        params.conviction_power,
    )
    odds = odds_factor_for(Decimal(row.leader_vwap), params.odds_weight)
    base = Decimal(row.base_budget) * params.balance_pct_scale
    return base * intensity * Decimal(row.price_factor) * odds


def summarize(rows, current: Parameters, proposed: Parameters) -> dict:
    reproduced, skipped = [], 0
    for row in rows:
        modelled = raw_target(row, current)
        if modelled is None or abs(modelled - Decimal(row.target_budget)) > EXACT:
            skipped += 1
            continue
        reproduced.append(row)
    changes = []
    for row in reproduced:
        was = Decimal(row.target_budget)
        becomes = raw_target(row, proposed)
        changes.append((becomes - was, was, becomes, row))
    return {
        "rows": len(rows),
        "projected": len(reproduced),
        "excluded": skipped,
        "was_total": sum((was for _, was, _, _ in changes), ZERO),
        "becomes_total": sum((becomes for _, _, becomes, _ in changes), ZERO),
        "bigger": sum(1 for change, *_ in changes if change > EXACT),
        "smaller": sum(1 for change, *_ in changes if change < -EXACT),
        "largest": sorted(changes, key=lambda item: abs(item[0]), reverse=True)[:5],
    }


def render(report: dict, current: Parameters, proposed: Parameters) -> str:
    lines = [
        f"Rows with a sizing audit: {report['rows']}",
        f"Reproduced exactly and projected: {report['projected']}",
        f"Excluded (clamped, or recorded under other settings): {report['excluded']}",
    ]
    if not report["projected"]:
        lines.append("\nNothing to project. Run again once entries land under current settings.")
        return "\n".join(lines)
    was, becomes = report["was_total"], report["becomes_total"]
    lines += [
        "",
        f"current   power={current.conviction_power:g} odds={current.odds_weight:g}"
        f"   total target ${was:.2f}",
        f"proposed  power={proposed.conviction_power:g} odds={proposed.odds_weight:g}"
        f"   total target ${becomes:.2f}",
        f"           change {becomes - was:+.2f}"
        + (f" ({(becomes - was) / was * 100:+.1f}%)" if was else ""),
        "",
        f"bigger: {report['bigger']}   smaller: {report['smaller']}",
    ]
    if report["largest"]:
        lines.append("\nLargest moves:")
        for _, old, new, row in report["largest"]:
            norm = Decimal(row.leader_notional) / Decimal(row.reference_notional)
            lines.append(
                f"  trade {row.copy_trade_id}: ${old:.2f} -> ${new:.2f}"
                f"   entry {norm:.2f}x norm, price {Decimal(row.leader_vwap) * 100:.0f}c"
            )
    lines.append("\nTargets only: the book is not retained, so fills cannot be replayed.")
    return "\n".join(lines)


async def main(proposed: Parameters) -> None:
    settings = get_settings()
    current = Parameters(
        settings.sizing_conviction_power,
        settings.sizing_odds_weight,
        settings.smart_sizing_max_multiplier,
    )
    async with SessionLocal() as session:
        if session.bind.dialect.name == "postgresql":
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = list(await session.scalars(select(SizingAudit)))
    print(render(summarize(rows, current, proposed), current, proposed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--power", type=Decimal, required=True, help="SIZING_CONVICTION_POWER")
    parser.add_argument("--odds-weight", type=Decimal, required=True, help="SIZING_ODDS_WEIGHT")
    parser.add_argument("--max-multiplier", type=Decimal, default=None)
    parser.add_argument(
        "--balance-pct-scale",
        type=Decimal,
        default=Decimal(1),
        help="Scale the base budget, e.g. 2 for twice COPY_BALANCE_PCT",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            Parameters(
                args.power,
                args.odds_weight,
                args.max_multiplier or get_settings().smart_sizing_max_multiplier,
                args.balance_pct_scale,
            )
        )
    )
