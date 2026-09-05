"""Read-only settlement/fee check: python -m app.audit [condition_id ...]."""

import argparse
import asyncio
import json

from .config import get_settings
from .polymarket import PolymarketClient


async def check(condition_ids: list[str]) -> int:
    if not condition_ids:
        from .db import SessionLocal, engine
        from .repository import positions

        async with SessionLocal() as session:
            condition_ids = list(dict.fromkeys(p.condition_id for p in await positions(session)))
        await engine.dispose()
    client = PolymarketClient(get_settings())
    failures = 0
    try:
        for condition in condition_ids:
            result = {"requested_condition_id": condition}
            try:
                market = await client.get_market(condition)
                result.update(
                    returned_condition_id=market["condition_id"],
                    question=market.get("question"),
                    url=f"https://polymarket.com/event/{market.get('market_slug', '')}",
                    closed=market.get("closed"),
                    tokens=market.get("tokens"),
                    seconds_delay=market.get("seconds_delay"),
                )
                result["fee_rate"] = str(await client.get_fee_rate(condition))
                result["payouts"] = {
                    token["token_id"]: str(
                        await client.get_resolution(condition, token["outcome"], token["token_id"])
                    )
                    for token in market.get("tokens", [])
                }
            except Exception as exc:
                result["error"] = str(exc)
                failures += 1
            print(json.dumps(result, ensure_ascii=True, indent=2))
    finally:
        await client.close()
    return int(failures > 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("condition_id", nargs="*")
    raise SystemExit(asyncio.run(check(parser.parse_args().condition_id)))
