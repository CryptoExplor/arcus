"""Named wallet profiles, and the guard rails around wallet private keys.

Two wallets on testnet plus one on mainnet in a single `.env` is convenient and
also the exact setup where "I thought I was on testnet" happens. These tests pin
the switching behaviour and, more importantly, the handling of withdrawal-capable
keys.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.config import Config  # noqa: E402
from arcusbot.wallets import (  # noqa: E402
    ALLOW_MAINNET_PK,
    REDACTED,
    apply_to_config,
    discover_profiles,
    format_table,
    load_all,
    load_profile,
)

T1_ADDR = "0x1111111111111111111111111111111111111111"
T2_ADDR = "0x2222222222222222222222222222222222222222"
M1_ADDR = "0x3333333333333333333333333333333333333333"
T1_SEC = "aa" * 32
T2_SEC = "bb" * 32
M1_SEC = "cc" * 32
M1_PK = "dd" * 32

ENV = {
    "ARCUS_ADDRESS_T1": T1_ADDR, "ARCUS_API_SECRET_T1": T1_SEC,
    "ARCUS_LABEL_T1": "testnet wallet 1",
    "ARCUS_ADDRESS_T2": T2_ADDR, "ARCUS_API_SECRET_T2": T2_SEC,
    "ARCUS_ADDRESS_M1": M1_ADDR, "ARCUS_API_SECRET_M1": M1_SEC,
    "ARCUS_NETWORK_M1": "mainnet", "ARCUS_PRIVATE_KEY_M1": M1_PK,
}


# ------------------------------------------------------------- discovery ----


def test_all_three_profiles_are_discovered() -> None:
    assert set(discover_profiles(ENV)) == {"t1", "t2", "m1"}


def test_default_profile_appears_only_when_defined() -> None:
    assert "default" not in discover_profiles(ENV)
    assert "default" in discover_profiles({**ENV, "ARCUS_ADDRESS": T1_ADDR})


def test_each_profile_loads_its_own_credentials() -> None:
    t1, t2 = load_profile("t1", ENV), load_profile("t2", ENV)
    assert (t1.address, t1.api_secret) == (T1_ADDR, T1_SEC)
    assert (t2.address, t2.api_secret) == (T2_ADDR, T2_SEC)
    assert t1.address != t2.address, "profiles must not bleed into each other"


def test_profile_names_are_case_insensitive() -> None:
    assert load_profile("T1", ENV).address == load_profile("t1", ENV).address


# --------------------------------------------------------------- network ----


def test_network_is_inferred_from_the_name() -> None:
    """t*/test* -> testnet, m*/main* -> mainnet, without an explicit setting."""
    env = {"ARCUS_ADDRESS_T9": T1_ADDR, "ARCUS_API_SECRET_T9": T1_SEC,
           "ARCUS_ADDRESS_M9": M1_ADDR, "ARCUS_API_SECRET_M9": M1_SEC}
    assert load_profile("t9", env).network == "testnet"
    assert load_profile("m9", env).network == "mainnet"


def test_unrecognised_name_defaults_to_testnet_not_mainnet() -> None:
    """Guessing wrong must fail safe."""
    env = {"ARCUS_ADDRESS_ZZ": T1_ADDR, "ARCUS_API_SECRET_ZZ": T1_SEC}
    assert load_profile("zz", env).network == "testnet"


def test_explicit_network_beats_the_inferred_one() -> None:
    env = {"ARCUS_ADDRESS_T5": T1_ADDR, "ARCUS_API_SECRET_T5": T1_SEC,
           "ARCUS_NETWORK_T5": "mainnet"}
    assert load_profile("t5", env).network == "mainnet"


def test_applying_a_mainnet_profile_switches_the_urls() -> None:
    """Otherwise a mainnet profile would keep talking to the testnet host."""
    cfg = Config(network="testnet")
    apply_to_config(cfg, load_profile("m1", ENV))
    assert cfg.network == "mainnet"
    assert "testnet" not in cfg.rest_url
    assert "testnet" not in cfg.ws_url


def test_applying_a_testnet_profile_keeps_testnet_urls() -> None:
    cfg = Config(network="mainnet")
    apply_to_config(cfg, load_profile("t1", ENV))
    assert cfg.network == "testnet"
    assert "testnet" in cfg.rest_url


# ------------------------------------------------------------ validation ----


@pytest.mark.parametrize("key,bad", [
    ("ARCUS_ADDRESS_T1", "not-an-address"),
    ("ARCUS_ADDRESS_T1", "0x123"),
    ("ARCUS_API_SECRET_T1", "tooshort"),
    ("ARCUS_API_SECRET_T1", "zz" * 32),
])
def test_malformed_credentials_are_rejected(key: str, bad: str) -> None:
    profile = load_profile("t1", {**ENV, key: bad})
    assert not profile.usable
    assert profile.problems


def test_missing_credentials_are_reported_not_silently_empty() -> None:
    profile = load_profile("t3", ENV)
    assert not profile.usable
    assert any("not set" in p for p in profile.problems)


def test_unsafe_profile_names_are_refused() -> None:
    assert not load_profile("../../etc", ENV).usable


# ------------------------------------------------ wallet private key rules --


def test_private_key_is_not_loaded_during_ordinary_use() -> None:
    """Trading never needs it, so it must not sit in memory by default."""
    profile = load_profile("m1", ENV)
    assert profile.private_key == ""
    assert profile.has_private_key, "but its presence should still be known"


def test_mainnet_private_key_needs_a_second_explicit_opt_in() -> None:
    profile = load_profile("m1", ENV, require_private_key=True)
    assert profile.private_key == ""
    assert any(ALLOW_MAINNET_PK in p for p in profile.problems)


def test_mainnet_private_key_loads_once_allowed() -> None:
    profile = load_profile("m1", {**ENV, ALLOW_MAINNET_PK: "true"},
                           require_private_key=True)
    assert profile.private_key == M1_PK
    assert not profile.problems


def test_testnet_private_key_needs_only_the_request() -> None:
    """Testnet funds are worthless, so no second gate — but still opt-in."""
    env = {**ENV, "ARCUS_PRIVATE_KEY_T1": M1_PK}
    assert load_profile("t1", env).private_key == ""
    assert load_profile("t1", env, require_private_key=True).private_key == M1_PK


def test_malformed_private_key_is_rejected() -> None:
    env = {**ENV, "ARCUS_PRIVATE_KEY_T1": "nonsense"}
    profile = load_profile("t1", env, require_private_key=True)
    assert profile.private_key == ""
    assert any("64 hex" in p for p in profile.problems)


def test_private_key_accepts_both_0x_and_bare_hex() -> None:
    for value in (M1_PK, "0x" + M1_PK):
        env = {**ENV, "ARCUS_PRIVATE_KEY_T1": value}
        assert load_profile("t1", env, require_private_key=True).private_key == value


# ------------------------------------------------------------- redaction ----


def test_secrets_never_appear_in_serialised_output() -> None:
    payload = str(load_profile("m1", {**ENV, ALLOW_MAINNET_PK: "true"},
                               require_private_key=True).as_dict())
    assert M1_SEC not in payload
    assert M1_PK not in payload
    assert REDACTED in payload


def test_secrets_never_appear_in_describe_or_table() -> None:
    profiles = load_all(ENV)
    text = format_table(profiles) + " ".join(p.describe() for p in profiles)
    for secret in (T1_SEC, T2_SEC, M1_SEC, M1_PK):
        assert secret not in text


def test_table_warns_about_a_stored_wallet_key() -> None:
    text = format_table(load_all(ENV))
    assert "WARNING" in text and "withdraw" in text
    assert "holds" in text, "single profile should read naturally"


def test_address_is_shortened_for_display_but_kept_in_full_in_data() -> None:
    profile = load_profile("t1", ENV)
    assert profile.short_address() == "0x1111…1111"
    assert profile.as_dict()["address"] == T1_ADDR
