import asyncio
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.polymarket import PolymarketClient

CONDITION = "0x" + "a" * 64


def client_for(handler):
    client = PolymarketClient(Settings(_env_file=None))
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def market(**overrides):
    return {
        "condition_id": CONDITION,
        "closed": True,
        "tokens": [
            {"token_id": "11", "outcome": "Up", "winner": True, "price": 1},
            {"token_id": "22", "outcome": "Down", "winner": False, "price": 0},
        ],
        **overrides,
    }


@pytest.mark.asyncio
async def test_activity_includes_public_polymarket_name():
    payload = [
        {
            "type": "TRADE",
            "asset": "11",
            "side": "BUY",
            "transactionHash": "0xtrade",
            "timestamp": 123,
            "conditionId": CONDITION,
            "size": 5,
            "price": 0.4,
            "title": "Market",
            "outcome": "Yes",
            "name": "blackewolf83",
            "pseudonym": "Radiant-Metaphor",
        }
    ]
    client = client_for(lambda _: httpx.Response(200, json=payload))
    try:
        events = await client.get_activity("0x" + "1" * 40)
        assert events[0].trader_name == "blackewolf83"
        assert events[0].received_at > 0
        assert events[0].received_monotonic > 0
    finally:
        await client.close()


async def test_concurrent_metadata_is_shared_but_closed_status_is_not_cached():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json=market(closed=calls > 1))

    client = client_for(handler)
    try:
        first = asyncio.create_task(client.get_market(CONDITION))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(client.get_market(CONDITION))
        await asyncio.sleep(0)
        release.set()
        a, b = await asyncio.gather(first, second)
        assert calls == 1
        assert a["closed"] is False and b["closed"] is False
        assert (await client.get_market(CONDITION))["closed"] is True
        assert calls == 2
    finally:
        release.set()
        await client.close()


@pytest.mark.asyncio
async def test_resolution_uses_verified_clob_path_and_token():
    def handler(request):
        assert request.url.path == f"/markets/{CONDITION}"
        assert not request.url.query
        return httpx.Response(200, json=market())

    client = client_for(handler)
    try:
        assert await client.get_resolution(CONDITION, "Up", "11") == 1
        assert await client.get_resolution(CONDITION, "Down", "22") == 0
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [market(condition_id="wrong"), market(tokens=[])])
async def test_wrong_market_or_token_never_settles(payload):
    client = client_for(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ValueError):
            await client.get_resolution(CONDITION, "Up", "11")
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [True, False])
async def test_price_one_without_winner_is_not_resolution(closed):
    payload = market(closed=closed)
    for token in payload["tokens"]:
        token["winner"] = False
    client = client_for(lambda _: httpx.Response(200, json=payload))
    try:
        assert await client.get_resolution(CONDITION, "Up", "11") is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_explicit_split_resolution():
    client = client_for(lambda _: httpx.Response(200, json=market(is_50_50_outcome=True)))
    try:
        assert await client.get_resolution(CONDITION, "Up", "11") == Decimal("0.5")
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("rate", "exponent"), [("0", 1), ("0.05", 1), ("0.07", 2)])
async def test_explicit_exchange_fee_including_zero_and_supported_exponents(rate, exponent):
    def handler(request):
        assert request.url.path == f"/clob-markets/{CONDITION}"
        return httpx.Response(200, json={"c": CONDITION, "fd": {"r": rate, "e": exponent}})

    client = client_for(handler)
    try:
        assert await client.get_fee_rate(CONDITION) == Decimal(rate)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wrong_fee_market_identity_is_an_error():
    payload = {"c": "wrong", "fd": {"r": 0.05, "e": 1}}
    client = client_for(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ValueError):
            await client.get_fee_rate(CONDITION, "Bitcoin")
        assert CONDITION not in client._fee_cache
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"c": CONDITION},
        {"c": CONDITION, "fd": {"r": 0.05, "e": 0}},
        {"c": CONDITION, "fd": {"r": "NaN", "e": 1}},
    ],
)
async def test_missing_or_invalid_fee_schedule_uses_conservative_fallback(payload):
    client = client_for(lambda _: httpx.Response(200, json=payload))
    try:
        assert await client.get_fee_rate(CONDITION, "Bitcoin") == Decimal("0.07")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_fee_network_failure_uses_conservative_fallback():
    client = client_for(lambda _: httpx.Response(503))
    try:
        assert await client.get_fee_rate(CONDITION, "Bitcoin Up or Down") == Decimal("0.07")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sports_fee_network_fallback_matches_documented_rate():
    client = client_for(lambda _: httpx.Response(503))
    try:
        assert await client.get_fee_rate(CONDITION, "Will Barrow AFC win?") == Decimal("0.03")
    finally:
        await client.close()
