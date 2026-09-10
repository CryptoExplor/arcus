"""Durable state across restarts.

Without this the bot is amnesiac: every restart resets the drawdown baseline,
the daily-loss counter and the lifetime volume that drives the fee tier. That
turns the kill switch into a suggestion — crash-loop a bot with a $25 drawdown
limit and it will happily lose $25 per restart, forever, while each individual
session reports itself as healthy.

What is persisted (``state/session.json``):

``lifetime``
    Cumulative volume, fees and realized PnL across every session. Feeds the
    fee-tier estimate and the VIP milestone, both of which are lifetime
    quantities, not per-session ones.
``day``
    Rolling UTC-day realized PnL and volume, for ``RISK_MAX_DAILY_LOSS_USD``.
    Rolls over automatically at midnight UTC.
``risk``
    The peak net PnL and equity high-water marks that define drawdown.
``sessions``
    A bounded history of recent runs, for `python -m arcusbot history`.

Design notes
------------
* **Writes are atomic** (temp file + ``os.replace``). A half-written state file
  read on the next boot would be worse than none at all.
* **Corruption is survivable.** A malformed file is moved aside and the bot
  starts fresh with a warning rather than refusing to run.
* **Restart carry-over is opt-out** (``BOT_PERSIST_STATE=false``) because
  backtests, sweeps and CI want a clean slate every time.
* PnL *positions* are deliberately NOT restored from here. The exchange is the
  authority on inventory; the bot re-reads positions at startup. Persisting
  them would risk contradicting the venue.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .scaling import D, dec_str

log = logging.getLogger("arcusbot.session")

STATE_VERSION = 1
MAX_SESSION_HISTORY = 50


def _utc_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


@dataclass
class LifetimeStats:
    volume_usd: Decimal = Decimal(0)
    maker_volume_usd: Decimal = Decimal(0)
    fees_paid_usd: Decimal = Decimal(0)
    rebates_usd: Decimal = Decimal(0)
    realized_pnl_usd: Decimal = Decimal(0)
    fills: int = 0
    sessions: int = 0
    first_seen: float = field(default_factory=time.time)

    @property
    def net_pnl_usd(self) -> Decimal:
        return self.realized_pnl_usd + self.rebates_usd - self.fees_paid_usd

    def bps_of_volume(self) -> Decimal:
        if self.volume_usd <= 0:
            return Decimal(0)
        return self.net_pnl_usd / self.volume_usd * Decimal(10_000)


@dataclass
class DayStats:
    day: str = field(default_factory=_utc_day)
    volume_usd: Decimal = Decimal(0)
    realized_pnl_usd: Decimal = Decimal(0)
    fees_paid_usd: Decimal = Decimal(0)
    rebates_usd: Decimal = Decimal(0)

    @property
    def net_pnl_usd(self) -> Decimal:
        return self.realized_pnl_usd + self.rebates_usd - self.fees_paid_usd

    def roll_if_needed(self, now: float | None = None) -> bool:
        """Reset at UTC midnight. Returns True if a rollover happened."""
        today = _utc_day(now)
        if today == self.day:
            return False
        self.day = today
        self.volume_usd = Decimal(0)
        self.realized_pnl_usd = Decimal(0)
        self.fees_paid_usd = Decimal(0)
        self.rebates_usd = Decimal(0)
        return True


@dataclass
class RiskState:
    """High-water marks that define drawdown across restarts."""

    peak_net_pnl_usd: Decimal = Decimal(0)
    peak_equity_usd: Decimal | None = None
    consecutive_crashes: int = 0
    last_exit_reason: str = ""
    last_exit_ts: float = 0.0


class SessionStore:
    """Load/save the durable state file."""

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.lifetime = LifetimeStats()
        self.day = DayStats()
        self.risk = RiskState()
        self.history: list[dict[str, Any]] = []
        self.loaded_from_disk = False

    # ------------------------------------------------------------- loading --
    def load(self) -> "SessionStore":
        if not self.enabled or not self.path.is_file():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Never let a bad state file stop the bot from running.
            backup = self.path.with_suffix(".corrupt")
            with_suppress_oserror(lambda: self.path.replace(backup))
            log.warning("session state unreadable (%s); moved to %s and starting fresh",
                        exc, backup.name)
            return self

        if int(raw.get("version", 0)) != STATE_VERSION:
            log.warning("session state version %s != %s — starting fresh",
                        raw.get("version"), STATE_VERSION)
            return self

        self.lifetime = _lifetime_from(raw.get("lifetime", {}))
        self.day = _day_from(raw.get("day", {}))
        self.risk = _risk_from(raw.get("risk", {}))
        self.history = list(raw.get("sessions", []))[-MAX_SESSION_HISTORY:]
        self.loaded_from_disk = True

        if self.day.roll_if_needed():
            log.info("new UTC day (%s) — daily loss counter reset", self.day.day)
        return self

    # -------------------------------------------------------------- saving --
    def save(self) -> None:
        if not self.enabled:
            return
        payload = {
            "version": STATE_VERSION,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "lifetime": _dec_dict(asdict(self.lifetime)),
            "day": _dec_dict(asdict(self.day)),
            "risk": _dec_dict(asdict(self.risk)),
            "sessions": self.history[-MAX_SESSION_HISTORY:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)      # atomic: never a half-written file
        except OSError as exc:
            log.warning("could not persist session state: %s", exc)

    # ---------------------------------------------------------- accounting --
    def start_session(self, session_id: str, cfg_summary: dict[str, Any]) -> None:
        self.day.roll_if_needed()
        self.lifetime.sessions += 1
        self.history.append({
            "sessionId": session_id,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "running",
            **cfg_summary,
        })
        self.history = self.history[-MAX_SESSION_HISTORY:]

    def finish_session(self, session_id: str, pnl_snapshot: dict[str, Any],
                       exit_reason: str) -> None:
        """Fold a completed session into the cumulative totals."""
        self.day.roll_if_needed()

        volume = D(pnl_snapshot.get("volumeUsd", 0))
        maker_volume = D(pnl_snapshot.get("makerVolumeUsd", 0))
        fees = D(pnl_snapshot.get("feesPaid", 0))
        rebates = D(pnl_snapshot.get("rebatesEarned", 0))
        realized = D(pnl_snapshot.get("realizedPnl", 0))
        fills = int(pnl_snapshot.get("fillCount", 0) or 0)

        self.lifetime.volume_usd += volume
        self.lifetime.maker_volume_usd += maker_volume
        self.lifetime.fees_paid_usd += fees
        self.lifetime.rebates_usd += rebates
        self.lifetime.realized_pnl_usd += realized
        self.lifetime.fills += fills

        self.day.volume_usd += volume
        self.day.fees_paid_usd += fees
        self.day.rebates_usd += rebates
        self.day.realized_pnl_usd += realized

        self.risk.last_exit_reason = exit_reason
        self.risk.last_exit_ts = time.time()
        # A clean, intentional stop clears the crash counter; anything else
        # (error storm, kill switch, killed process) counts against it.
        if _is_clean_exit(exit_reason):
            self.risk.consecutive_crashes = 0
        else:
            self.risk.consecutive_crashes += 1

        for row in reversed(self.history):
            if row.get("sessionId") == session_id:
                row.update({
                    "status": "finished",
                    "endedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "exitReason": exit_reason,
                    "volumeUsd": dec_str(_q(volume)),
                    "netPnl": dec_str(_q(D(pnl_snapshot.get("netPnl", 0)))),
                    "netBpsOfVolume": dec_str(
                        D(pnl_snapshot.get("netBpsOfVolume", 0)).quantize(Decimal("0.01"))),
                    "fills": fills,
                })
                break
        self.save()

    def note_equity(self, equity: Decimal | None) -> None:
        if equity is None:
            return
        if self.risk.peak_equity_usd is None or equity > self.risk.peak_equity_usd:
            self.risk.peak_equity_usd = equity

    # ------------------------------------------------------------ readouts --
    def carried_daily_loss(self) -> Decimal:
        """Loss already booked today, as a positive number."""
        net = self.day.net_pnl_usd
        return -net if net < 0 else Decimal(0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "loadedFromDisk": self.loaded_from_disk,
            "path": str(self.path),
            "lifetime": {
                "sessions": self.lifetime.sessions,
                "fills": self.lifetime.fills,
                "volumeUsd": dec_str(self.lifetime.volume_usd),
                "makerVolumeUsd": dec_str(self.lifetime.maker_volume_usd),
                "feesPaidUsd": dec_str(self.lifetime.fees_paid_usd),
                "rebatesUsd": dec_str(self.lifetime.rebates_usd),
                "realizedPnlUsd": dec_str(self.lifetime.realized_pnl_usd),
                "netPnlUsd": dec_str(self.lifetime.net_pnl_usd),
                "netBpsOfVolume": dec_str(self.lifetime.bps_of_volume()),
                "firstSeen": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime(self.lifetime.first_seen)),
            },
            "day": {
                "day": self.day.day,
                "volumeUsd": dec_str(self.day.volume_usd),
                "netPnlUsd": dec_str(self.day.net_pnl_usd),
                "lossSoFarUsd": dec_str(self.carried_daily_loss()),
            },
            "risk": {
                "peakNetPnlUsd": dec_str(self.risk.peak_net_pnl_usd),
                "peakEquityUsd": dec_str(self.risk.peak_equity_usd)
                if self.risk.peak_equity_usd is not None else None,
                "consecutiveCrashes": self.risk.consecutive_crashes,
                "lastExitReason": self.risk.last_exit_reason,
            },
        }

    def describe(self) -> str:
        lt = self.lifetime
        return (
            f"lifetime: {lt.sessions} sessions, {lt.fills} fills, "
            f"${dec_str(lt.volume_usd.quantize(Decimal('0.01')))} volume, "
            f"net ${dec_str(lt.net_pnl_usd.quantize(Decimal('0.01')))} "
            f"({dec_str(lt.bps_of_volume().quantize(Decimal('0.01')))} bps) | "
            f"today: ${dec_str(self.day.volume_usd.quantize(Decimal('0.01')))} volume, "
            f"net ${dec_str(self.day.net_pnl_usd.quantize(Decimal('0.01')))}"
        )


# --------------------------------------------------------------- helpers ----
CLEAN_EXITS = ("max runtime", "volume target", "signal", "completed", "cancelled")


def _is_clean_exit(reason: str) -> bool:
    low = (reason or "").lower()
    return any(token in low for token in CLEAN_EXITS)


def with_suppress_oserror(fn) -> None:
    try:
        fn()
    except OSError:
        pass


def _dec_dict(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def _lifetime_from(raw: dict[str, Any]) -> LifetimeStats:
    return LifetimeStats(
        volume_usd=D(raw.get("volume_usd", 0)),
        maker_volume_usd=D(raw.get("maker_volume_usd", 0)),
        fees_paid_usd=D(raw.get("fees_paid_usd", 0)),
        rebates_usd=D(raw.get("rebates_usd", 0)),
        realized_pnl_usd=D(raw.get("realized_pnl_usd", 0)),
        fills=int(raw.get("fills", 0) or 0),
        sessions=int(raw.get("sessions", 0) or 0),
        first_seen=float(raw.get("first_seen", time.time())),
    )


def _day_from(raw: dict[str, Any]) -> DayStats:
    return DayStats(
        day=str(raw.get("day", _utc_day())),
        volume_usd=D(raw.get("volume_usd", 0)),
        realized_pnl_usd=D(raw.get("realized_pnl_usd", 0)),
        fees_paid_usd=D(raw.get("fees_paid_usd", 0)),
        rebates_usd=D(raw.get("rebates_usd", 0)),
    )


def _risk_from(raw: dict[str, Any]) -> RiskState:
    peak_equity = raw.get("peak_equity_usd")
    return RiskState(
        peak_net_pnl_usd=D(raw.get("peak_net_pnl_usd", 0)),
        peak_equity_usd=D(peak_equity) if peak_equity not in (None, "", "None") else None,
        consecutive_crashes=int(raw.get("consecutive_crashes", 0) or 0),
        last_exit_reason=str(raw.get("last_exit_reason", "")),
        last_exit_ts=float(raw.get("last_exit_ts", 0) or 0),
    )


def _q(value: Decimal) -> Decimal:
    """Round money to cents for display/storage."""
    return D(value).quantize(Decimal("0.01"))
