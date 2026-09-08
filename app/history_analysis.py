"""Offline execution analysis: python -m app.history_analysis PATH_TO_AUDIT_JSON.

No network or database writes. Historical execution is not a live-fill guarantee.
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from .accounting import replay
from .price_limits import allowed_buy_price

D = Decimal
ZERO = D(0)


def analyze_history(history):
    # Accept the live export (Decimal/datetime) and its JSON representation.
    trades = {t["id"]: t for t in history["copy_trades"]}
    names = {l["id"]: l.get("label") or l["address"] for l in history["leaders"]}
    observations = history.get("source_observations", [])
    metadata = {o["token_id"]: o for o in observations}
    rows = []
    for raw in history["paper_orders"]:
        values = dict(raw)
        for name in ("filled_shares", "average_fill_price", "fee"):
            values[name] = D(values[name])
        stamp = values["created_at"]
        values["created_at"] = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        order = SimpleNamespace(**values)
        trade = trades.get(order.copy_trade_id)
        rows.append((order, trade["leader_id"] if trade else None))
    rows.sort(key=lambda row: (row[0].created_at, row[0].id))
    holdings, warnings = replay(rows)
    cash_flows = defaultdict(lambda: {"buy_cost": ZERO, "sell_net": ZERO, "payout": ZERO})
    fills, fees, adverse_cost, favorable_saving = [], ZERO, ZERO, ZERO
    for order, owner in rows:
        if order.status not in {"filled", "partial", "settled"} or order.filled_shares <= 0:
            continue
        amount = order.filled_shares * order.average_fill_price
        flow = cash_flows[order.token_id]
        fees += order.fee
        if order.side == "BUY":
            flow["buy_cost"] += amount + order.fee
            trade = trades.get(order.copy_trade_id)
            leader_price = D(trade["leader_price"]) if trade else None
            price_cost = (
                order.filled_shares * (order.average_fill_price - leader_price) if trade else None
            )
            if price_cost is not None:
                adverse_cost += max(ZERO, price_cost)
                favorable_saving += max(ZERO, -price_cost)
            fills.append(
                {
                    "order_id": order.id,
                    "leader": names.get(owner, owner),
                    "token_id": order.token_id,
                    "leader_price": leader_price,
                    "fill_price": order.average_fill_price,
                    "debit": amount + order.fee,
                    "outside_current_buy_range": not allowed_buy_price(order.average_fill_price)
                    or (leader_price is not None and not allowed_buy_price(leader_price)),
                }
            )
        elif order.status == "settled":
            flow["payout"] += amount - order.fee
        else:
            flow["sell_net"] += amount - order.fee
    by_token = []
    for token, flow in cash_flows.items():
        owners = [(owner, h) for (t, owner), h in holdings.items() if t == token]
        by_token.append(
            {
                "token_id": token,
                "title": metadata.get(token, {}).get("title"),
                "outcome": metadata.get(token, {}).get("outcome"),
                **flow,
                "open_shares": sum((h.shares for _, h in owners), ZERO),
                "realized_pnl": sum((h.realized for _, h in owners), ZERO),
                "leaders": [
                    {"id": owner, "name": names.get(owner), "realized_pnl": h.realized}
                    for owner, h in owners
                ],
            }
        )
    recorded_delays = []
    for t in trades.values():
        if t["side"] != "BUY" or not t["timestamp"]:
            continue
        stamp = t["created_at"]
        stamp = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        recorded_delays.append(D(str(stamp.timestamp())) - D(t["timestamp"]))
    return {
        "buy_signals": sum(t["side"] == "BUY" for t in trades.values()),
        "buy_fills": len(fills),
        "buy_skip_reasons": dict(
            Counter(
                t["skip_reason"] or t["status"]
                for t in trades.values()
                if t["side"] == "BUY" and t["status"] != "executed"
            )
        ),
        "realized_pnl": sum((h.realized for h in holdings.values()), ZERO),
        "fees": fees,
        "buy_price_disadvantage_dollars": adverse_cost,
        "buy_price_improvement_dollars": favorable_saving,
        "outside_current_range_buys": [f for f in fills if f["outside_current_buy_range"]],
        "source_to_record_max_seconds": max(recorded_delays, default=None),
        "markets": sorted(by_token, key=lambda row: row["realized_pnl"]),
        "warnings": warnings,
        "note": "All retained history, not necessarily today. BUY signals may be batched. "
        "Source-to-record delay includes publication, detection, queue and processing, not VPS latency alone. "
        "Price comparisons use the same filled shares; they are not an achievable counterfactual PNL. "
        "Missing historical market names/transaction hashes are not inferred.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    with args.path.open(encoding="utf-8-sig") as stream:
        report = json.load(stream)
    print(
        json.dumps(
            analyze_history(report.get("history", report)),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
