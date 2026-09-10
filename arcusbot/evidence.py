"""Session evidence — the 22 metrics that decide whether a strategy works.

This module exists to make a specific failure impossible: declaring success on
the basis of a short lucky run, or on unrealized PnL that never gets realized.

The rules encoded here:

  * A session is economically successful only when
    ``realized + unrealized + funding + rebates - fees > 0``.
    Unrealized-only "profit" is explicitly called out, not counted as a win.
  * ``netBpsOfVolume`` is the headline number, not absolute dollars.
  * A verdict is only offered once the sample is large enough to mean
    something; below that the honest answer is INSUFFICIENT-DATA.

Nothing in here modifies trading behaviour. It observes and records.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .scaling import D, dec_str

# Below this many fills, per-session numbers are noise. Chosen so that a
# maker/maker round trip (2 fills) has ~15 independent observations.
MIN_FILLS_FOR_VERDICT = 30

# Aggregate sample required before any claim about edge is credible.
MIN_FILLS_FOR_EDGE_CLAIM = 400
MIN_SESSIONS_FOR_EDGE_CLAIM = 5


def _q(value: Any, places: str = "0.01") -> str:
    return dec_str(D(value).quantize(Decimal(places)))


@dataclass
class SessionEvidence:
    """One session's raw, unmassaged numbers."""

    session_id: str = ""
    label: str = ""
    regime: str = ""
    network: str = "testnet"
    venue: str = "arcus"
    markets: list[str] = field(default_factory=list)
    started_at: float = 0.0
    runtime_s: float = 0.0

    # --- execution ---
    volume_usd: Decimal = Decimal(0)
    fills: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    orders_sent: int = 0
    orders_rejected: int = 0
    cancels_sent: int = 0

    # --- economics ---
    gross_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    rebates: Decimal = Decimal(0)
    funding_pnl: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)

    # --- quality ---
    slippage_bps: Decimal = Decimal(0)
    adverse_selection_bps: Decimal = Decimal(0)

    # --- inventory ---
    avg_inventory_usd: Decimal = Decimal(0)
    max_inventory_usd: Decimal = Decimal(0)
    max_inventory_age_s: float = 0.0
    max_drawdown_usd: Decimal = Decimal(0)

    # --- reliability ---
    api_errors: int = 0
    ws_disconnects: int = 0
    reconciliations: int = 0
    reconcile_discrepancies: int = 0
    flatten_attempted: bool = False
    flatten_succeeded: bool = False
    residual_positions: dict[str, str] = field(default_factory=dict)
    exit_reason: str = ""

    # --- config actually used (so a result can be reproduced) ---
    params: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ derived --
    @property
    def net_pnl(self) -> Decimal:
        """The only number that counts as profit.

        Deliberately recomputed from components rather than trusting a
        reported total, so an accounting bug shows up as a mismatch.
        """
        return (self.realized_pnl + self.unrealized_pnl + self.funding_pnl
                + self.rebates - self.fees_paid)

    @property
    def net_bps_of_volume(self) -> Decimal:
        if self.volume_usd <= 0:
            return Decimal(0)
        return self.net_pnl / self.volume_usd * Decimal(10_000)

    @property
    def fee_coverage_ratio(self) -> Decimal:
        if self.fees_paid <= 0:
            return Decimal(0)
        return (self.gross_pnl + self.rebates) / self.fees_paid

    @property
    def maker_ratio(self) -> Decimal:
        if self.fills <= 0:
            return Decimal(0)
        return Decimal(self.maker_fills) / Decimal(self.fills)

    @property
    def fill_rate(self) -> Decimal:
        if self.orders_sent <= 0:
            return Decimal(0)
        return Decimal(self.fills) / Decimal(self.orders_sent)

    @property
    def rejection_rate(self) -> Decimal:
        if self.orders_sent <= 0:
            return Decimal(0)
        return Decimal(self.orders_rejected) / Decimal(self.orders_sent)

    @property
    def realized_only_net(self) -> Decimal:
        """Net excluding unrealized — the conservative view."""
        return (self.realized_pnl + self.funding_pnl + self.rebates
                - self.fees_paid)

    @property
    def profitable(self) -> bool:
        return self.net_pnl > 0

    @property
    def unrealized_dependent(self) -> bool:
        """True when the session only looks profitable thanks to open marks.

        This is the trap the reviewer flagged: an inventory position marked
        favourably is not profit until it is closed.
        """
        return self.net_pnl > 0 >= self.realized_only_net

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.fills < MIN_FILLS_FOR_VERDICT:
            out.append(f"only {self.fills} fills — below the {MIN_FILLS_FOR_VERDICT} "
                       f"needed for a per-session verdict")
        if self.unrealized_dependent:
            out.append("profit depends on UNREALIZED marks; realized-only net is "
                       f"${_q(self.realized_only_net)} — not a demonstrated gain")
        if self.residual_positions:
            out.append(f"exited with open position(s): {self.residual_positions}")
        if self.flatten_attempted and not self.flatten_succeeded:
            out.append("flatten did not confirm flat")
        if self.reconcile_discrepancies:
            out.append(f"{self.reconcile_discrepancies} reconciliation discrepancy(ies)")
        if self.rejection_rate > Decimal("0.1"):
            out.append(f"high rejection rate {_q(self.rejection_rate * 100)}%")
        if self.ws_disconnects:
            out.append(f"{self.ws_disconnects} websocket disconnect(s)")
        return out

    def as_dict(self) -> dict[str, Any]:
        d = {k: (dec_str(v) if isinstance(v, Decimal) else v)
             for k, v in asdict(self).items()}
        d.update({
            "netPnl": _q(self.net_pnl, "0.0001"),
            "netBpsOfVolume": _q(self.net_bps_of_volume, "0.01"),
            "feeCoverageRatio": _q(self.fee_coverage_ratio, "0.001"),
            "makerRatio": _q(self.maker_ratio, "0.001"),
            "fillRate": _q(self.fill_rate, "0.001"),
            "rejectionRate": _q(self.rejection_rate, "0.001"),
            "realizedOnlyNet": _q(self.realized_only_net, "0.0001"),
            "unrealizedDependent": self.unrealized_dependent,
            "profitable": self.profitable,
            "warnings": self.warnings(),
        })
        return d

    def describe(self) -> str:
        head = (f"[{self.regime or 'session'}] {self.session_id} "
                f"{', '.join(self.markets)} · {int(self.runtime_s)}s")
        econ = (f"vol ${_q(self.volume_usd)} · {self.fills} fills "
                f"({_q(self.maker_ratio * 100, '0.1')}% maker) · "
                f"net ${_q(self.net_pnl, '0.0001')} "
                f"({_q(self.net_bps_of_volume)} bps) · "
                f"feeCoverage {_q(self.fee_coverage_ratio, '0.001')}x")
        lines = [head, "  " + econ]
        for w in self.warnings():
            lines.append(f"  ! {w}")
        return "\n".join(lines)

    # ------------------------------------------------------- construction --
    @classmethod
    def from_status(cls, status: dict[str, Any], *, regime: str = "",
                    label: str = "") -> "SessionEvidence":
        """Build from an Engine.status() payload."""
        pnl = status.get("pnl") or {}
        risk = status.get("risk") or {}
        ws = status.get("ws") or {}
        rec = status.get("reconcile") or {}
        markets = [m.get("market") for m in (status.get("markets") or []) if m.get("market")]

        # pnl.makerShare is a PERCENTAGE (0-100), not a fraction.
        maker_pct = D(pnl.get("makerShare") or 0)
        fills = int(pnl.get("fillCount") or 0)
        maker_fills = int(round(float(maker_pct) / 100.0 * fills)) if fills else 0
        maker_fills = max(0, min(maker_fills, fills))

        rejections = risk.get("rejections") or {}
        rejected = sum(int(v) for v in rejections.values()) if isinstance(rejections, dict) else 0

        return cls(
            session_id=str(pnl.get("sessionId") or ""),
            label=label,
            regime=regime,
            network=str(status.get("network") or "testnet"),
            venue=str(status.get("venue") or "arcus"),
            markets=markets,
            started_at=time.time() - float(pnl.get("runtimeSeconds") or 0),
            runtime_s=float(pnl.get("runtimeSeconds") or 0),
            volume_usd=D(pnl.get("volumeUsd") or 0),
            fills=fills,
            maker_fills=maker_fills,
            taker_fills=max(fills - maker_fills, 0),
            orders_sent=int(status.get("ordersSent") or 0),
            orders_rejected=rejected,
            cancels_sent=int(status.get("cancelsSent") or 0),
            gross_pnl=D(pnl.get("grossPnl") or 0),
            fees_paid=D(pnl.get("feesPaid") or 0),
            rebates=D(pnl.get("rebatesEarned") or 0),
            funding_pnl=D(pnl.get("fundingPnl") or 0),
            realized_pnl=D(pnl.get("realizedPnl") or 0),
            unrealized_pnl=D(pnl.get("unrealizedPnl") or 0),
            max_drawdown_usd=D(pnl.get("drawdown") or 0),
            api_errors=int(risk.get("totalErrors") or 0),
            ws_disconnects=int(ws.get("reconnects") or 0) if isinstance(ws, dict) else 0,
            reconciliations=int(rec.get("reconciles") or 0),
            flatten_attempted=bool(status.get("finalStateConfirmed") is not None),
            flatten_succeeded=not bool(status.get("residualPositions")),
            residual_positions=dict(status.get("residualPositions") or {}),
            exit_reason=str(status.get("exitReason") or ""),
        )


