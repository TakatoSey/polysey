"""V2 compatibility tests use the real pinned signer and mocked HTTP only."""

from decimal import Decimal as D

import pytest
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import MarketOrderArgs, PartialCreateOrderOptions
from test_live_trading import live_settings, trader_for

from app.live import LiveTrader, LiveTradingUnavailable, build_client, contract_addresses


@pytest.mark.parametrize("signature_type", [0, 1, 2, 3])
@pytest.mark.parametrize("neg_risk", [False, True])
def test_real_sdk_signs_v2_with_fee_budget_and_correct_wallet(signature_type, neg_risk):
    funder = "0x" + "22" * 20
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key="0x" + "11" * 32,
        signature_type=signature_type,
        funder=funder,
    )

    def mock_get(url, **kwargs):
        if url.endswith("/version"):
            return {"version": 2}
        if "/markets-by-token/" in url:
            return {"condition_id": "condition"}
        return {"t": [{"t": "123"}], "mts": "0.01", "nr": neg_risk, "fd": {"r": 0.02, "e": 1}}

    client._get = mock_get
    signed = client.create_market_order(
        MarketOrderArgs(token_id="123", amount=5, side="BUY", price=0.5, user_usdc_balance=5),
        PartialCreateOrderOptions(neg_risk=neg_risk),
    )
    LiveTrader._check_v2_signature(signed)
    assert signed.maker.lower() == funder
    assert signed.signer.lower() == (
        funder if signature_type == 3 else client.get_address().lower()
    )
    assert int(signed.signatureType) == signature_type
    assert 0 < int(signed.makerAmount) < 5_000_000  # fee included in $5 budget
    assert not hasattr(signed, "feeRateBps")
    assert not hasattr(signed, "nonce")


def test_contracts_are_v2_and_pusd():
    c = contract_addresses(137)
    assert c["collateral_contract"].lower() == "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
    assert c["exchange_contract"].lower() == "0xe111180000d2663c0091e4f400237545b87b996b"
    assert c["neg_risk_exchange_contract"].lower() == "0xe2222d279d744050d28e00520010520000310f59"


def test_factory_uses_v2_auth_method(monkeypatch):
    from py_clob_client_v2.clob_types import ApiCreds

    creds = ApiCreds(api_key="test", api_secret="test", api_passphrase="test")
    monkeypatch.setattr(ClobClient, "create_or_derive_api_key", lambda self: creds)
    client = build_client(live_settings(POLYMARKET_SIGNATURE_TYPE=3))
    assert isinstance(client, ClobClient)
    assert client.creds is creds
    assert int(client.builder.signature_type) == 3


def test_legacy_signature_cannot_be_posted():
    from types import SimpleNamespace

    with pytest.raises(LiveTradingUnavailable, match="non-V2"):
        LiveTrader._check_v2_signature(SimpleNamespace(nonce=0, feeRateBps=0))


async def test_allowance_map_uses_v2_spenders_not_legacy(monkeypatch):
    c = contract_addresses(137)
    trader, _ = await trader_for(
        monkeypatch,
        balance={
            "balance": "13140000",
            "allowances": {
                c["exchange_contract"].lower(): "20000000",
                c["neg_risk_exchange_contract"]: "15000000",
                "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E": "999999999999",
            },
        },
    )
    assert trader.account.cash == D("13.14")
    assert trader.account.allowance == 15


async def test_bad_balance_is_not_silently_zero(monkeypatch):
    with pytest.raises(LiveTradingUnavailable):
        await trader_for(monkeypatch, balance={"error": "unauthorized"})


async def test_buy_passes_all_in_cap_to_sdk(monkeypatch):
    trader, fake = await trader_for(monkeypatch)
    await trader.buy("token", D(2), D("0.5"))
    assert fake.posted[0][0].args.user_usdc_balance == 2


async def test_missing_trade_data_does_not_use_limit_as_fill_price(monkeypatch):
    trader, _ = await trader_for(
        monkeypatch,
        post={"success": True, "orderID": "o"},
        order={"status": "MATCHED", "size_matched": "5", "price": "0.9"},
    )
    result = await trader.buy("token", D(5), D("0.9"))
    assert not result.confirmed
    assert result.average_price == 0


def test_deposit_wallet_requires_funder():
    assert not live_settings(POLYMARKET_SIGNATURE_TYPE=3).live_problems()
    assert live_settings(POLYMARKET_SIGNATURE_TYPE=3, POLYMARKET_FUNDER=None).live_problems()
