"""A stored NUMERIC keeps its scale; the panel must not show that scale."""

from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.formatting import cents, percent, plain
from app.price_limits import PriceRange, leader_price_range


@pytest.mark.parametrize(
    "stored, expected",
    [
        # What a NUMERIC(20, 10) column and a scaled zero actually hold.
        (("0E-10", "1.0000000000"), "0–100¢"),
        (("0.0100000000", "0.9900000000"), "1–99¢"),
        (("0.0200000000", "0.9800000000"), "2–98¢"),
        (("0.0050000000", "0.9950000000"), "0.5–99.5¢"),
    ],
)
def test_price_range_label_has_no_trailing_zeros_or_exponents(stored, expected):
    minimum, maximum = (D(value) for value in stored)
    assert PriceRange(minimum, maximum).label == expected
    assert (
        leader_price_range(SimpleNamespace(min_buy_price=minimum, max_buy_price=maximum)).label
        == expected
    )


@pytest.mark.parametrize(
    "stored, expected",
    [
        ("5.0000", "5"),
        ("0.5000", "0.5"),
        ("200.0000", "200"),
        ("1000", "1000"),
        ("12.3400", "12.34"),
    ],
)
def test_percent_drops_the_column_scale(stored, expected):
    assert percent(D(stored)) == expected


def test_cents_and_plain_round_instead_of_printing_noise():
    assert cents(D("0.025")) == "2.5"
    assert cents(D("0.0005")) == "0.05"
    # Rounded to the place shown, not truncated.
    assert cents(D("0.029999")) == "3"
    assert plain(D("1.50000")) == "1.5"
    assert plain(D("3")) == "3"


def test_unusable_values_are_a_dash_not_a_crash():
    assert plain(None) == "—"
    assert percent(None) == "—"
    assert cents(None) == "—"
    assert plain(D("NaN")) == "—"
    assert plain(D("Infinity")) == "—"
