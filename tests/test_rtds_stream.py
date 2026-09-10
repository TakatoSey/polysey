"""The RTDS feed carries every Polymarket trade, not only our leaders'."""

import json
from decimal import Decimal as D
from unittest.mock import AsyncMock

from app.rtds import UNTRACKED, RTDSTradeStream

TRACKED = "0x" + "1" * 40
OTHER = "0x" + "2" * 40


def trade(address=TRACKED, **changes):
    payload = {
        "asset": "123456789",
        "conditionId": "0x" + "a" * 64,
        "proxyWallet": address,
        "transactionHash": "0x" + "b" * 64,
        "side": "BUY",
        "timestamp": 1788876280,
        "size": "4",
        "price": "0.93",
        "title": "Bitcoin Up or Down",
        "slug": "btc-updown-5m",
    }
    payload.update(changes)
    return payload


def frame(*payloads):
    return json.dumps({"topic": "activity", "type": "trades", "payload": list(payloads)})


def stream():
    return RTDSTradeStream(AsyncMock(return_value=None), {TRACKED})


def test_somebody_elses_trade_is_untracked_not_invalid():
    assert RTDSTradeStream._parse(trade(OTHER), tracked_addresses={TRACKED}) is UNTRACKED


def test_a_malformed_payload_is_invalid():
    assert RTDSTradeStream._parse(trade(price="7"), tracked_addresses={TRACKED}) is None
    assert RTDSTradeStream._parse({"asset": "1"}, tracked_addresses={TRACKED}) is None


async def test_counters_separate_other_traders_from_broken_payloads():
    feed = stream()

    await feed.handle_message(frame(trade(), trade(OTHER), trade(OTHER), trade(side="HOLD")))

    assert feed.counters["payloads"] == 4
    assert feed.counters["parsed"] == 1
    # Three quarters of a global feed being other people is normal operation.
    assert feed.counters["untracked"] == 2
    assert feed.counters["invalid"] == 1
    assert "invalid_trades" not in feed.counters


async def test_a_tracked_trade_reaches_the_callback_with_its_economic_fields():
    feed = stream()

    await feed.handle_message(frame(trade()))

    event = feed.on_trade.await_args.args[0]
    assert event.trader_address == TRACKED
    assert (event.side, event.size, event.price) == ("BUY", D(4), D("0.93"))
    assert event.source == "rtds"
    assert feed.counters["delivered"] == 1
