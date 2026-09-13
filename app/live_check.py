"""Read-only live-trading preflight: python -m app.live_check [TOKEN_ID]

Sends no trading order. It signs API-authentication challenges and reads the
wallet balance and permissions. Contract addresses come from the pinned SDK;
an optional market check reads current exchange order rules.

Run it on the VPS before switching TRADING_MODE to live, and again after any
change to the key, the funder or the signature type.
"""

import argparse
import asyncio
import json

from .config import LIVE_ACKNOWLEDGEMENT, get_settings
from .live import LiveTrader, LiveTradingUnavailable, contract_addresses


async def check(token_id: str | None) -> int:
    settings = get_settings()
    report: dict = {
        "trading_mode": settings.trading_mode,
        "dry_run": settings.live_dry_run,
        "signature_type": settings.polymarket_signature_type,
        "chain_id": settings.polygon_chain_id,
        "clob": settings.clob_api,
        "collateral_symbol": "pUSD",
        "sdk": "py-clob-client-v2",
        **contract_addresses(settings.polygon_chain_id),
    }
    problems = settings.live_problems()
    if not settings.live:
        # Checking a paper configuration is still useful: it reports exactly
        # what is missing before the switch is flipped.
        forced = settings.model_copy(update={"trading_mode": "live"})
        problems = forced.live_problems()
        report["note"] = (
            "TRADING_MODE is not live; reporting what live mode would require. "
            f"LIVE_CONFIRM must be {LIVE_ACKNOWLEDGEMENT}."
        )
    if problems:
        report["configuration_problems"] = problems
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1

    trader = LiveTrader(settings)
    try:
        account = await trader.start()
    except Exception as exc:
        report["error"] = (
            str(exc) if isinstance(exc, LiveTradingUnavailable) else type(exc).__name__
        )
        report["ready_for_live"] = False
        await trader.close()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1
    client = trader._client  # noqa: SLF001 - a preflight legitimately looks inside
    report.update(
        signer=account.signer,
        funder=account.funder,
        collateral_balance=str(account.cash),
        pusd_balance=str(account.cash),
        exchange_allowance=str(account.allowance),
    )
    verdict = []
    try:
        report["server_ok"] = await asyncio.to_thread(client.get_ok)
    except Exception as exc:
        report["server_error"] = type(exc).__name__
        verdict.append("CLOB health query failed")
    if account.cash <= 0:
        verdict.append(
            "CLOB reports zero pUSD for this wallet/signature configuration; "
            "verify wallet type and funder"
        )
    if account.allowance <= 0:
        verdict.append(
            "the exchange has no allowance over the collateral: orders would be "
            "unable to execute. Check pUSD approval for the V2 exchanges shown above."
        )
    elif account.allowance < account.cash:
        verdict.append(
            f"allowance {account.allowance} is below the balance {account.cash}: "
            "only part of the balance is usable"
        )
    if account.signature_type in (1, 2, 3) and account.signer == account.funder:
        verdict.append(
            "signature type expects a proxy wallet, but the funder equals the "
            "signer. Check POLYMARKET_FUNDER against the address shown in the "
            "Polymarket UI."
        )
    if token_id:
        try:
            report["market"] = {
                "token_id": token_id,
                "tick_size": await asyncio.to_thread(client.get_tick_size, token_id),
                "neg_risk": await asyncio.to_thread(client.get_neg_risk, token_id),
                "fee_exponent": await asyncio.to_thread(client.get_fee_exponent, token_id),
                "shares_held": str(await trader.token_shares(token_id)),
            }
        except Exception as exc:
            report["market"] = {"token_id": token_id, "error": str(exc)}
            verdict.append("this market's order rules could not be read")
    try:
        open_orders = await trader.open_orders()
        report["open_orders"] = len(open_orders)
    except Exception as exc:
        report["open_orders_error"] = type(exc).__name__
        verdict.append("authenticated open-order query failed")
    await trader.close()
    report["blocking_problems"] = verdict
    report["ready_for_live"] = not verdict
    report["readiness_scope"] = (
        "balance/allowance/API reads only; order signature and settlement not tested"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if not verdict else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "token_id",
        nargs="?",
        help="optional: an outcome token id to read tick size, neg risk and fee for",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.token_id)))
