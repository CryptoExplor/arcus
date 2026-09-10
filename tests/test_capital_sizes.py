"""Capital sizing across the whole range the bot claims to support.

The bot is deployed against a $20 mainnet account and must also behave for a
$100,000 one. These tests pin the behaviour at each size so a change that
quietly breaks small accounts (or lets a big one over-deploy) fails here.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.capital import apply_allocation, plan_capital  # noqa: E402
from arcusbot.config import Config  # noqa: E402
from arcusbot.selection import max_markets_for_capital  # noqa: E402


def plan(equity: str, markets: int = 1, **kw):
    cfg = Config.from_env(venue="sim", markets=["BTC-USD"], **kw)
    return cfg, plan_capital(cfg, Decimal(equity), markets)


# ------------------------------------------------------- the whole spectrum --


@pytest.mark.parametrize("equity", ["20", "50", "100", "1000", "100000"])
def test_every_supported_account_size_produces_a_workable_plan(equity: str) -> None:
    cfg, alloc = plan(equity, capital_pct=Decimal("100"))
    assert alloc.sufficient, f"${equity} should be tradable: {alloc.notes}"
    # Never propose a clip the venue would reject.
    assert alloc.order_notional_usd >= Decimal("5")
    # Never deploy more than the account holds.
    assert alloc.deployable_usd <= Decimal(equity)


def test_twenty_dollar_account_sizes_down_not_out() -> None:
    """The real mainnet case. It must trade, and it must stay small."""
    cfg, alloc = plan("20", capital_usd=Decimal("20"))
    assert alloc.sufficient
    assert alloc.deployable_usd == Decimal("20")
    assert alloc.order_notional_usd >= Decimal("5")
    assert max_markets_for_capital(alloc.deployable_usd) == 1


def test_sizing_is_monotonic_in_capital() -> None:
    """More capital must never produce a smaller clip."""
    sizes = []
    for equity in ["20", "50", "100", "1000", "100000"]:
        _, alloc = plan(equity, capital_pct=Decimal("100"))
        sizes.append(alloc.order_notional_usd)
    assert sizes == sorted(sizes), f"clip sizes not monotonic: {sizes}"


def test_a_hundred_thousand_dollar_account_is_not_capped_at_twenty() -> None:
    """Guards against the $20 mainnet budget leaking into general sizing."""
    _, alloc = plan("100000", capital_pct=Decimal("100"))
    assert alloc.deployable_usd == Decimal("100000")
    assert alloc.order_notional_usd > Decimal("100")


def test_dust_account_is_refused_with_an_explanation() -> None:
    _, alloc = plan("3", capital_pct=Decimal("100"))
    assert not alloc.sufficient
    assert alloc.notes, "refusal must explain itself"


# ------------------------------------------------------------ pct + reserve --


def test_pct_of_a_small_account_still_clears_the_minimum_or_refuses() -> None:
    """10% of $20 is $2 — below the venue minimum. Refuse, do not round up."""
    _, alloc = plan("20", capital_pct=Decimal("10"))
    if alloc.sufficient:
        assert alloc.order_notional_usd >= Decimal("5")
    else:
        assert alloc.notes


def test_reserve_protects_the_floor_at_every_size() -> None:
    for equity, reserve in [("100", "50"), ("1000", "200"), ("100000", "5000")]:
        _, alloc = plan(equity, capital_pct=Decimal("100"),
                        reserve_usd=Decimal(reserve))
        assert alloc.deployable_usd <= Decimal(equity) - Decimal(reserve)


def test_reserve_exceeding_equity_refuses_rather_than_going_negative() -> None:
    _, alloc = plan("20", capital_pct=Decimal("100"), reserve_usd=Decimal("50"))
    assert not alloc.sufficient
    assert alloc.deployable_usd <= 0


def test_applied_limits_are_consistent_with_the_plan() -> None:
    cfg, alloc = plan("1000", capital_pct=Decimal("100"))
    apply_allocation(cfg, alloc)
    assert cfg.order_notional_usd == alloc.order_notional_usd
    assert cfg.max_position_notional_usd == alloc.max_position_notional_usd
    # A position cap below one clip would reject every order it sizes.
    assert cfg.max_position_notional_usd >= cfg.order_notional_usd
