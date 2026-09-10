"""Adaptive control: reacting to conditions without ever chasing losses."""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.adaptive import (  # noqa: E402
    MAX_EDGE_MULT,
    MAX_INTERVAL_MULT,
    MIN_EDGE_MULT,
    AdaptiveController,
)


def ctl(market: str = "BTC-USD") -> AdaptiveController:
    return AdaptiveController(market=market)


def profitable(c: AdaptiveController, quotes: int = 20, fills: int = 5) -> None:
    """Put a controller into a demonstrably profitable, benign state."""
    c.note_pnl(Decimal("0"))
    for _ in range(quotes):
        c.note_quote()
    for i in range(fills):
        c.note_fill("BUY", Decimal("64000"), Decimal("64000"), Decimal("4"))
    # Every fill was followed by the mid moving in our favour.
    c.note_mid(Decimal("64100"), settle_s=0.0)
    c.note_pnl(Decimal("5"))


# ------------------------------------------------------------- the core rule --


def test_never_speeds_up_while_losing() -> None:
    """The single most important guarantee in this module."""
    c = ctl()
    c.note_pnl(Decimal("10"))
    c.note_pnl(Decimal("-5"))
    adj = c.evaluate()
    assert adj.interval_multiplier > 1, "must slow down, not speed up, while losing"
    assert adj.edge_multiplier > 1, "must demand more edge while losing"
    assert any("losing" in r for r in adj.reasons)


def test_speeds_up_only_when_profitable_and_healthy() -> None:
    c = ctl()
    profitable(c)
    adj = c.evaluate()
    assert adj.interval_multiplier < 1
    assert any("increasing quote frequency" in r for r in adj.reasons)


def test_profitable_but_rate_limited_does_not_speed_up() -> None:
    c = ctl()
    profitable(c)
    c.note_rate_limited(5.0)
    adj = c.evaluate()
    assert adj.interval_multiplier > 1
    assert any("rate limited" in r for r in adj.reasons)


def test_profitable_but_erroring_does_not_speed_up() -> None:
    c = ctl()
    profitable(c)
    for _ in range(4):
        c.note_api_error()
    adj = c.evaluate()
    assert adj.interval_multiplier > 1
    assert any("API errors" in r for r in adj.reasons)


def test_profitable_but_adversely_selected_does_not_speed_up() -> None:
    c = ctl()
    c.note_pnl(Decimal("0"))
    for _ in range(20):
        c.note_quote()
    for _ in range(10):
        c.note_fill("BUY", Decimal("64000"), Decimal("64000"), Decimal("4"))
    c.note_mid(Decimal("63500"), settle_s=0.0)  # every buy immediately underwater
    c.note_pnl(Decimal("5"))
    adj = c.evaluate()
    assert adj.edge_multiplier > 1
    assert not any("increasing quote frequency" in r for r in adj.reasons)


# ------------------------------------------------------- adverse selection --


def test_adverse_move_is_measured_per_side() -> None:
    c = ctl()
    c.note_fill("BUY", Decimal("100"), Decimal("100"), Decimal("4"))
    c.note_mid(Decimal("99"), settle_s=0.0)  # price fell after we bought: bad
    assert c.adverse_selection_bps() > 0

    c2 = ctl()
    c2.note_fill("SELL", Decimal("100"), Decimal("100"), Decimal("4"))
    c2.note_mid(Decimal("99"), settle_s=0.0)  # price fell after we sold: good
    assert c2.adverse_selection_bps() < 0


def test_being_picked_off_widens_the_edge() -> None:
    c = ctl()
    for _ in range(10):
        c.note_quote()
        c.note_fill("BUY", Decimal("64000"), Decimal("64000"), Decimal("4"))
    c.note_mid(Decimal("63000"), settle_s=0.0)
    adj = c.evaluate()
    assert adj.edge_multiplier > 1
    assert any("moved against us" in r for r in adj.reasons)


def test_unresolved_fills_are_not_judged_prematurely() -> None:
    """A fill that has not settled yet must not count as adverse."""
    c = ctl()
    c.note_fill("BUY", Decimal("64000"), Decimal("64000"), Decimal("4"))
    c.note_mid(Decimal("60000"), settle_s=999)  # too soon to judge
    assert c.adverse_selection_bps() == 0


# -------------------------------------------------------- market conditions --


def test_high_volatility_against_a_narrow_spread_widens_the_edge() -> None:
    c = ctl()
    calm = c.evaluate(vol_bps=Decimal("2"), spread_bps=Decimal("10"))
    wild = c.evaluate(vol_bps=Decimal("40"), spread_bps=Decimal("10"))
    assert wild.edge_multiplier > calm.edge_multiplier


def test_thin_liquidity_widens_and_slows() -> None:
    c = ctl()
    adj = c.evaluate(depth_ratio=Decimal("1.5"))
    assert adj.edge_multiplier > 1 and adj.interval_multiplier > 1
    assert any("thin book" in r for r in adj.reasons)


def test_inventory_near_cap_slows_new_quoting() -> None:
    c = ctl()
    adj = c.evaluate(inventory_utilisation=Decimal("0.9"))
    assert adj.interval_multiplier > 1
    assert any("inventory near cap" in r for r in adj.reasons)


def test_old_inventory_widens_the_edge() -> None:
    c = ctl()
    adj = c.evaluate(inventory_age_s=600)
    assert adj.edge_multiplier > 1


def test_benign_conditions_are_nominal() -> None:
    c = ctl()
    adj = c.evaluate(vol_bps=Decimal("2"), spread_bps=Decimal("10"),
                     depth_ratio=Decimal("50"))
    assert adj.edge_multiplier == 1 and adj.interval_multiplier == 1
    assert "nominal" in adj.describe()


# ------------------------------------------------------------------ bounds --


def test_multipliers_are_bounded() -> None:
    """Stacked bad news must not produce an absurd edge or a frozen bot."""
    c = ctl()
    c.note_pnl(Decimal("100"))
    c.note_pnl(Decimal("-100"))
    for _ in range(20):
        c.note_quote()
        c.note_fill("BUY", Decimal("64000"), Decimal("64000"), Decimal("1"))
        c.note_api_error()
    c.note_mid(Decimal("50000"), settle_s=0.0)
    c.note_rate_limited(30)
    adj = c.evaluate(vol_bps=Decimal("500"), spread_bps=Decimal("1"),
                     depth_ratio=Decimal("0.1"), inventory_age_s=99999,
                     inventory_utilisation=Decimal("1"))
    assert MIN_EDGE_MULT <= adj.edge_multiplier <= MAX_EDGE_MULT
    assert adj.interval_multiplier <= MAX_INTERVAL_MULT


def test_edge_multiplier_never_goes_below_the_floor() -> None:
    c = ctl()
    profitable(c)
    adj = c.evaluate()
    assert adj.edge_multiplier >= MIN_EDGE_MULT


def test_fill_rate_reporting() -> None:
    c = ctl()
    assert c.fill_rate() == 0
    for _ in range(10):
        c.note_quote()
    for _ in range(2):
        c.note_fill("BUY", Decimal("1"), Decimal("1"), Decimal("4"))
    assert c.fill_rate() == Decimal("0.2")


def test_adjustment_is_serialisable_for_the_dashboard() -> None:
    c = ctl()
    c.note_pnl(Decimal("1"))
    payload = c.evaluate().as_dict()
    for key in ("edgeMultiplier", "intervalMultiplier", "fillRate", "adverseBps"):
        assert key in payload
