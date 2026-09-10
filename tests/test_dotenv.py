"""The .env parser.

This is small but load-bearing: a parsing slip silently feeds a wrong number
into the sizing or risk limits. It is exercised directly rather than through
Config so failures point at the parser.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.config import _strip_inline_comment, load_dotenv  # noqa: E402


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    def write(text: str) -> Path:
        p = tmp_path / ".env"
        p.write_text(text, encoding="utf-8")
        for key in list(os.environ):
            if key.startswith(("ARCUS_", "BOT_", "RISK_", "SIM_", "T_")):
                monkeypatch.delenv(key, raising=False)
        return p
    return write


def test_plain_assignment(env_file) -> None:
    load_dotenv(env_file("T_A=hello\n"))
    assert os.environ["T_A"] == "hello"


def test_inline_comment_is_stripped(env_file) -> None:
    """The bug this file exists for: `0   # subaccount` must parse as `0`."""
    load_dotenv(env_file("T_IDX=0          # subaccount; each has its own pools\n"))
    assert os.environ["T_IDX"] == "0"
    assert int(os.environ["T_IDX"]) == 0


def test_decimal_with_inline_comment(env_file) -> None:
    load_dotenv(env_file("T_PCT=0.5    # share of leverage deployed\n"))
    assert os.environ["T_PCT"] == "0.5"


def test_full_line_comments_and_blanks_ignored(env_file) -> None:
    load_dotenv(env_file("# header\n\n   \nT_B=2\n# trailing\n"))
    assert os.environ["T_B"] == "2"
    assert "# header" not in os.environ


def test_export_prefix_supported(env_file) -> None:
    load_dotenv(env_file("export T_C=3\n"))
    assert os.environ["T_C"] == "3"


def test_quotes_are_stripped(env_file) -> None:
    load_dotenv(env_file("T_D=\"quoted value\"\nT_E='single'\n"))
    assert os.environ["T_D"] == "quoted value"
    assert os.environ["T_E"] == "single"


def test_quoted_value_keeps_its_hash(env_file) -> None:
    """A quoted value is literal — a secret containing # must survive."""
    load_dotenv(env_file("T_F=\"abc#def\"\n"))
    assert os.environ["T_F"] == "abc#def"


def test_hash_without_leading_space_is_kept(env_file) -> None:
    load_dotenv(env_file("T_G=abc#def\n"))
    assert os.environ["T_G"] == "abc#def"


def test_existing_environment_wins(env_file, monkeypatch) -> None:
    """`FOO=x python -m arcusbot` must override the file, not the reverse."""
    path = env_file("T_H=from-file\n")      # fixture clears T_* first
    monkeypatch.setenv("T_H", "from-shell")
    load_dotenv(path)
    assert os.environ["T_H"] == "from-shell"


def test_value_containing_equals_is_preserved(env_file) -> None:
    load_dotenv(env_file("T_I=a=b=c\n"))
    assert os.environ["T_I"] == "a=b=c"


def test_empty_value_is_allowed(env_file) -> None:
    load_dotenv(env_file("T_J=\n"))
    assert os.environ["T_J"] == ""


def test_missing_file_is_not_an_error(tmp_path) -> None:
    load_dotenv(tmp_path / "nope.env")


def test_urls_survive_intact(env_file) -> None:
    load_dotenv(env_file("T_K=https://testnet.arcus.xyz/ref/AAAA   # referral\n"))
    assert os.environ["T_K"] == "https://testnet.arcus.xyz/ref/AAAA"


@pytest.mark.parametrize("raw,want", [
    ("value", "value"),
    ("value # note", "value"),
    ("value\t# note", "value"),
    ("#leading", ""),
    ("a#b", "a#b"),
    ("  spaced  # note", "spaced"),
])
def test_strip_inline_comment_cases(raw, want) -> None:
    assert _strip_inline_comment(raw) == want


def test_shipped_env_example_parses_cleanly(env_file, monkeypatch) -> None:
    """The template we tell people to copy must actually load."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    if not example.is_file():
        pytest.skip(".env.example not present")
    for key in list(os.environ):
        if key.startswith(("ARCUS_", "BOT_", "RISK_", "SIM_")):
            monkeypatch.delenv(key, raising=False)
    load_dotenv(example)
    # Spot-check values of each type that previously broke.
    assert int(os.environ["ARCUS_ACCOUNT_INDEX"]) == 0
    assert float(os.environ["BOT_CAPITAL_UTILISATION"]) == 0.5
    assert int(os.environ["BOT_CAPITAL_CLIPS"]) >= 1
    assert os.environ["ARCUS_REFERRAL_TESTNET"] == "AAAA"