# --------------------------------------------------------------- aggregate --


@dataclass
class Aggregate:
    """Verdict across many sessions. Refuses to conclude on thin data."""

    sessions: list[SessionEvidence] = field(default_factory=list)

    @property
    def total_volume(self) -> Decimal:
        return sum((s.volume_usd for s in self.sessions), Decimal(0))

    @property
    def total_fills(self) -> int:
        return sum(s.fills for s in self.sessions)

    @property
    def total_net(self) -> Decimal:
        return sum((s.net_pnl for s in self.sessions), Decimal(0))

    @property
    def total_realized_only(self) -> Decimal:
        return sum((s.realized_only_net for s in self.sessions), Decimal(0))

    @property
    def total_fees(self) -> Decimal:
        return sum((s.fees_paid for s in self.sessions), Decimal(0))

    @property
    def total_gross(self) -> Decimal:
        return sum((s.gross_pnl for s in self.sessions), Decimal(0))

    @property
    def net_bps_of_volume(self) -> Decimal:
        if self.total_volume <= 0:
            return Decimal(0)
        return self.total_net / self.total_volume * Decimal(10_000)

    @property
    def fee_coverage_ratio(self) -> Decimal:
        if self.total_fees <= 0:
            return Decimal(0)
        return self.total_gross / self.total_fees

    @property
    def maker_ratio(self) -> Decimal:
        fills = self.total_fills
        if fills <= 0:
            return Decimal(0)
        return Decimal(sum(s.maker_fills for s in self.sessions)) / Decimal(fills)

    def bps_series(self) -> list[float]:
        return [float(s.net_bps_of_volume) for s in self.sessions if s.volume_usd > 0]

    def bps_stdev(self) -> float:
        series = self.bps_series()
        return statistics.stdev(series) if len(series) > 1 else 0.0

    def bps_stderr(self) -> float:
        """Standard error of the mean — the honest error bar."""
        series = self.bps_series()
        if len(series) < 2:
            return 0.0
        return statistics.stdev(series) / math.sqrt(len(series))

    def t_statistic(self) -> float:
        """How many standard errors the mean edge sits above zero.

        Zero variance is a degenerate case: with several sessions all showing
        the same sign, the mean is as far from zero as the data can express, so
        report a large statistic rather than 0 (which would read as "no
        evidence" for the most consistent possible result). A single session
        still yields 0 — one observation is never evidence.
        """
        series = self.bps_series()
        if len(series) < 2:
            return 0.0
        mean = statistics.fmean(series)
        se = self.bps_stderr()
        if se <= 0:
            return 0.0 if mean == 0 else math.copysign(999.0, mean)
        return mean / se

    def credible_positive_edge(self) -> bool:
        """A positive edge we would actually bet on.

        Requires the sample to be large enough AND the mean to clear roughly
        two standard errors. This is intentionally strict: the cost of a false
        positive is real money.
        """
        if (self.total_fills < MIN_FILLS_FOR_EDGE_CLAIM
                or len(self.sessions) < MIN_SESSIONS_FOR_EDGE_CLAIM):
            return False
        return (self.total_net > 0
                and self.total_realized_only > 0
                and self.fee_coverage_ratio > 1
                and self.t_statistic() >= 2.0)

    def blockers(self) -> list[str]:
        """Everything standing between here and a mainnet experiment."""
        out: list[str] = []
        if len(self.sessions) < MIN_SESSIONS_FOR_EDGE_CLAIM:
            out.append(f"only {len(self.sessions)} session(s); need "
                       f"{MIN_SESSIONS_FOR_EDGE_CLAIM}+ across different regimes")
        if self.total_fills < MIN_FILLS_FOR_EDGE_CLAIM:
            out.append(f"only {self.total_fills} fills; need "
                       f"{MIN_FILLS_FOR_EDGE_CLAIM}+ for a statistical claim")
        if self.total_net <= 0:
            out.append(f"aggregate net PnL is ${_q(self.total_net, '0.0001')} (not positive)")
        if self.total_realized_only <= 0:
            out.append(f"realized-only net is ${_q(self.total_realized_only, '0.0001')} "
                       f"(not positive) — unrealized marks do not count")
        if self.fee_coverage_ratio <= 1:
            out.append(f"fee coverage {_q(self.fee_coverage_ratio, '0.001')}x <= 1 "
                       f"(the edge does not pay for the fees)")
        t = self.t_statistic()
        if self.bps_series() and t < 2.0:
            out.append(f"edge is not statistically distinguishable from zero "
                       f"(t={t:.2f}, need >= 2.0)")
        for s in self.sessions:
            if s.residual_positions:
                out.append(f"session {s.session_id} exited with open positions")
            if s.reconcile_discrepancies:
                out.append(f"session {s.session_id} had reconciliation discrepancies")
            if s.flatten_attempted and not s.flatten_succeeded:
                out.append(f"session {s.session_id} failed to flatten")
        return out

    def verdict(self) -> str:
        if not self.sessions:
            return "NO-DATA"
        if self.total_fills < MIN_FILLS_FOR_EDGE_CLAIM or \
                len(self.sessions) < MIN_SESSIONS_FOR_EDGE_CLAIM:
            return "INSUFFICIENT-DATA"
        if self.credible_positive_edge():
            return "POSITIVE-EDGE"
        if self.total_net <= 0:
            return "NEGATIVE-EDGE"
        return "INCONCLUSIVE"

    def ready_for_mainnet(self) -> bool:
        return self.verdict() == "POSITIVE-EDGE" and not self.blockers()

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessions": len(self.sessions),
            "totalVolumeUsd": _q(self.total_volume),
            "totalFills": self.total_fills,
            "totalGrossPnl": _q(self.total_gross, "0.0001"),
            "totalFees": _q(self.total_fees, "0.0001"),
            "totalNetPnl": _q(self.total_net, "0.0001"),
            "realizedOnlyNet": _q(self.total_realized_only, "0.0001"),
            "netBpsOfVolume": _q(self.net_bps_of_volume, "0.01"),
            "feeCoverageRatio": _q(self.fee_coverage_ratio, "0.001"),
            "makerRatio": _q(self.maker_ratio, "0.001"),
            "bpsStdev": round(self.bps_stdev(), 3),
            "bpsStdErr": round(self.bps_stderr(), 3),
            "tStatistic": round(self.t_statistic(), 3),
            "verdict": self.verdict(),
            "readyForMainnet": self.ready_for_mainnet(),
            "blockers": self.blockers(),
            "regimesCovered": sorted({s.regime for s in self.sessions if s.regime}),
        }

    def report(self) -> str:
        d = self.as_dict()
        lines = [
            "=" * 72,
            "TESTNET VALIDATION EVIDENCE",
            "=" * 72,
            f"sessions          {d['sessions']}   regimes: "
            f"{', '.join(d['regimesCovered']) or '(none tagged)'}",
            f"volume            ${d['totalVolumeUsd']}",
            f"fills             {d['totalFills']}  (maker {d['makerRatio']})",
            f"gross PnL         ${d['totalGrossPnl']}",
            f"fees              ${d['totalFees']}",
            f"NET PnL           ${d['totalNetPnl']}",
            f"realized-only     ${d['realizedOnlyNet']}",
            f"netBpsOfVolume    {d['netBpsOfVolume']} bps "
            f"(stderr {d['bpsStdErr']}, t={d['tStatistic']})",
            f"feeCoverageRatio  {d['feeCoverageRatio']}x",
            "-" * 72,
            f"VERDICT           {d['verdict']}",
            f"ready for mainnet {'YES' if d['readyForMainnet'] else 'NO'}",
        ]
        if d["blockers"]:
            lines.append("blockers:")
            lines.extend(f"  - {b}" for b in d["blockers"])
        lines.append("=" * 72)
        return "\n".join(lines)


