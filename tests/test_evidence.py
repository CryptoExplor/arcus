"""Evidence recording and the rules that stop us fooling ourselves.

The reviewer's central point: a strategy is not validated by a short lucky run,
and unrealized PnL is not profit. These tests encode both.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.evidence import (  # noqa: E402
    MIN_FILLS_FOR_EDGE_CLAIM,
    MIN_SESSIONS_FOR_EDGE_CLAIM,
    SessionEvidence,
    aggregate,
    append_session,
    load_sessions,
)


def session(**kw) -> SessionEvidence:
    base = dict(
        session_id="s1", markets=["BTC-USD"], runtime_s=900,
        volume_usd=Decimal("10000"), fills=100, maker_fills=90, taker_fills=10,
        orders_sent=400, gross_pnl=Decimal("5"), fees_paid=Decimal("3"),
        realized_pnl=Decimal("5"), unrealized_pnl=Decimal("0"),
    )
    base.update(kw)
    return SessionEvidence(**base)


# --------------------------------------------------- the core PnL identity --


def test_net_pnl_is_recomputed_from_components() -> None:
    ev = session(realized_pnl=Decimal("10"), unrealized_pnl=Decimal("2"),
                 funding_pnl=Decimal("1"), rebates=Decimal("0.5"),
                 fees_paid=Decimal("4"))
    assert ev.net_pnl == Decimal("9.5")


def test_net_bps_of_volume_is_the_headline_metric() -> None:
    ev = session(volume_usd=Decimal("10000"), realized_pnl=Decimal("5"),
                 fees_paid=Decimal("3"))
    assert ev.net_bps_of_volume == Decimal("2")  # $2 / $10k = 2 bps


def test_zero_volume_does_not_divide_by_zero() -> None:
    assert session(volume_usd=Decimal("0")).net_bps_of_volume == Decimal("0")
    assert session(fees_paid=Decimal("0")).fee_coverage_ratio == Decimal("0")


# ------------------------------------------- the unrealized-PnL trap ------


def test_profit_that_depends_on_unrealized_marks_is_flagged() -> None:
    """The exact failure the reviewer warned about."""
    ev = session(realized_pnl=Decimal("-1"), unrealized_pnl=Decimal("5"),
                 fees_paid=Decimal("2"))
    assert ev.net_pnl > 0, "headline looks positive"
    assert ev.realized_only_net < 0, "but nothing has actually been banked"
    assert ev.unrealized_dependent
    assert any("UNREALIZED" in w for w in ev.warnings())


def test_genuinely_realized_profit_is_not_flagged() -> None:
    ev = session(realized_pnl=Decimal("10"), unrealized_pnl=Decimal("1"),
                 fees_paid=Decimal("2"))
    assert not ev.unrealized_dependent
    assert not any("UNREALIZED" in w for w in ev.warnings())


def test_aggregate_requires_realized_profit_not_just_marks() -> None:
    sessions = [session(session_id=f"s{i}", realized_pnl=Decimal("-1"),
                        unrealized_pnl=Decimal("5"), fees_paid=Decimal("2"),
                        fills=200)
                for i in range(6)]
    agg = aggregate(sessions)
    assert agg.total_net > 0
    assert agg.total_realized_only < 0
    assert not agg.ready_for_mainnet()
    assert any("realized-only" in b for b in agg.blockers())


# ------------------------------------------------- statistical discipline --


def test_a_single_lucky_session_is_never_enough() -> None:
    agg = aggregate([session(realized_pnl=Decimal("50"), fills=1000)])
    assert agg.verdict() == "INSUFFICIENT-DATA"
    assert not agg.ready_for_mainnet()


def test_too_few_fills_is_insufficient_regardless_of_profit() -> None:
    sessions = [session(session_id=f"s{i}", fills=10, realized_pnl=Decimal("5"))
                for i in range(MIN_SESSIONS_FOR_EDGE_CLAIM + 1)]
    agg = aggregate(sessions)
    assert agg.total_fills < MIN_FILLS_FOR_EDGE_CLAIM
    assert agg.verdict() == "INSUFFICIENT-DATA"


def test_noisy_positive_mean_is_not_a_credible_edge() -> None:
    """+edge on average but wildly variable => not something to bet on."""
    swings = ["40", "-38", "42", "-36", "39", "-35", "41", "-30"]
    sessions = [session(session_id=f"s{i}", fills=100,
                        realized_pnl=Decimal(v), fees_paid=Decimal("0"))
                for i, v in enumerate(swings)]
    agg = aggregate(sessions)
    assert agg.t_statistic() < 2.0
    assert not agg.credible_positive_edge()
    assert any("statistically" in b for b in agg.blockers())


def test_consistent_positive_edge_across_many_sessions_passes() -> None:
    sessions = [session(session_id=f"s{i}", fills=100,
                        volume_usd=Decimal("10000"),
                        gross_pnl=Decimal("6"),
                        realized_pnl=Decimal("6"), fees_paid=Decimal("3"))
                for i in range(8)]
    agg = aggregate(sessions)
    assert agg.total_fills >= MIN_FILLS_FOR_EDGE_CLAIM
    assert agg.fee_coverage_ratio > 1
    assert agg.verdict() == "POSITIVE-EDGE"
    assert agg.ready_for_mainnet()


def test_negative_aggregate_is_reported_as_negative() -> None:
    sessions = [session(session_id=f"s{i}", fills=100,
                        realized_pnl=Decimal("1"), fees_paid=Decimal("3"))
                for i in range(8)]
    agg = aggregate(sessions)
    assert agg.total_net < 0
    assert agg.verdict() == "NEGATIVE-EDGE"
    assert not agg.ready_for_mainnet()


def test_fee_coverage_below_one_blocks_even_when_net_is_positive() -> None:
    """Gross edge must pay for the fees, not be rescued by funding/rebates."""
    sessions = [session(session_id=f"s{i}", fills=100,
                        gross_pnl=Decimal("2"), fees_paid=Decimal("3"),
                        realized_pnl=Decimal("2"), funding_pnl=Decimal("5"))
                for i in range(8)]
    agg = aggregate(sessions)
    assert agg.total_net > 0 and agg.fee_coverage_ratio < 1
    assert not agg.ready_for_mainnet()
    assert any("fee coverage" in b for b in agg.blockers())


def test_no_data_is_its_own_verdict() -> None:
    assert aggregate([]).verdict() == "NO-DATA"
    assert not aggregate([]).ready_for_mainnet()


# ------------------------------------------------------ operational faults --


def test_open_positions_at_exit_block_readiness() -> None:
    good = [session(session_id=f"s{i}", fills=100, volume_usd=Decimal("10000"),
                    gross_pnl=Decimal("6"), realized_pnl=Decimal("6"),
                    fees_paid=Decimal("3")) for i in range(8)]
    good[3].residual_positions = {"BTC-USD": "0.01"}
    agg = aggregate(good)
    assert not agg.ready_for_mainnet()
    assert any("open positions" in b for b in agg.blockers())


def test_failed_flatten_blocks_readiness() -> None:
    good = [session(session_id=f"s{i}", fills=100, volume_usd=Decimal("10000"),
                    gross_pnl=Decimal("6"), realized_pnl=Decimal("6"),
                    fees_paid=Decimal("3")) for i in range(8)]
    good[2].flatten_attempted = True
    good[2].flatten_succeeded = False
    assert any("flatten" in b for b in aggregate(good).blockers())


def test_reconciliation_discrepancies_block_readiness() -> None:
    good = [session(session_id=f"s{i}", fills=100, volume_usd=Decimal("10000"),
                    gross_pnl=Decimal("6"), realized_pnl=Decimal("6"),
                    fees_paid=Decimal("3")) for i in range(8)]
    good[1].reconcile_discrepancies = 2
    assert any("reconciliation" in b for b in aggregate(good).blockers())


def test_high_rejection_rate_is_warned() -> None:
    ev = session(orders_sent=100, orders_rejected=25)
    assert any("rejection" in w for w in ev.warnings())


# -------------------------------------------------------------- round trip --


def test_sessions_survive_a_write_read_cycle(tmp_path: Path) -> None:
    path = tmp_path / "evidence.jsonl"
    original = session(regime="high-vol", volume_usd=Decimal("1234.56"),
                       realized_pnl=Decimal("7.89"), fills=42)
    append_session(path, original)
    append_session(path, session(session_id="s2", regime="thin"))

    loaded = load_sessions(path)
    assert len(loaded) == 2
    assert loaded[0].regime == "high-vol"
    assert loaded[0].volume_usd == Decimal("1234.56")
    assert loaded[0].fills == 42
    assert loaded[0].net_pnl == original.net_pnl


def test_a_corrupt_line_does_not_lose_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "evidence.jsonl"
    append_session(path, session())
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    append_session(path, session(session_id="s3"))
    assert len(load_sessions(path)) == 2


def test_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load_sessions(tmp_path / "nope.jsonl") == []


def test_report_states_the_verdict_and_blockers() -> None:
    text = aggregate([session(fills=5)]).report()
    assert "VERDICT" in text and "INSUFFICIENT-DATA" in text
    assert "ready for mainnet NO" in text


# ------------------------------------------------------ status ingestion ----


def test_maker_share_percentage_is_converted_correctly() -> None:
    """Regression: makerShare is 0-100, not a 0-1 fraction."""
    ev = SessionEvidence.from_status({
        "pnl": {"fillCount": 100, "makerShare": "85.7", "volumeUsd": "1000"},
    })
    assert ev.maker_fills == 86
    assert ev.taker_fills == 14
    assert Decimal("0.8") < ev.maker_ratio < Decimal("0.9")


def test_maker_fills_can_never_exceed_total_fills() -> None:
    ev = SessionEvidence.from_status({
        "pnl": {"fillCount": 10, "makerShare": "100", "volumeUsd": "100"},
    })
    assert ev.maker_fills == 10 and ev.taker_fills == 0


# ------------------------------- simulator output is not evidence ----------
#
# The failure this guards against: `.env.example` ships ARCUS_VENUE=sim, so
# `arcusbot run --network testnet --mode live` runs the OFFLINE SIMULATOR while
# logging "mode=live" and "network=testnet". The output is evidence-shaped and
# completely synthetic. Nothing that gates real money may count it.


def test_sim_venue_session_is_not_live_execution() -> None:
    assert session(venue="sim").is_live_execution is False
    assert session(venue="arcus", mode="dry-run").is_live_execution is False
    assert session(venue="arcus", mode="live").is_live_execution is True


def test_simulated_session_warns_loudly() -> None:
    warnings = " ".join(session(venue="sim").warnings()).lower()
    assert "simulated" in warnings
    assert "not evidence" in warnings


def test_perfect_simulator_results_never_reach_positive_edge() -> None:
    """Even flawless sim numbers must not unlock mainnet."""
    sessions = [
        session(session_id=f"sim{i}", venue="sim", regime=f"r{i}",
                fills=500, volume_usd=Decimal("100000"),
                gross_pnl=Decimal("100"), fees_paid=Decimal("10"),
                realized_pnl=Decimal("100"))
        for i in range(8)
    ]
    agg = aggregate(sessions)
    assert agg.total_net > 0                      # the numbers look great
    assert agg.credible_positive_edge() is False  # and are still refused
    assert agg.ready_for_mainnet() is False
    assert any("simulated" in b.lower() for b in agg.blockers())


def test_dry_run_against_real_venue_also_refused() -> None:
    sessions = [
        session(session_id=f"d{i}", venue="arcus", mode="dry-run",
                fills=500, volume_usd=Decimal("100000"),
                gross_pnl=Decimal("100"), fees_paid=Decimal("10"),
                realized_pnl=Decimal("100"))
        for i in range(8)
    ]
    assert aggregate(sessions).ready_for_mainnet() is False


def test_one_simulated_session_contaminates_an_otherwise_good_set() -> None:
    """A single sim session poisons the aggregate rather than being averaged in."""
    good = [
        session(session_id=f"live{i}", regime=f"r{i}", fills=500,
                volume_usd=Decimal("100000"), gross_pnl=Decimal("60"),
                fees_paid=Decimal("10"), realized_pnl=Decimal("60"))
        for i in range(6)
    ]
    assert aggregate(good).ready_for_mainnet() is True
    contaminated = good + [session(session_id="oops", venue="sim", fills=500,
                                   volume_usd=Decimal("100000"),
                                   gross_pnl=Decimal("60"),
                                   fees_paid=Decimal("10"),
                                   realized_pnl=Decimal("60"))]
    assert aggregate(contaminated).ready_for_mainnet() is False


def test_live_execution_flag_survives_a_jsonl_round_trip(tmp_path) -> None:
    path = tmp_path / "ev.jsonl"
    append_session(path, session(session_id="a", venue="sim"))
    append_session(path, session(session_id="b", venue="arcus", mode="live"))
    loaded = load_sessions(path)
    assert [s.is_live_execution for s in loaded] == [False, True]
