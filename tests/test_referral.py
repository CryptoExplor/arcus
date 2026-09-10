"""Referral links and VIP milestone arithmetic."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.config import Config  # noqa: E402
from arcusbot.referral import (  # noqa: E402
    VIP_VOLUME_TARGET_USD,
    all_referral_links,
    banner,
    referral_code,
    referral_link,
    vip_progress,
)

D = Decimal


# ------------------------------------------------------------------- links ---
def test_default_links_match_the_published_ones() -> None:
    testnet = Config(network="testnet")
    mainnet = Config(network="mainnet")
    assert referral_link(testnet) == "https://testnet.arcus.xyz/ref/ARCUS"
    assert referral_link(mainnet) == "https://app.arcus.xyz/ref/AIAGENT"


def test_link_follows_the_configured_network() -> None:
    assert "testnet.arcus.xyz" in referral_link(Config(network="testnet"))
    assert "app.arcus.xyz" in referral_link(Config(network="mainnet"))


def test_codes_are_overridable_for_forks() -> None:
    c = Config(network="testnet", referral_testnet="MYCODE")
    assert referral_code(c) == "MYCODE"
    assert referral_link(c).endswith("/ref/MYCODE")


def test_empty_code_yields_no_link_rather_than_a_broken_one() -> None:
    c = Config(network="testnet", referral_testnet="")
    assert referral_link(c) == ""
    assert "testnet" not in all_referral_links(c)


def test_all_links_lists_both_networks() -> None:
    links = all_referral_links(Config())
    assert set(links) == {"testnet", "mainnet"}


# ------------------------------------------------------------------ banner ---
def test_banner_mentions_both_networks_and_the_opt_out() -> None:
    text = banner(Config())
    assert "testnet.arcus.xyz/ref/ARCUS" in text
    assert "app.arcus.xyz/ref/AIAGENT" in text
    assert "BOT_SHOW_REFERRAL=false" in text


def test_banner_can_be_switched_off() -> None:
    assert banner(Config(show_referral=False)) == ""


def test_banner_empty_when_no_codes_configured() -> None:
    assert banner(Config(referral_testnet="", referral_mainnet="")) == ""


# --------------------------------------------------------------------- VIP ---
def snap(volume: str, per_hour: str = "0", net_bps: str = "0") -> dict:
    return {"volumeUsd": volume, "volumePerHourUsd": per_hour, "netBpsOfVolume": net_bps}


def test_target_is_one_billion() -> None:
    assert VIP_VOLUME_TARGET_USD == D("1000000000")


def test_progress_percentage_and_remainder() -> None:
    p = vip_progress(snap("250000000"))
    assert p.pct == D(25)
    assert p.remaining_usd == D("750000000")


def test_eta_uses_the_observed_rate() -> None:
    # 1M/hour against a 1B target -> 1000 hours remaining (from zero).
    p = vip_progress(snap("0", per_hour="1000000"))
    assert p.hours_remaining == D(1000)


def test_eta_is_unknown_when_not_trading() -> None:
    assert vip_progress(snap("100", per_hour="0")).hours_remaining is None
    assert vip_progress(snap("100")).as_dict()["hoursRemaining"] is None


def test_negative_edge_makes_the_milestone_cost_money() -> None:
    """The whole point of this number: $1B at -2 bps is a $200k bill."""
    p = vip_progress(snap("0", per_hour="1000", net_bps="-2"))
    assert p.projected_pnl_usd == D(-200_000)
    assert "costing" in p.describe()


def test_positive_edge_projects_a_profit() -> None:
    p = vip_progress(snap("0", per_hour="1000", net_bps="1"))
    assert p.projected_pnl_usd == D(100_000)
    assert "earning" in p.describe()


def test_progress_never_goes_negative_past_the_target() -> None:
    p = vip_progress(snap("2000000000"))
    assert p.remaining_usd == D(0)
    assert p.projected_pnl_usd == D(0)


def test_dict_is_json_safe_strings() -> None:
    d = vip_progress(snap("1234.5", per_hour="1000", net_bps="-1")).as_dict()
    assert all(v is None or isinstance(v, str) for v in d.values())
    assert set(d) >= {"targetUsd", "volumeUsd", "pctComplete", "remainingUsd",
                      "hoursRemaining", "daysRemaining", "projectedPnlAtTargetUsd"}


def test_missing_fields_are_treated_as_zero_not_crash() -> None:
    p = vip_progress({})
    assert p.volume_usd == D(0)
    assert p.hours_remaining is None