# ------------------------------------------------------------- persistence --


def append_session(path: Path, evidence: SessionEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(evidence.as_dict()) + "\n")


def load_sessions(path: Path) -> list[SessionEvidence]:
    """Re-read recorded sessions. Tolerates a partially written last line."""
    if not path.exists():
        return []
    out: list[SessionEvidence] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(_from_record(raw))
    return out


def _from_record(raw: dict[str, Any]) -> SessionEvidence:
    """Rebuild from a record, ignoring the derived//computed keys.

    ``as_dict`` deliberately also emits derived values (netPnl, verdict flags)
    for readability. Those are recomputed on load, never assigned back.
    """
    ev = SessionEvidence()
    settable = {f for f in ev.__dataclass_fields__}
    for key, value in raw.items():
        if key not in settable:
            continue
        current = getattr(ev, key)
        if isinstance(current, Decimal):
            setattr(ev, key, D(value))
        elif isinstance(current, bool):
            setattr(ev, key, bool(value))
        elif isinstance(current, int) and not isinstance(current, bool):
            setattr(ev, key, int(value or 0))
        elif isinstance(current, float):
            setattr(ev, key, float(value or 0))
        else:
            setattr(ev, key, value)
    return ev


def aggregate(sessions: Iterable[SessionEvidence]) -> Aggregate:
    return Aggregate(sessions=list(sessions))
