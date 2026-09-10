"""Durable state across restarts.

The point of these tests: a bot that forgets its losses on restart has no risk
limits at all. A crash loop with a $25 drawdown cap must not be able to lose
$25 per restart forever.
"""

from __future__ import annotations

import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.config import Config  # noqa: E402
from arcusbot.pnl import PnLTracker  # noqa: E402
from arcusbot.risk import HALT, RiskManager  # noqa: E402
from arcusbot.session import STATE_VERSION, SessionStore, _utc_day  # noqa: E402

D = Decimal


def store(tmp_path, enabled: bool = True) -> SessionStore:
    return SessionStore(tmp_path / "session.json", enabled=enabled).load()


def snap(volume="1000", net="-5", realized="-4", fees="1", rebates="0", fills=10) -> dict:
    return {"volumeUsd": volume, "makerVolumeUsd": volume, "netPnl": net,
            "realizedPnl": realized, "feesPaid": fees, "rebatesEarned": rebates,
            "fillCount": fills, "netBpsOfVolume": "-50"}


# ------------------------------------------------------------- round trip ----
def test_nothing_to_load_on_first_run(tmp_path) -> None:
    s = store(tmp_path)
    assert not s.loaded_from_disk
    assert s.lifetime.volume_usd == 0


def test_totals_survive_a_restart(tmp_path) -> None:
    s = store(tmp_path)
    s.start_session("a", {})
    s.finish_session("a", snap(volume="1000", realized="-4", fees="1"), "max runtime")

    reloaded = store(tmp_path)
    assert reloaded.loaded_from_disk
    assert reloaded.lifetime.volume_usd == D(1000)
    assert reloaded.lifetime.fills == 10
    assert reloaded.lifetime.realized_pnl_usd == D(-4)
    assert reloaded.lifetime.fees_paid_usd == D(1)


def test_totals_accumulate_over_many_restarts(tmp_path) -> None:
    for i in range(3):
        s = store(tmp_path)
        s.start_session(f"s{i}", {})
        s.finish_session(f"s{i}", snap(volume="500"), "max runtime")
    assert store(tmp_path).lifetime.volume_usd == D(1500)
    assert store(tmp_path).lifetime.sessions == 3


def test_disabled_store_writes_nothing(tmp_path) -> None:
    s = store(tmp_path, enabled=False)
    s.start_session("a", {})
    s.finish_session("a", snap(), "max runtime")
    assert not (tmp_path / "session.json").exists()


# -------------------------------------------------------------- daily loss ---
def test_daily_loss_carries_into_the_next_run(tmp_path) -> None:
    s = store(tmp_path)
    s.start_session("a", {})
    s.finish_session("a", snap(realized="-30", fees="0"), "max runtime")
    assert store(tmp_path).carried_daily_loss() == D(30)


def test_profit_does_not_produce_a_carried_loss(tmp_path) -> None:
    s = store(tmp_path)
    s.start_session("a", {})
    s.finish_session("a", snap(realized="12", fees="1"), "max runtime")
    assert store(tmp_path).carried_daily_loss() == D(0)


def test_day_rolls_over_at_utc_midnight(tmp_path) -> None:
    s = store(tmp_path)
    s.start_session("a", {})
    s.finish_session("a", snap(realized="-30", fees="0"), "max runtime")
    # Backdate the stored day; the next load must reset the counter.
    raw = json.loads((tmp_path / "session.json").read_text())
    raw["day"]["day"] = "2000-01-01"
    (tmp_path / "session.json").write_text(json.dumps(raw))

    reloaded = store(tmp_path)
    assert reloaded.day.day == _utc_day()
    assert reloaded.carried_daily_loss() == D(0)
    # Lifetime totals must NOT be reset by a day rollover.
    assert reloaded.lifetime.volume_usd == D(1000)


# ------------------------------------------------------------ crash counter --
@pytest.mark.parametrize("reason", ["max runtime 900s reached", "volume target reached",
                                    "signal SIGINT", "cancelled"])
def test_clean_exits_clear_the_crash_counter(tmp_path, reason) -> None:
    s = store(tmp_path)
    s.risk.consecutive_crashes = 4
    s.start_session("a", {})
    s.finish_session("a", snap(), reason)
    assert store(tmp_path).risk.consecutive_crashes == 0


@pytest.mark.parametrize("reason", ["12 consecutive errors", "drawdown $25 >= $25", ""])
def test_dirty_exits_increment_the_crash_counter(tmp_path, reason) -> None:
    s = store(tmp_path)
    s.start_session("a", {})
    s.finish_session("a", snap(), reason)
    assert store(tmp_path).risk.consecutive_crashes == 1


