"""Decimal <-> engine-integer conversion and market-grid snapping.

Arcus signs prices as integer *ticks* and sizes as integer *quantums*:

    p = price / market.tickSize      (must divide exactly)
    q = size  / market.stepSize      (must divide exactly)

The divisor is ALWAYS the market's top-level ``tickSize`` even when the price
falls in a coarser ``tickTiers`` band. Tiers only constrain which prices the
engine *accepts*: a price in a coarser band must additionally be a multiple of
that band's tick, or the order is rejected with
``... is not a multiple of tick size ... for its price tier``.

Reference: https://docs.arcus.xyz/api-reference/authentication
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Mapping

__all__ = [
    "D",
    "dec_str",
    "to_ticks",
    "to_quantums",
    "tier_tick",
    "snap_price",
    "snap_size",
    "clamp_slippage_price",
]


def D(value: Any) -> Decimal:
    """Decimal from anything, without binary-float surprises."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def dec_str(value: Any) -> str:
    """Plain-decimal string ('0.0000001', never '1E-7').

    The REST body carries ``price`` / ``quantity`` as decimal strings and the
    engine rejects exponent notation, so every wire value goes through here.
    """
    d = D(value)
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    # format() keeps a trailing '.' on some normalizations
    return s.rstrip(".") if "." in s else s


def _exact_div(value: Any, unit: Any, what: str) -> int:
    n = D(value) / D(unit)
    if n != n.to_integral_value():
        raise ValueError(f"{what} {dec_str(value)} is not a multiple of {dec_str(unit)}")
    return int(n)


def to_ticks(price: Any, market: Mapping[str, Any]) -> int:
    """price -> integer ticks using the market's TOP-LEVEL tickSize."""
    return _exact_div(price, market["tickSize"], "price")


def to_quantums(size: Any, market: Mapping[str, Any]) -> int:
    """size -> integer quantums using the market's stepSize."""
    return _exact_div(size, market["stepSize"], "size")


def tier_tick(market: Mapping[str, Any], price: Any) -> Decimal:
    """The tick a limit price must align to, given the market's tickTiers ladder.

    Bands ascend by ``upToPrice`` (exclusive upper bound); the last band is
    unbounded. Falls back to ``tickSize`` when no tiers are published.
    """
    p = D(price)
    base = D(market["tickSize"])
    tiers = market.get("tickTiers") or []
    for tier in tiers:
        tick = D(tier["tick"])
        upto = tier.get("upToPrice")
        if upto is None or p < D(upto):
            # A tier tick must remain an integer multiple of tickSize, otherwise
            # the signed integer and the accepted grid cannot both be satisfied.
            if (tick / base) != (tick / base).to_integral_value():
                return base
            return tick
    return base


def snap_price(market: Mapping[str, Any], price: Any, *, side: str | None = None) -> Decimal:
    """Snap a price onto the market's accepted grid for its tier.

    ``side`` biases the rounding so a snapped quote never becomes more
    aggressive than intended: BUY rounds down, SELL rounds up, None rounds
    half-up. Snapping is applied twice because rounding can move the price into
    a neighbouring tick tier.
    """
    p = D(price)
    for _ in range(3):
        tick = tier_tick(market, p)
        if side == "BUY":
            rounding = ROUND_FLOOR
        elif side == "SELL":
            rounding = ROUND_CEILING
        else:
            rounding = ROUND_HALF_UP
        snapped = (p / tick).to_integral_value(rounding=rounding) * tick
        if snapped == p:
            break
        p = snapped
    return p


def snap_size(market: Mapping[str, Any], size: Any) -> Decimal:
    """Round a size DOWN to the market step (never grow exposure by rounding)."""
    step = D(market["stepSize"])
    return (D(size) / step).to_integral_value(rounding=ROUND_DOWN) * step


def clamp_slippage_price(
    market: Mapping[str, Any],
    side: str,
    reference_price: Any,
    slippage_bps: int,
    *,
    max_deviation_pct: Decimal = Decimal("0.095"),
) -> Decimal:
    """Protective limit price for an aggressive (taker) order.

    A MARKET order must carry a ``price`` within 10% of the mark price, and an
    aggressive LIMIT is subject to the ``OracleDeviation`` check, so the bound
    is clamped to 9.5% by default — comfortably inside the engine's 10%.
    """
    ref = D(reference_price)
    bps = D(slippage_bps) / D(10_000)
    bps = min(bps, max_deviation_pct)
    raw = ref * (D(1) + bps) if side == "BUY" else ref * (D(1) - bps)
    return snap_price(market, raw, side="SELL" if side == "BUY" else "BUY")
