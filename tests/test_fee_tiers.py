"""Fee tier selection, progression and the rebate edge case.

Fee tiers are not cosmetic: at high volume maker fees go negative, which
changes the sign of the round-trip cost and therefore the spread the quoter is
willing to post. The floor that stops that from collapsing to zero is the
subject of the last group of tests.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.book import BookState, MarketState  # noqa: E402
from arcusbot.config import Config  # noqa: E402
from arcusbot.pnl import FeeSchedule, PnLTracker  # noqa: E402
from arcusbot.strategy import MarketWorker  # noqa: E402

D = Decimal

TIERS = {"tiers": [
    {"level": 0, "name": "Base", "volumeThreshold": 0, "makerFeePpm": 150, "takerFeePpm": 400},
    {"level": 1, "name": "VIP1", "volumeThreshold": 1_000_000, "makerFeePpm": 50, "takerFeePpm": 300},
    {"level": 2, "name": "VIP2", "volumeThreshold": 10_000_000, "makerFeePpm": -50, "takerFeePpm": 250},
]}


# ------------------------------------------------------------- selection -----
def test_base_tier_when_no_volume() -> None:
    f = FeeSchedule.from_fee_tiers(TIERS, D(0))
    assert (f.level, f.maker_bps, f.taker_bps) == (0, D("1.5"), D(4))


def test_tier_upgrades_at_the_threshold() -> None:
    assert FeeSchedule.from_fee_tiers(TIERS, D(999_999)).level == 0
    assert FeeSchedule.from_fee_tiers(TIERS, D(1_000_000)).level == 1
    assert FeeSchedule.from_fee_tiers(TIERS, D(10_000_000)).level == 2


def test_unsorted_tier_table_is_handled() -> None:
    shuffled = {"tiers": list(reversed(TIERS["tiers"]))}
    assert FeeSchedule.from_fee_tiers(shuffled, D(2_000_000)).level == 1


def test_missing_volume_assumes_the_base_tier() -> None:
    """Never assume a discount we have not earned."""
    assert FeeSchedule.from_fee_tiers(TIERS, None).level == 0


def test_empty_payload_falls_back_to_conservative_defaults() -> None:
    f = FeeSchedule.from_fee_tiers({}, D(0))
    assert f.source == "default"
    assert f.maker_bps == D(2) and f.taker_bps == D(5)


# ----------------------------------------------------------- progression -----
def test_next_tier_reports_distance_and_prize() -> None:
    p = FeeSchedule.from_fee_tiers(TIERS, D(500_000)).next_tier_progress()
    assert p["name"] == "VIP1"
    assert p["remainingVolumeUsd"] == "500000"
    assert D(p["pctComplete"]) == D(50)
    # 1.5 -> 0.5 bps maker, on two legs = 2 bps saved per round trip.
    assert p["savingBpsPerRoundTrip"] == "2"


def test_next_tier_flags_the_rebate_unlock() -> None:
    p = FeeSchedule.from_fee_tiers(TIERS, D(2_000_000)).next_tier_progress()
    assert p["name"] == "VIP2"
    assert p["unlocksMakerRebate"] is True


def test_top_tier_has_no_next() -> None:
    assert FeeSchedule.from_fee_tiers(TIERS, D(50_000_000)).next_tier_progress() is None


def test_progress_is_exposed_in_the_dict() -> None:
    d = FeeSchedule.from_fee_tiers(TIERS, D(500_000)).as_dict()
    assert d["next_tier"]["name"] == "VIP1"
    assert d["maker_is_rebate"] is False
    assert d["volume_30d_usd"] == "500000"


# --------------------------------------------------------------- rebates -----
def test_rebate_tier_is_detected() -> None:
    f = FeeSchedule.from_fee_tiers(TIERS, D(50_000_000))
    assert f.maker_is_rebate
    assert f.maker_bps == D("-0.5")


def test_round_trip_cost_goes_negative_at_a_rebate_tier() -> None:
    """Resting both legs EARNS money — the cost must not be clamped to zero."""
    f = FeeSchedule.from_fee_tiers(TIERS, D(50_000_000))
    assert f.round_trip_bps(maker_legs=2) == D(-1)


def test_maker_fee_is_credited_as_a_negative_fee() -> None:
    f = FeeSchedule.from_fee_tiers(TIERS, D(50_000_000))
    assert f.fee_for(D(10_000), "MAKER") == D("-0.5")
    assert f.fee_for(D(10_000), "TAKER") == D("2.5")


# ------------------------------------------------- the floor that matters ----
def worker(fees: FeeSchedule, **cfg_kw) -> MarketWorker:
    base = dict(spread_bps=D(0), fee_buffer_bps=D(0), min_edge_bps=D(1))
    base.update(cfg_kw)
    state = MarketState(market="X", market_id=1, book=BookState(market="X"),
                        meta={"tickSize": "0.1", "stepSize": "0.001",
                              "minOrderNotional": "5"})
    return MarketWorker(cfg=Config(**base), market="X", state=state,
                        pnl=PnLTracker(fees=fees))


def test_required_edge_falls_as_tiers_improve() -> None:
    base = worker(FeeSchedule.from_fee_tiers(TIERS, D(0))).required_edge_bps(2)
    vip1 = worker(FeeSchedule.from_fee_tiers(TIERS, D(2_000_000))).required_edge_bps(2)
    assert base == D(3)      # 1.5 x 2
    assert vip1 == D(1)      # 0.5 x 2, at the floor
    assert vip1 < base


def test_rebate_tier_never_drops_the_edge_to_zero() -> None:
    """Quoting both sides at mid would maximise adverse selection."""
    w = worker(FeeSchedule.from_fee_tiers(TIERS, D(50_000_000)))
    assert w.pnl.edge_required_bps(D(0), maker_legs=2) == D(-1)   # raw fee term
    assert w.required_edge_bps(2) == D(1)                          # floor holds
    assert w.required_edge_bps(2) >= w.cfg.min_edge_bps


def test_min_edge_floor_is_configurable() -> None:
    w = worker(FeeSchedule.from_fee_tiers(TIERS, D(50_000_000)), min_edge_bps=D(4))
    assert w.required_edge_bps(2) == D(4)


def test_operator_spread_still_wins_when_wider() -> None:
    w = worker(FeeSchedule.from_fee_tiers(TIERS, D(50_000_000)),
               spread_bps=D(12), min_edge_bps=D(1))
    assert w.edge_bps() == D(12)


# ---------------------------------------------- the REAL published schedule --
# The tests above use a synthetic fixture to exercise tier LOGIC. These pin the
# actual Arcus perpetuals numbers the simulator ships with, so a careless edit
# (or a stale guess) cannot silently change the economics every sweep depends on.


def test_sim_schedule_matches_the_published_arcus_tiers() -> None:
    from arcusbot.sim import SIM_FEE_TIERS

    expected = [
        (0, 0, 150, 450),
        (1, 5_000_000, 120, 380),
        (2, 20_000_000, 80, 320),
        (3, 100_000_000, 40, 270),
        (4, 400_000_000, 0, 230),
        (5, 1_000_000_000, -20, 200),
        (6, 3_000_000_000, -30, 190),
    ]
    actual = [(t["level"], t["volumeThreshold"], t["makerFeePpm"], t["takerFeePpm"])
              for t in SIM_FEE_TIERS["tiers"]]
    assert actual == expected


def test_base_tier_round_trip_is_six_bps() -> None:
    """maker+taker at tier 0 = 1.5 + 4.5 bps. Any strategy must clear this."""
    from arcusbot.sim import SIM_FEE_TIERS

    sched = FeeSchedule.from_fee_tiers(SIM_FEE_TIERS, volume_30d_usd=Decimal("0"))
    assert sched.maker_bps == Decimal("1.5")
    assert sched.taker_bps == Decimal("4.5")
    assert sched.round_trip_bps(maker_legs=2) == Decimal("3.0")
    assert sched.round_trip_bps(maker_legs=1) == Decimal("6.0")


def test_maker_rebates_require_a_billion_in_volume() -> None:
    """Guards against a strategy that assumes rebates are reachable."""
    from arcusbot.sim import SIM_FEE_TIERS

    below = FeeSchedule.from_fee_tiers(SIM_FEE_TIERS,
                                       volume_30d_usd=Decimal("999_000_000"))
    assert not below.maker_is_rebate
    assert below.maker_bps == Decimal("0")  # tier 4: free, but not paid

    at = FeeSchedule.from_fee_tiers(SIM_FEE_TIERS,
                                    volume_30d_usd=Decimal("1_000_000_000"))
    assert at.maker_is_rebate
    assert at.maker_bps == Decimal("-0.2")
