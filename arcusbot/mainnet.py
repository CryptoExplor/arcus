"""The mainnet gate.

Everything in this module exists to make one thing hard: spending real money by
accident. Testnet mistakes cost nothing; mainnet mistakes cost the account.

The gate is **conjunctive and fail-closed**. Live mainnet execution requires ALL
of the following to be explicitly and correctly set:

===============================  ==========================================
``ARCUS_NETWORK=mainnet``        you meant mainnet
``BOT_MODE=live``                you meant live
``BOT_MAINNET_ENABLED=true``     explicit opt-in, no default, no inference
``BOT_MAINNET_CAPITAL_USD=20``   a hard cap, > 0 and <= the ceiling
``BOT_MAINNET_ACK=<phrase>``     a typed acknowledgement you cannot fat-finger
===============================  ==========================================

Design rules, in order of importance:

1. **Malformed means NO.** ``BOT_MAINNET_ENABLED=ture`` is not true. An
   unparseable capital figure is not a capital figure. Every parse failure
   denies the gate and says exactly which key was wrong. There is no code path
   where a typo opens the gate.
2. **The cap is enforced, not documented.** ``mainnet_capital_cap`` feeds the
   capital planner, and ``assert_capital_within_cap`` is re-checked by the risk
   manager every loop, so a mid-session re-size cannot exceed it either.
3. **Limits only ever tighten.** ``mainnet_risk_floor`` clamps drawdown, daily
   loss, position and inventory caps to fractions of the mainnet budget. If the
   operator configured something stricter, theirs wins.
4. **Dry-run is always allowed.** Reading mainnet market data is harmless and
   useful; only order placement is gated.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .scaling import dec_str

log = logging.getLogger("arcusbot.mainnet")

#: The phrase ``BOT_MAINNET_ACK`` must contain. Deliberately awkward to type.
ACK_PHRASE = "i-understand-the-risk"

#: Absolute ceiling on mainnet capital regardless of configuration. This is a
#: guard against a mistyped budget (``2000`` for ``20``), not a business rule.
#: Raise it consciously via BOT_MAINNET_MAX_CAPITAL_USD once you have a track
#: record; it can never be exceeded silently.
DEFAULT_MAX_MAINNET_CAPITAL_USD = Decimal("100")

TRUE_TOKENS = {"1", "true", "yes", "on"}
FALSE_TOKENS = {"", "0", "false", "no", "off"}


def strict_bool(raw: Any, key: str) -> tuple[bool, str | None]:
    """Parse a boolean that must not guess.

    Returns ``(value, error)``. Anything unrecognised is ``(False, reason)`` —
    a typo can only ever deny the gate, never open it.
    """
    if raw is None:
        return False, None
    token = str(raw).strip().lower()
    if token in TRUE_TOKENS:
        return True, None
    if token in FALSE_TOKENS:
        return False, None
    return False, (
        f"{key}={raw!r} is not a recognised boolean "
        f"(use true/false); refusing to interpret it as true"
    )


def strict_decimal(raw: Any, key: str) -> tuple[Decimal | None, str | None]:
    if raw is None or str(raw).strip() == "":
        return None, None
    try:
        return Decimal(str(raw).strip()), None
    except (InvalidOperation, ValueError):
        return None, f"{key}={raw!r} is not a valid number"


@dataclass
class GateResult:
    """Outcome of evaluating the mainnet gate."""

    allowed: bool
    reasons: list[str] = field(default_factory=list)
    capital_cap: Decimal = Decimal(0)
    is_mainnet_live_attempt: bool = False

    def describe(self) -> str:
        if not self.is_mainnet_live_attempt:
            return "mainnet gate: not applicable (not a mainnet live run)"
        if self.allowed:
            return (f"mainnet gate: OPEN — capital capped at "
                    f"${dec_str(self.capital_cap)}")
        return "mainnet gate: CLOSED — " + "; ".join(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.is_mainnet_live_attempt,
            "allowed": self.allowed,
            "capitalCapUsd": dec_str(self.capital_cap),
            "reasons": list(self.reasons),
        }


def _env(name: str) -> Any:
    value = os.environ.get(name)
    return None if value is None or value == "" else value


def evaluate_gate(cfg: Any, env: dict[str, Any] | None = None) -> GateResult:
    """Decide whether live mainnet execution is permitted.

    ``env`` is injectable for testing; it defaults to the process environment.
    Reads the environment directly rather than the parsed config so that a
    malformed value is seen as malformed rather than silently defaulted.
    """
    getenv = (lambda k: env.get(k) if env is not None else _env(k))

    is_mainnet = str(getattr(cfg, "network", "")).lower() == "mainnet"
    is_live = str(getattr(cfg, "mode", "")).lower() == "live"
    # Trading against the simulator is never real money, whatever the labels say.
    is_sim = str(getattr(cfg, "venue", "arcus")).lower() == "sim"

    if not (is_mainnet and is_live and not is_sim):
        return GateResult(allowed=True, is_mainnet_live_attempt=False)

    reasons: list[str] = []

    enabled, err = strict_bool(getenv("BOT_MAINNET_ENABLED"), "BOT_MAINNET_ENABLED")
    if err:
        reasons.append(err)
    elif not enabled:
        reasons.append("BOT_MAINNET_ENABLED is not true (explicit opt-in required)")

    ack = str(getenv("BOT_MAINNET_ACK") or "").strip().lower()
    if ack != ACK_PHRASE:
        reasons.append(
            f"BOT_MAINNET_ACK must be exactly {ACK_PHRASE!r}"
            + (f" (got {ack!r})" if ack else " (unset)")
        )

    ceiling, err = strict_decimal(getenv("BOT_MAINNET_MAX_CAPITAL_USD"),
                                  "BOT_MAINNET_MAX_CAPITAL_USD")
    if err:
        reasons.append(err)
        ceiling = DEFAULT_MAX_MAINNET_CAPITAL_USD
    ceiling = ceiling if ceiling is not None else DEFAULT_MAX_MAINNET_CAPITAL_USD

    cap, err = strict_decimal(getenv("BOT_MAINNET_CAPITAL_USD"), "BOT_MAINNET_CAPITAL_USD")
    if err:
        reasons.append(err)
        cap = Decimal(0)
    elif cap is None:
        reasons.append("BOT_MAINNET_CAPITAL_USD is required for mainnet live "
                       "(the hard spending cap, e.g. 20)")
        cap = Decimal(0)
    elif cap <= 0:
        reasons.append(f"BOT_MAINNET_CAPITAL_USD must be > 0 (got {dec_str(cap)})")
    elif cap > ceiling:
        reasons.append(
            f"BOT_MAINNET_CAPITAL_USD ${dec_str(cap)} exceeds the ceiling "
            f"${dec_str(ceiling)}. If that was deliberate raise "
            f"BOT_MAINNET_MAX_CAPITAL_USD consciously; if it was a typo, "
            f"this just saved you ${dec_str(cap - ceiling)}."
        )

    allowed = not reasons
    return GateResult(
        allowed=allowed,
        reasons=reasons,
        capital_cap=cap if allowed else Decimal(0),
        is_mainnet_live_attempt=True,
    )


def mainnet_capital_cap(cfg: Any, env: dict[str, Any] | None = None) -> Decimal | None:
    """The hard capital cap for this run, or None when not gated."""
    gate = evaluate_gate(cfg, env)
    if gate.is_mainnet_live_attempt and gate.allowed:
        return gate.capital_cap
    return None


def assert_capital_within_cap(deployable: Decimal, cap: Decimal | None) -> str | None:
    """Re-assert the cap after any (re-)sizing. Returns an error, or None.

    Called by the risk manager every loop so that an equity-driven mid-session
    re-size cannot quietly exceed the mainnet budget.
    """
    if cap is None:
        return None
    if deployable > cap:
        return (f"deployable ${dec_str(deployable)} exceeds the mainnet cap "
                f"${dec_str(cap)}")
    return None


#: Risk limits as a fraction of the mainnet capital cap. A $20 account cannot
#: meaningfully use a $25 drawdown limit; these make the defaults proportional.
MAINNET_RISK_FRACTIONS = {
    "max_drawdown_usd": Decimal("0.15"),          # 15% of budget
    "max_daily_loss_usd": Decimal("0.20"),        # 20% of budget
    "max_position_notional_usd": Decimal("1.5"),  # 1.5x budget (leverage aware)
    "max_inventory_notional_usd": Decimal("1.0"),
}


def mainnet_risk_floor(cap: Decimal) -> dict[str, Decimal]:
    """Proportional risk limits for a mainnet budget."""
    return {key: (cap * frac).quantize(Decimal("0.01"))
            for key, frac in MAINNET_RISK_FRACTIONS.items()}


def apply_mainnet_limits(cfg: Any, cap: Decimal) -> list[str]:
    """Clamp config limits to the mainnet floor. Only ever tightens.

    Returns a human-readable list of what changed, for the startup log.
    """
    applied: list[str] = []
    for key, limit in mainnet_risk_floor(cap).items():
        current = getattr(cfg, key, None)
        if current is None:
            continue
        if Decimal(current) > limit:
            setattr(cfg, key, limit)
            applied.append(f"{key} ${dec_str(Decimal(current))} -> ${dec_str(limit)}")
    # The capital budget itself is capped too, whichever way it was expressed.
    if getattr(cfg, "capital_usd", Decimal(0)) == 0 and getattr(cfg, "capital_pct", Decimal(0)) == 0:
        cfg.capital_usd = cap
        applied.append(f"capital_usd unset -> ${dec_str(cap)} (mainnet cap)")
    elif getattr(cfg, "capital_usd", Decimal(0)) > cap:
        applied.append(f"capital_usd ${dec_str(cfg.capital_usd)} -> ${dec_str(cap)}")
        cfg.capital_usd = cap
    return applied
