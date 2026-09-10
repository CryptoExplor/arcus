"""Market selection: refusing to trade where a maker strategy cannot work."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.selection import (  # noqa: E402
    MAX_CONCURRENT_MARKETS,
    max_markets_for_capital,
    rank_markets,
    score_market,
    select_markets,
)
from arcusbot.sim import SIM_MARKETS  # noqa: E402


def meta(**overrides) -> dict:
    base = {
        "marketDisplayName": "BTC-USD", "marketId": 1, "status": "ONLINE",
        "tickSize": "0.1", "stepSize": "0.00001",
        "minOrderNotional": "5", "minOrderSize": "0.0001",
        "markPrice": "64000",
    }
    base.update(overrides)
    return base


GOOD = {"spread_bps": Decimal("10"), "depth_usd": Decimal("50000"),
        "vol_bps": Decimal("5"), "required_edge_bps": Decimal("3")}


# ------------------------------------------------------------ hard rejects --


def test_offline_market_is_rejected() -> None:
    c = score_market(meta(status="HALTED"), per_market_budget=Decimal("100"), **GOOD)
    assert not c.tradable and "HALTED" in c.reasons[0]


def test_market_without_a_mark_price_is_rejected() -> None:
    """markPrice '0' means unavailable — never substitute the oracle."""
    c = score_market(meta(markPrice="0"), per_market_budget=Decimal("100"), **GOOD)
    assert not c.tradable and "mark price" in c.reasons[0]


def test_spread_below_the_required_edge_is_rejected() -> None:
    c = score_market(meta(), per_market_budget=Decimal("100"),
                     spread_bps=Decimal("2"), depth_usd=Decimal("50000"),
                     vol_bps=Decimal("5"), required_edge_bps=Decimal("3"))
    assert not c.tradable and "required edge" in c.reasons[0]


def test_coarse_tick_is_rejected() -> None:
    """If one tick is wider than the edge, the edge cannot be priced."""
    c = score_market(meta(tickSize="100"), per_market_budget=Decimal("100"), **GOOD)
    assert not c.tradable and "tick" in c.reasons[-1]


def test_minimum_clip_larger_than_the_budget_is_rejected() -> None:
    c = score_market(meta(minOrderNotional="50"), per_market_budget=Decimal("20"), **GOOD)
    assert not c.tradable and "exceeds the per-market budget" in c.reasons[0]


def test_minimum_clip_eating_the_budget_is_rejected() -> None:
    """$5 minimum against an $8 budget leaves no room to scale."""
    c = score_market(meta(), per_market_budget=Decimal("8"), **GOOD)
    assert not c.tradable and "no room to scale" in c.reasons[0]


# ------------------------------------------------------------------ scoring --


def test_a_healthy_market_scores_well() -> None:
    c = score_market(meta(), per_market_budget=Decimal("500"), **GOOD)
    assert c.tradable and c.score > Decimal("0.5")


def test_wider_spread_scores_higher_all_else_equal() -> None:
    tight = score_market(meta(), per_market_budget=Decimal("500"),
                         spread_bps=Decimal("4"), depth_usd=Decimal("50000"),
                         vol_bps=Decimal("5"), required_edge_bps=Decimal("3"))
    wide = score_market(meta(), per_market_budget=Decimal("500"),
                        spread_bps=Decimal("15"), depth_usd=Decimal("50000"),
                        vol_bps=Decimal("5"), required_edge_bps=Decimal("3"))
    assert wide.score > tight.score


def test_thin_book_is_flagged_and_scores_lower() -> None:
    thin = score_market(meta(), per_market_budget=Decimal("500"),
                        spread_bps=Decimal("10"), depth_usd=Decimal("600"),
                        vol_bps=Decimal("5"), required_edge_bps=Decimal("3"))
    deep = score_market(meta(), per_market_budget=Decimal("500"),
                        spread_bps=Decimal("10"), depth_usd=Decimal("100000"),
                        vol_bps=Decimal("5"), required_edge_bps=Decimal("3"))
    assert thin.score < deep.score
    assert any("thin" in r for r in thin.reasons)


def test_volatility_dwarfing_the_spread_is_penalised() -> None:
    """High vol against a narrow spread is adverse selection, not opportunity."""
    calm = score_market(meta(), per_market_budget=Decimal("500"),
                        spread_bps=Decimal("10"), depth_usd=Decimal("50000"),
                        vol_bps=Decimal("6"), required_edge_bps=Decimal("3"))
    wild = score_market(meta(), per_market_budget=Decimal("500"),
                        spread_bps=Decimal("10"), depth_usd=Decimal("50000"),
                        vol_bps=Decimal("60"), required_edge_bps=Decimal("3"))
    assert wild.score < calm.score
    assert any("adverse selection" in r for r in wild.reasons)


def test_missing_observations_are_flagged_not_assumed_good() -> None:
    c = score_market(meta(), per_market_budget=Decimal("500"))
    assert c.tradable
    assert any("metadata only" in r for r in c.reasons)
    observed = score_market(meta(), per_market_budget=Decimal("500"), **GOOD)
    assert observed.score > c.score, "an observed good market must outrank an unknown one"


# -------------------------------------------------------- capacity by size --


def test_twenty_dollars_gets_exactly_one_market() -> None:
    assert max_markets_for_capital(Decimal("20")) == 1


def test_market_count_grows_with_capital_but_is_capped() -> None:
    assert max_markets_for_capital(Decimal("50")) >= 2
    assert max_markets_for_capital(Decimal("1000")) == MAX_CONCURRENT_MARKETS
    assert max_markets_for_capital(Decimal("1000000")) == MAX_CONCURRENT_MARKETS


def test_capital_below_one_clip_supports_no_markets() -> None:
    assert max_markets_for_capital(Decimal("4")) == 0
    assert max_markets_for_capital(Decimal("0")) == 0


# ---------------------------------------------------------------- selection --


def test_selection_concentrates_a_small_account() -> None:
    obs = {"BTC-USD": {"spreadBps": Decimal("8"), "depthUsd": Decimal("50000"),
                       "volBps": Decimal("5")},
           "ETH-USD": {"spreadBps": Decimal("12"), "depthUsd": Decimal("20000"),
                       "volBps": Decimal("6")}}
    chosen, _ = select_markets(SIM_MARKETS.values(), deployable=Decimal("20"),
                               observations=obs)
    assert len(chosen) == 1, "a $20 account must not be spread across markets"


def test_selection_expands_for_a_large_account() -> None:
    chosen, _ = select_markets(SIM_MARKETS.values(), deployable=Decimal("100000"))
    assert len(chosen) > 1


def test_requested_markets_are_a_filter_not_an_override() -> None:
    """Asking for a market does not make it tradable."""
    metas = [meta(marketDisplayName="BTC-USD", status="HALTED")]
    chosen, ranked = select_markets(metas, deployable=Decimal("1000"),
                                    requested=["BTC-USD"])
    assert chosen == []
    assert ranked and not ranked[0].tradable


def test_requested_markets_restrict_the_universe() -> None:
    chosen, _ = select_markets(SIM_MARKETS.values(), deployable=Decimal("100000"),
                               requested=["ETH-USD"])
    assert chosen == ["ETH-USD"]


def test_ranking_puts_tradable_markets_first() -> None:
    metas = [meta(marketDisplayName="DEAD", marketId=9, status="HALTED"),
             meta(marketDisplayName="GOOD", marketId=1)]
    ranked = rank_markets(metas, per_market_budget=Decimal("500"),
                          observations={"GOOD": {"spreadBps": Decimal("10"),
                                                 "depthUsd": Decimal("50000"),
                                                 "volBps": Decimal("5")}})
    assert ranked[0].market == "GOOD" and ranked[0].tradable
    assert not ranked[-1].tradable


def test_every_candidate_explains_itself() -> None:
    _, ranked = select_markets(SIM_MARKETS.values(), deployable=Decimal("1000"))
    for c in ranked:
        payload = c.as_dict()
        assert "score" in payload and "reasons" in payload and "metrics" in payload
