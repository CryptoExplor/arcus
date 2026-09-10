"""The mainnet gate — the code that stands between a typo and real money.

Every one of these tests exists because the failure it describes would spend
funds that are not meant to be spent. The gate is conjunctive and fail-closed:
anything unrecognised means "no".
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.config import Config  # noqa: E402
from arcusbot.mainnet import (  # noqa: E402
    ACK_PHRASE,
    DEFAULT_MAX_MAINNET_CAPITAL_USD,
    apply_mainnet_limits,
    assert_capital_within_cap,
    evaluate_gate,
    mainnet_risk_floor,
    strict_bool,
    strict_decimal,
)

FULL = {
    "BOT_MAINNET_ENABLED": "true",
    "BOT_MAINNET_ACK": ACK_PHRASE,
    "BOT_MAINNET_CAPITAL_USD": "20",
}


def live_cfg(**kw) -> Config:
    return Config(network="mainnet", mode="live", **kw)


# ----------------------------------------------------------- strict parsing --


def test_strict_bool_accepts_only_known_tokens() -> None:
    for token in ("1", "true", "TRUE", "yes", "on"):
        value, err = strict_bool(token, "K")
        assert value is True and not err
    for token in ("0", "false", "no", "off", ""):
        value, err = strict_bool(token, "K")
        assert value is False and not err
    for junk in ("ture", "y", "enabled", "2", "maybe"):
        value, err = strict_bool(junk, "K")
        assert value is False and err, f"{junk!r} must be rejected, not coerced"


def test_strict_decimal_rejects_junk() -> None:
    value, err = strict_decimal("20", "K")
    assert value == Decimal("20") and not err
    for junk in ("twenty", "20usd", "1e", "--3"):
        value, err = strict_decimal(junk, "K")
        assert err and value is None, f"{junk!r} must be rejected"
    # Empty is "unset", not "invalid" — the gate reports it as missing.
    assert strict_decimal("", "K") == (None, None)


# --------------------------------------------------------------- the gate ----


def test_all_opt_ins_present_opens_the_gate() -> None:
    result = evaluate_gate(live_cfg(), FULL)
    assert result.allowed
    assert result.capital_cap == Decimal("20")
    assert result.is_mainnet_live_attempt


def test_every_single_missing_opt_in_closes_the_gate() -> None:
    """The conditions are AND-ed: dropping any one must deny."""
    for key in FULL:
        env = {k: v for k, v in FULL.items() if k != key}
        result = evaluate_gate(live_cfg(), env)
        assert not result.allowed, f"missing {key} must deny"
        assert any(key in r for r in result.reasons)


def test_empty_environment_denies() -> None:
    result = evaluate_gate(live_cfg(), {})
    assert not result.allowed
    assert len(result.reasons) >= 3


def test_typo_in_the_enable_flag_denies() -> None:
    """A malformed value must never be read as 'true'."""
    for junk in ("ture", "True!", "y", "1.0", "enabled"):
        result = evaluate_gate(live_cfg(), {**FULL, "BOT_MAINNET_ENABLED": junk})
        assert not result.allowed, f"{junk!r} must not enable mainnet"


def test_wrong_ack_phrase_denies() -> None:
    for junk in ("", "yes", "i understand the risk", "i-understand"):
        result = evaluate_gate(live_cfg(), {**FULL, "BOT_MAINNET_ACK": junk})
        assert not result.allowed, f"{junk!r} must not pass as the ack"


def test_capital_must_be_positive_and_numeric() -> None:
    for junk in ("0", "-5", "twenty", ""):
        result = evaluate_gate(live_cfg(), {**FULL, "BOT_MAINNET_CAPITAL_USD": junk})
        assert not result.allowed, f"{junk!r} must not be accepted as capital"


def test_fat_finger_capital_is_capped_by_the_ceiling() -> None:
    """20 -> 2000 is one keystroke. The ceiling catches it."""
    result = evaluate_gate(live_cfg(), {**FULL, "BOT_MAINNET_CAPITAL_USD": "2000"})
    assert not result.allowed
    assert any("ceiling" in r for r in result.reasons)


def test_ceiling_is_itself_overridable_but_explicit() -> None:
    env = {**FULL, "BOT_MAINNET_CAPITAL_USD": "250",
           "BOT_MAINNET_MAX_CAPITAL_USD": "500"}
    assert evaluate_gate(live_cfg(), env).allowed
    assert DEFAULT_MAX_MAINNET_CAPITAL_USD == Decimal("100")


# ------------------------------------------------------- scope of the gate --


def test_dry_run_on_mainnet_is_never_gated() -> None:
    result = evaluate_gate(Config(network="mainnet", mode="dry-run"), {})
    assert result.allowed and not result.is_mainnet_live_attempt


def test_testnet_live_is_never_gated() -> None:
    result = evaluate_gate(Config(network="testnet", mode="live"), {})
    assert result.allowed and not result.is_mainnet_live_attempt


def test_sim_venue_is_never_gated() -> None:
    result = evaluate_gate(Config(network="mainnet", mode="live", venue="sim"), {})
    assert result.allowed and not result.is_mainnet_live_attempt


def test_config_validate_surfaces_the_gate_reasons() -> None:
    problems = Config(network="mainnet", mode="live").validate()
    assert any("mainnet" in p.lower() for p in problems)


# ------------------------------------------------------------ risk limits ---


def test_risk_floor_scales_with_the_cap_not_the_default_config() -> None:
    """First-experiment limits: bound the LOSS, not just the deposit."""
    floor = mainnet_risk_floor(Decimal("20"))
    assert floor["max_drawdown_usd"] == Decimal("1.00")
    assert floor["max_daily_loss_usd"] == Decimal("2.00")
    assert floor["max_position_notional_usd"] == Decimal("15.00")
    assert floor["max_inventory_notional_usd"] == Decimal("15.00")


def test_position_notional_never_exceeds_the_capital_cap() -> None:
    """Guards the "$20 account with a $30 position" ambiguity.

    Notional above equity is only safe if you reason about leverage and
    liquidation. For a first experiment we simply refuse to go there.
    """
    for cap in ("20", "50", "100"):
        floor = mainnet_risk_floor(Decimal(cap))
        assert floor["max_position_notional_usd"] <= Decimal(cap)
        assert floor["max_inventory_notional_usd"] <= Decimal(cap)


def test_loss_limits_are_a_small_fraction_of_capital() -> None:
    floor = mainnet_risk_floor(Decimal("20"))
    assert floor["max_drawdown_usd"] <= Decimal("20") * Decimal("0.05")
    assert floor["max_daily_loss_usd"] <= Decimal("20") * Decimal("0.10")


def test_apply_mainnet_limits_only_tightens() -> None:
    """Defaults are sized for testnet; on a $20 account they are enormous."""
    cfg = live_cfg(max_drawdown_usd=Decimal("25"), max_daily_loss_usd=Decimal("40"),
                   max_position_notional_usd=Decimal("150"),
                   max_inventory_notional_usd=Decimal("75"))
    changes = apply_mainnet_limits(cfg, Decimal("20"))
    assert cfg.max_drawdown_usd == Decimal("1.00")
    assert cfg.max_daily_loss_usd == Decimal("2.00")
    assert changes, "tightening must be reported to the operator"


def test_apply_mainnet_limits_never_loosens_a_stricter_setting() -> None:
    cfg = live_cfg(max_drawdown_usd=Decimal("1"), max_daily_loss_usd=Decimal("1"),
                   max_position_notional_usd=Decimal("5"),
                   max_inventory_notional_usd=Decimal("5"))
    apply_mainnet_limits(cfg, Decimal("20"))
    assert cfg.max_drawdown_usd == Decimal("1"), "operator's stricter limit must survive"
    assert cfg.max_position_notional_usd == Decimal("5")


def test_capital_over_the_cap_is_rejected_at_runtime() -> None:
    """Defence in depth: re-assert the cap even after the gate opened."""
    assert assert_capital_within_cap(Decimal("20"), Decimal("20")) is None
    assert assert_capital_within_cap(Decimal("19.99"), Decimal("20")) is None
    breach = assert_capital_within_cap(Decimal("20.01"), Decimal("20"))
    assert breach and "20.01" in breach
