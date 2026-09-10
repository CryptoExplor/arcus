"""Capital allocation: budget -> sizing, and the guards around it."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.capital import (  # noqa: E402
    VENUE_MIN_NOTIONAL,
    apply_allocation,
    capital_mode_enabled,
    needs_resize,
    plan_capital,
)
from arcusbot.config import Config  # noqa: E402

D = Decimal


def cfg(**kw) -> Config:
    base = dict(capital_usd=D(0), capital_pct=D(0), reserve_usd=D(0),
                capital_utilisation=D("0.5"), capital_clips=3, leverage=3,
                capital_inventory_fraction=D("0.5"), max_drawdown_pct=D(0))
    base.update(kw)
    return Config(**base)


# --------------------------------------------------------------- mode switch --
def test_fixed_is_the_default_and_leaves_config_untouched() -> None:
    c = cfg(order_notional_usd=D(25), max_position_notional_usd=D(150))
    assert not capital_mode_enabled(c)
    alloc = plan_capital(c, D(1000), 2)
    assert alloc.mode == "fixed"
    assert alloc.order_notional_usd == D(25)
    apply_allocation(c, alloc)
    assert c.order_notional_usd == D(25)      # unchanged


@pytest.mark.parametrize("kw", [{"capital_usd": D(100)}, {"capital_pct": D(10)}])
def test_either_knob_enables_capital_mode(kw) -> None:
    assert capital_mode_enabled(cfg(**kw))


# ------------------------------------------------------------- the arithmetic --
def test_budget_flows_through_leverage_and_utilisation() -> None:
    # 500 deployable x 3 leverage x 0.5 utilisation = 750 exposure over 2 markets
    # -> 375/market -> /3 clips -> 125 per clip.
    alloc = plan_capital(cfg(capital_usd=D(500)), D(1000), 2)
    assert alloc.deployable_usd == D(500)
    assert alloc.exposure_budget_usd == D(750)
    assert alloc.per_market_usd == D("375")
    assert alloc.order_notional_usd == D("125.00")
    assert alloc.max_position_notional_usd == D("375.00")
    assert alloc.max_inventory_notional_usd == D("187.50")   # x0.5 fraction


def test_pct_is_taken_of_equity() -> None:
    alloc = plan_capital(cfg(capital_pct=D(25)), D(2000), 1)
    assert alloc.deployable_usd == D(500)


def test_more_markets_splits_the_same_budget() -> None:
    one = plan_capital(cfg(capital_usd=D(600)), D(10_000), 1)
    three = plan_capital(cfg(capital_usd=D(600)), D(10_000), 3)
    assert three.per_market_usd == one.per_market_usd / 3
    assert three.exposure_budget_usd == one.exposure_budget_usd


def test_utilisation_caps_leverage_use() -> None:
    full = plan_capital(cfg(capital_usd=D(100), capital_utilisation=D(1)), D(1000), 1)
    half = plan_capital(cfg(capital_usd=D(100), capital_utilisation=D("0.5")), D(1000), 1)
    assert half.exposure_budget_usd == full.exposure_budget_usd / 2


# ------------------------------------------------------------------- reserve --
def test_reserve_is_subtracted_before_anything_else() -> None:
    alloc = plan_capital(cfg(capital_usd=D(900), reserve_usd=D(400)), D(1000), 1)
    assert alloc.deployable_usd == D(600)          # 1000 - 400, not 900
    assert any("reserve" in n for n in alloc.notes)


def test_reserve_becomes_the_free_collateral_floor() -> None:
    """A reserve you are willing to trade into is not a reserve."""
    c = cfg(capital_usd=D(500), reserve_usd=D(300), min_free_collateral_usd=D(10))
    alloc = plan_capital(c, D(1000), 1)
    assert alloc.min_free_collateral_usd >= D(300)


def test_reserve_larger_than_equity_yields_no_budget() -> None:
    alloc = plan_capital(cfg(capital_usd=D(500), reserve_usd=D(5000)), D(1000), 1)
    assert alloc.deployable_usd == D(0)
    assert not alloc.sufficient


def test_both_budgets_set_uses_the_stricter() -> None:
    alloc = plan_capital(cfg(capital_usd=D(200), capital_pct=D(90)), D(1000), 1)
    assert alloc.budget_usd == D(200)
    assert any("using the lower" in n for n in alloc.notes)


# ---------------------------------------------------- venue minimum handling --
def test_clip_is_lifted_to_the_venue_minimum() -> None:
    """Better to run fewer, legal clips than many the venue rejects."""
    alloc = plan_capital(cfg(capital_usd=D(10), leverage=1, capital_clips=10), D(1000), 1)
    assert alloc.order_notional_usd == VENUE_MIN_NOTIONAL
    assert alloc.sufficient
    assert any("venue minimum" in n for n in alloc.notes)


def test_budget_too_small_for_one_clip_is_refused_with_advice() -> None:
    alloc = plan_capital(cfg(capital_usd=D(3), leverage=1), D(1000), 1)
    assert not alloc.sufficient
    note = " ".join(alloc.notes)
    assert "cannot fund even one" in note
    assert "BOT_CAPITAL_USD" in note          # tells the operator what to change


def test_caps_never_fall_below_one_clip() -> None:
    alloc = plan_capital(cfg(capital_usd=D(6), leverage=1, capital_clips=5), D(1000), 1)
    assert alloc.max_position_notional_usd >= alloc.order_notional_usd
    assert alloc.max_inventory_notional_usd >= alloc.order_notional_usd


# --------------------------------------------------------------- unfunded ----
@pytest.mark.parametrize("equity", [None, Decimal(0)])
def test_unknown_equity_falls_back_instead_of_guessing(equity) -> None:
    alloc = plan_capital(cfg(capital_pct=D(50)), equity, 1)
    assert alloc.mode == "unfunded"
    assert alloc.order_notional_usd == D(25)     # the fixed default
    assert any("equity is unknown" in n for n in alloc.notes)


def test_unfunded_plan_does_not_overwrite_config() -> None:
    c = cfg(capital_pct=D(50), order_notional_usd=D(25))
    apply_allocation(c, plan_capital(c, None, 1))
    assert c.order_notional_usd == D(25)


# ------------------------------------------------------------ loss limits ----
def test_drawdown_pct_scales_with_deployed_capital() -> None:
    alloc = plan_capital(cfg(capital_usd=D(400), max_drawdown_pct=D(5)), D(1000), 1)
    assert alloc.max_drawdown_usd == D("20.00")      # 5% of 400


def test_drawdown_usd_is_kept_when_pct_is_zero() -> None:
    alloc = plan_capital(cfg(capital_usd=D(400), max_drawdown_usd=D(33)), D(1000), 1)
    assert alloc.max_drawdown_usd == D(33)


# ---------------------------------------------------------------- applying ----
def test_apply_writes_every_derived_limit_into_config() -> None:
    c = cfg(capital_usd=D(500))
    alloc = plan_capital(c, D(1000), 2)
    apply_allocation(c, alloc)
    assert c.order_notional_usd == alloc.order_notional_usd
    assert c.max_position_notional_usd == alloc.max_position_notional_usd
    assert c.max_inventory_notional_usd == alloc.max_inventory_notional_usd
    assert c.min_free_collateral_usd == alloc.min_free_collateral_usd
    # And the result must still be internally consistent for the risk manager.
    assert c.max_position_notional_usd >= c.order_notional_usd


# ----------------------------------------------------------------- resizing --
def test_resize_only_past_the_threshold() -> None:
    alloc = plan_capital(cfg(capital_pct=D(50)), D(1000), 1)
    assert not needs_resize(alloc, D(1100), D(20))    # 10% drift
    assert needs_resize(alloc, D(1250), D(20))        # 25% up
    assert needs_resize(alloc, D(700), D(20))         # 30% down


def test_fixed_mode_never_resizes() -> None:
    alloc = plan_capital(cfg(), D(1000), 1)
    assert not needs_resize(alloc, D(100_000), D(20))


def test_threshold_zero_disables_resizing() -> None:
    alloc = plan_capital(cfg(capital_pct=D(50)), D(1000), 1)
    assert not needs_resize(alloc, D(99_999), D(0))


# ------------------------------------------------------------- validation ----
def test_config_rejects_nonsense_capital_settings() -> None:
    assert any("BOT_CAPITAL_PCT" in p for p in cfg(capital_pct=D(150)).validate())
    assert any("UTILISATION" in p for p in cfg(capital_utilisation=D(0)).validate())
    assert any("UTILISATION" in p for p in cfg(capital_utilisation=D(2)).validate())
    assert any("CLIPS" in p for p in cfg(capital_clips=0).validate())
    assert any("RESERVE" in p for p in cfg(reserve_usd=D(-1)).validate())


def test_capital_mode_skips_the_fixed_notional_check() -> None:
    """In capital mode the clip is derived later, so a low default is fine."""
    c = cfg(capital_usd=D(500), order_notional_usd=D(0))
    assert not [p for p in c.validate() if "BOT_ORDER_NOTIONAL_USD" in p]
    assert any("BOT_ORDER_NOTIONAL_USD" in p
               for p in cfg(order_notional_usd=D(0)).validate())
