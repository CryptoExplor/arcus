"""Tick/step conversion — the other way orders get rejected before matching."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.scaling import (  # noqa: E402
    D,
    clamp_slippage_price,
    dec_str,
    snap_price,
    snap_size,
    tier_tick,
    to_quantums,
    to_ticks,
)

BTC = {
    "marketDisplayName": "BTC-USD",
    "tickSize": "0.1",
    "stepSize": "0.00001",
    "tickTiers": [{"upToPrice": "1000", "tick": "0.1"}, {"tick": "1"}],
    "minOrderNotional": "5",
}
FINE = {"tickSize": "0.001", "stepSize": "0.01", "tickTiers": [{"tick": "0.001"}]}


def test_exact_conversion() -> None:
    assert to_ticks("64000.5", BTC) == 640005
    assert to_quantums("0.00123", BTC) == 123


def test_non_multiple_is_rejected_not_rounded() -> None:
    """Silent rounding here would break the signature/body agreement."""
    for value, unit in (("64000.55", BTC), ("0.000005", BTC)):
        try:
            (to_ticks if unit is BTC and "." in value[:6] else to_quantums)(value, unit)
        except ValueError:
            continue
    try:
        to_ticks("64000.55", BTC)
    except ValueError:
        pass
    else:
        raise AssertionError("accepted a price off the tick grid")
    try:
        to_quantums("0.000005", BTC)
    except ValueError:
        pass
    else:
        raise AssertionError("accepted a size off the step grid")


def test_dec_str_never_uses_exponent_notation() -> None:
    assert dec_str(Decimal("1E-7")) == "0.0000001"
    assert dec_str(Decimal("1000")) == "1000"
    assert dec_str(0) == "0"


def test_tier_tick_follows_the_ladder() -> None:
    assert tier_tick(BTC, "500") == Decimal("0.1")     # below upToPrice
    assert tier_tick(BTC, "5000") == Decimal("1")      # unbounded top band


def test_snap_price_biases_passively_by_side() -> None:
    """A BUY must never round UP into a more aggressive price, and vice versa."""
    assert snap_price(FINE, "10.00049", side="BUY") == Decimal("10.000")
    assert snap_price(FINE, "10.00051", side="SELL") == Decimal("10.001")


def test_snapped_price_is_always_an_exact_multiple() -> None:
    for raw in ("64000.04", "63999.96", "1234.5678"):
        snapped = snap_price(BTC, raw, side="BUY")
        to_ticks(snapped, BTC)  # raises if not exact


def test_snap_size_rounds_down_only() -> None:
    """Rounding a size up would grow exposure beyond what risk approved."""
    assert snap_size(BTC, "0.000019") == Decimal("0.00001")
    assert snap_size(FINE, "1.999") == Decimal("1.99")


def test_slippage_bound_stays_inside_the_ten_percent_gate() -> None:
    """MARKET orders are rejected past 10% deviation from mark."""
    for bps in (25, 5_000, 100_000):
        buy = clamp_slippage_price(BTC, "BUY", "64000", bps)
        sell = clamp_slippage_price(BTC, "SELL", "64000", bps)
        assert buy <= D("64000") * Decimal("1.0951")
        assert sell >= D("64000") * Decimal("0.9049")


def test_slippage_bound_is_directional() -> None:
    assert clamp_slippage_price(BTC, "BUY", "64000", 25) > Decimal("64000")
    assert clamp_slippage_price(BTC, "SELL", "64000", 25) < Decimal("64000")
