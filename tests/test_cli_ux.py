"""Beginner-facing behaviour: mistakes must produce advice, not tracebacks.

Someone running this for the first time will get things wrong — no network, a
market name without the dash, no API keys. Each of those should print the next
command to try. These tests exist because a stack trace is a dead end for a
newcomer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.cli import _explain, main  # noqa: E402


# ------------------------------------------------------------- error advice --


@pytest.mark.parametrize("message,expected", [
    ("HTTP 0 on /v1/markets: network error: TLS/SSL closed", "--venue sim"),
    ("none of ['BTCUSD'] exist on the venue", "BTC-USD"),
    ("HTTP 401 Unauthorized", "onboard.py"),
    ("refusing to trade live on mainnet: ...", "BOT_OPERATIONS.md"),
])
def test_common_failures_explain_the_next_step(message: str, expected: str) -> None:
    advice = _explain(RuntimeError(message))
    assert advice, f"no advice offered for: {message}"
    assert expected in advice


def test_unknown_errors_do_not_pretend_to_have_advice() -> None:
    assert _explain(RuntimeError("something entirely unexpected")) == ""


def test_a_crash_becomes_a_message_not_a_traceback(capsys) -> None:
    """main() must convert an unexpected exception into exit code 1 + advice."""
    code = main(["quote", "--venue", "sim", "--markets", "BTCUSD"])
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "BTC-USD" in err, "should suggest the correctly formatted name"


def test_market_typo_suggests_the_real_name(capsys) -> None:
    main(["quote", "--venue", "sim", "--markets", "ETHUSD"])
    assert "ETH-USD" in capsys.readouterr().err


def test_live_without_credentials_explains_the_offline_path(capsys) -> None:
    code = main(["run", "--venue", "arcus", "--mode", "live", "--duration", "1"])
    assert code == 2
    err = capsys.readouterr().err
    assert "--venue sim" in err, "must point beginners at the offline path"


def test_offline_selftest_needs_no_credentials() -> None:
    """The advertised first command must work with an empty config."""
    assert main(["markets", "--venue", "sim"]) == 0
