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
@pytest.mark.parametrize("rate", ["0", "0.05", "0.07"])
async def test_explicit_exchange_fee_including_zero(rate):
    def handler(request):
        assert request.url.path == f"/clob-markets/{CONDITION}"
        return httpx.Response(200, json={"c": CONDITION, "fd": {"r": rate, "e": 1}})

    client = client_for(handler)
    try:
        assert await client.get_fee_rate(CONDITION) == Decimal(rate)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"c": CONDITION},
        {"c": "wrong", "fd": {"r": 0.05, "e": 1}},
        {"c": CONDITION, "fd": {"r": 0.05, "e": 2}},
        {"c": CONDITION, "fd": {"r": "NaN", "e": 1}},
    ],
)
async def test_unverified_fee_is_an_error_not_five_percent(payload):
    client = client_for(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ValueError):
            await client.get_fee_rate(CONDITION, "Bitcoin")
        assert CONDITION not in client._fee_cache
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_fee_network_failure_does_not_fall_back():
    client = client_for(lambda _: httpx.Response(503))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_fee_rate(CONDITION)
    finally:
        await client.close()
