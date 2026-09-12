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
            "slug": "market-child",
            "eventSlug": "market-parent",
            "outcome": "Yes",
            "name": "blackewolf83",
            "pseudonym": "Radiant-Metaphor",
        }
    ]
    client = client_for(lambda _: httpx.Response(200, json=payload))
    try:
        events = await client.get_activity("0x" + "1" * 40)
        assert events[0].trader_name == "blackewolf83"
        assert events[0].slug == "market-child"
        assert events[0].event_slug == "market-parent"
        assert events[0].received_at > 0
        assert events[0].received_monotonic > 0
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"price": "0.01", "side": "SELL"}, Decimal("0.01")),
        ({"price": "0.5", "side": ""}, None),
        ({"price": "NaN", "side": "BUY"}, None),
    ],
)
async def test_last_trade_mark_rejects_no_trade_sentinel_and_invalid_prices(payload, expected):
    def handler(request):
        assert request.url.path == "/last-trade-price"
        assert request.url.params["token_id"] == "11"
        return httpx.Response(200, json=payload)

    client = client_for(handler)
    try:
        assert await client.get_last_trade_price("11") == expected
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


def book_payload(token_id="11", **overrides):
    return {
        "asset_id": token_id,
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
        "tick_size": "0.01",
        "min_order_size": "5",
        **overrides,
    }


@pytest.mark.asyncio
async def test_order_limits_are_remembered_per_token_not_assumed():
    minimums = {"11": "5", "22": "100"}

    def handler(request):
        token = request.url.params["token_id"]
        return httpx.Response(
            200, json=book_payload(token, min_order_size=minimums[token], tick_size="0.001")
        )

    client = client_for(handler)
    try:
        assert client.market_limits("11") is None  # never guessed before a read
        for token in minimums:
            await client.get_book(token)
        assert client.market_limits("11").min_order_size == Decimal(5)
        assert client.market_limits("22").min_order_size == Decimal(100)
        assert client.market_limits("11").tick_size == Decimal("0.001")
        assert client.market_limits("11").seen_at > 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_market_level_minimum_is_reported_but_execution_keeps_the_book(capsys):
    def handler(request):
        if request.url.path == "/book":
            return httpx.Response(200, json=book_payload(min_order_size="5"))
        return httpx.Response(200, json=market(closed=False, minimum_order_size=15))

    client = client_for(handler)
    try:
        book = await client.get_book("11")
        await client.get_market(CONDITION)
        reported = capsys.readouterr().out
        assert "market_min_order_mismatch" in reported
        assert "market_min_order_size=15" in reported and "book_min_order_size=5" in reported
        # The book stays the number orders are checked against.
        assert book.min_order_size == Decimal(5)
        assert client.market_limits("11").min_order_size == Decimal(5)
        await client.get_market(CONDITION)
        # Reported once for the market, not on every copied trade.
        assert "market_min_order_mismatch" not in capsys.readouterr().out
    finally:
        await client.close()