# ------------------------------------------------------------- durability ----
def test_corrupt_state_is_quarantined_not_fatal(tmp_path) -> None:
    (tmp_path / "session.json").write_text("{not json at all")
    s = store(tmp_path)
    assert not s.loaded_from_disk
    assert s.lifetime.volume_usd == 0
    assert (tmp_path / "session.corrupt").exists()


def test_unknown_version_starts_fresh(tmp_path) -> None:
    (tmp_path / "session.json").write_text(json.dumps(
        {"version": STATE_VERSION + 99, "lifetime": {"volume_usd": "999"}}))
    assert store(tmp_path).lifetime.volume_usd == 0


def test_write_is_atomic_leaving_no_temp_file(tmp_path) -> None:
    s = store(tmp_path)
    s.start_session("a", {})
    s.finish_session("a", snap(), "max runtime")
    assert (tmp_path / "session.json").is_file()
    assert not (tmp_path / "session.tmp").exists()


def test_history_is_bounded(tmp_path) -> None:
    s = store(tmp_path)
    for i in range(80):
        s.start_session(f"s{i}", {})
    assert len(s.history) <= 50


def test_equity_high_water_mark_only_rises(tmp_path) -> None:
    s = store(tmp_path)
    s.note_equity(D(1000))
    s.note_equity(D(800))
    assert s.risk.peak_equity_usd == D(1000)
    s.note_equity(D(1200))
    assert s.risk.peak_equity_usd == D(1200)


# ------------------------------------------------- integration with risk -----
def cfg(**kw) -> Config:
    base = dict(max_daily_loss_usd=D(40), max_drawdown_usd=D(25),
                carry_drawdown=True, max_restart_crashes=0)
    base.update(kw)
    return Config(**base)


def test_risk_manager_starts_halted_past_the_daily_limit(tmp_path) -> None:
    """The headline guarantee: yesterday's blown limit still blocks today."""
    s = store(tmp_path)
    s.start_session("a", {})
    s.finish_session("a", snap(realized="-41", fees="0"), "max runtime")

    risk = RiskManager(cfg(), PnLTracker(), store(tmp_path))
    assert risk.effective_daily_loss() >= D(41)
    assert risk.evaluate().state == HALT


def test_remaining_budget_is_what_is_left_not_the_full_limit(tmp_path) -> None:
    s = store(tmp_path)
    s.start_session("a", {})
    s.finish_session("a", snap(realized="-38", fees="0"), "max runtime")

    risk = RiskManager(cfg(), PnLTracker(), store(tmp_path))
    assert risk.evaluate().state != HALT          # $2 of headroom left
    assert risk.effective_daily_loss() == D(38)


def test_carried_drawdown_counts_toward_the_limit(tmp_path) -> None:
    s = store(tmp_path)
    s.risk.peak_net_pnl_usd = D(30)               # was up $30 previously
    s.save()

    pnl = PnLTracker()
    risk = RiskManager(cfg(), pnl, store(tmp_path))
    # Flat this session, but $30 below the all-time peak.
    assert risk.effective_drawdown() == D(30)
    assert risk.evaluate().state == HALT


def test_carry_drawdown_can_be_disabled(tmp_path) -> None:
    s = store(tmp_path)
    s.risk.peak_net_pnl_usd = D(30)
    s.save()
    risk = RiskManager(cfg(carry_drawdown=False), PnLTracker(), store(tmp_path))
    assert risk.effective_drawdown() == D(0)


def test_crash_loop_breaker_halts_before_trading(tmp_path) -> None:
    s = store(tmp_path)
    s.risk.consecutive_crashes = 3
    s.risk.last_exit_reason = "12 consecutive errors"
    s.save()
    risk = RiskManager(cfg(max_restart_crashes=3), PnLTracker(), store(tmp_path))
    assert risk.halt_reason is not None
    assert "consecutive unclean exits" in risk.halt_reason
    assert "session.json" in risk.halt_reason      # tells the operator the fix


def test_crash_breaker_off_by_default(tmp_path) -> None:
    s = store(tmp_path)
    s.risk.consecutive_crashes = 99
    s.save()
    assert RiskManager(cfg(), PnLTracker(), store(tmp_path)).halt_reason is None


def test_risk_manager_works_without_a_session(tmp_path) -> None:
    """Session state is optional; the manager must not require it."""
    risk = RiskManager(cfg(), PnLTracker(), None)
    assert risk.carried_daily_loss == D(0)
    assert risk.evaluate().state != HALT
