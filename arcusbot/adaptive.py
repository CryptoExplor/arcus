"""Adaptive control — reacting to what the market is actually doing to us.

A static quoting policy is fine until conditions change. This module watches
the feedback the exchange gives back (fill rate, whether fills immediately go
against us, realised profitability, API health) and adjusts two levers:

  * ``edge_multiplier``  — how much spread to demand before quoting
  * ``interval_multiplier`` — how often to re-quote

The single most important rule here, and the reason the module exists: **the
bot never trades faster while it is losing money.** Volume is only worth
generating if it is not being paid for out of capital, so every signal that
says "this is going badly" widens the edge and slows the loop. Speeding up is
only ever permitted from a position of demonstrated profitability.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Deque

from .scaling import dec_str

# How many recent quotes/fills to reason over. Small enough to react within a
# few minutes, large enough not to twitch at a single unlucky fill.
WINDOW = 40

# Bounds on the levers. The bot may demand up to 4x its base edge and slow to
# 6x its base interval, but may never quote tighter than 85% of base — the
# fee-derived floor is enforced separately and must not be undercut here.
MIN_EDGE_MULT = Decimal("0.85")
MAX_EDGE_MULT = Decimal("4.0")
MIN_INTERVAL_MULT = Decimal("0.6")
MAX_INTERVAL_MULT = Decimal("6.0")

# A fill is "adversely selected" if the mid moved against us by more than this
# fraction of the edge we captured, shortly after the fill.
ADVERSE_FRACTION = Decimal("1.0")


@dataclass
class FillOutcome:
    """A fill plus what the market did to it immediately afterwards."""

    ts: float
    market: str
    side: str
    price: Decimal
    mid_at_fill: Decimal
    edge_bps: Decimal
    mid_after: Decimal | None = None

    def adverse_bps(self) -> Decimal:
        """How far the mid moved against this fill, in bps. Negative = good."""
        if self.mid_after is None or self.mid_at_fill <= 0:
            return Decimal(0)
        move = (self.mid_after - self.mid_at_fill) / self.mid_at_fill * 10_000
        # A buy is hurt by the price falling; a sell by it rising.
        return -move if self.side == "BUY" else move


@dataclass
class AdaptiveController:
    """Per-market adaptive state. Cheap to update, safe to consult every loop."""

    market: str
    quotes_placed: int = 0
    fills: int = 0
    outcomes: Deque[FillOutcome] = field(default_factory=lambda: deque(maxlen=WINDOW))
    pnl_marks: Deque[tuple[float, Decimal]] = field(default_factory=lambda: deque(maxlen=WINDOW))
    api_errors: Deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))
    rate_limited_until: float = 0.0
    _pending: Deque[FillOutcome] = field(default_factory=lambda: deque(maxlen=WINDOW))

    # ------------------------------------------------------------ ingestion --
    def note_quote(self) -> None:
        self.quotes_placed += 1

    def note_fill(self, side: str, price: Decimal, mid: Decimal, edge_bps: Decimal) -> None:
        self.fills += 1
        outcome = FillOutcome(ts=time.time(), market=self.market, side=side,
                              price=price, mid_at_fill=mid, edge_bps=edge_bps)
        self._pending.append(outcome)
        self.outcomes.append(outcome)

    def note_mid(self, mid: Decimal, settle_s: float = 5.0) -> None:
        """Resolve pending fills once enough time has passed to judge them."""
        now = time.time()
        while self._pending and (now - self._pending[0].ts) >= settle_s:
            self._pending.popleft().mid_after = mid

    def note_pnl(self, net: Decimal) -> None:
        self.pnl_marks.append((time.time(), net))

    def note_api_error(self) -> None:
        self.api_errors.append(time.time())

    def note_rate_limited(self, retry_after_s: float) -> None:
        self.rate_limited_until = max(self.rate_limited_until, time.time() + retry_after_s)

    # -------------------------------------------------------------- signals --
    def fill_rate(self) -> Decimal:
        if self.quotes_placed <= 0:
            return Decimal(0)
        return Decimal(self.fills) / Decimal(self.quotes_placed)

    def adverse_selection_bps(self) -> Decimal:
        """Mean adverse move across resolved fills. Positive = being picked off."""
        resolved = [o for o in self.outcomes if o.mid_after is not None]
        if not resolved:
            return Decimal(0)
        total = sum((o.adverse_bps() for o in resolved), Decimal(0))
        return total / Decimal(len(resolved))

    def adverse_ratio(self) -> Decimal:
        """Share of resolved fills that moved against us. 0.5 is neutral."""
        resolved = [o for o in self.outcomes if o.mid_after is not None]
        if not resolved:
            return Decimal("0.5")
        bad = sum(1 for o in resolved if o.adverse_bps() > o.edge_bps * ADVERSE_FRACTION)
        return Decimal(bad) / Decimal(len(resolved))

    def pnl_trend(self) -> Decimal:
        """Change in net PnL across the window. Negative = losing."""
        if len(self.pnl_marks) < 2:
            return Decimal(0)
        return self.pnl_marks[-1][1] - self.pnl_marks[0][1]

    def recent_errors(self, window_s: float = 60.0) -> int:
        cutoff = time.time() - window_s
        return sum(1 for ts in self.api_errors if ts >= cutoff)

    def is_rate_limited(self) -> bool:
        return time.time() < self.rate_limited_until

    # --------------------------------------------------------------- levers --
    def evaluate(self, *, vol_bps: Decimal | None = None,
                 spread_bps: Decimal | None = None,
                 depth_ratio: Decimal | None = None,
                 inventory_age_s: float = 0.0,
                 inventory_utilisation: Decimal = Decimal(0)) -> "Adjustment":
        """Fold every signal into two multipliers plus an explanation."""
        edge = Decimal("1")
        interval = Decimal("1")
        reasons: list[str] = []
        losing = self.pnl_trend() < 0

        # --- profitability: the overriding constraint -----------------------
        if losing:
            edge *= Decimal("1.35")
            interval *= Decimal("1.5")
            reasons.append(f"losing ${_money(-self.pnl_trend())} over the window "
                           f"— widening edge and slowing down")

        # --- adverse selection ----------------------------------------------
        adverse = self.adverse_selection_bps()
        ratio = self.adverse_ratio()
        if ratio > Decimal("0.6") and len(self.outcomes) >= 5:
            bump = Decimal("1") + (ratio - Decimal("0.5"))
            edge *= bump
            interval *= Decimal("1.2")
            reasons.append(
                f"{dec_str((ratio * 100).quantize(Decimal('1')))}% of fills moved against us "
                f"(mean {_bps(adverse)}bps) — quoting wider"
            )

        # --- volatility vs spread -------------------------------------------
        if vol_bps is not None and spread_bps is not None and spread_bps > 0:
            if vol_bps > spread_bps:
                edge *= Decimal("1") + min(vol_bps / spread_bps / Decimal("4"), Decimal("1"))
                reasons.append(
                    f"volatility {_bps(vol_bps)}bps exceeds spread {_bps(spread_bps)}bps"
                )

        # --- liquidity -------------------------------------------------------
        if depth_ratio is not None and depth_ratio < 3:
            edge *= Decimal("1.2")
            interval *= Decimal("1.3")
            reasons.append(f"thin book ({dec_str(depth_ratio.quantize(Decimal('0.1')))}x our clip)")

        # --- inventory --------------------------------------------------------
        if inventory_utilisation > Decimal("0.7"):
            interval *= Decimal("1.4")
            reasons.append("inventory near cap — prioritising reduction over new quotes")
        if inventory_age_s > 300:
            edge *= Decimal("1.15")
            reasons.append(f"inventory {int(inventory_age_s)}s old")

        # --- API health -------------------------------------------------------
        if self.is_rate_limited():
            interval *= Decimal("2.5")
            reasons.append("rate limited — backing off")
        errors = self.recent_errors()
        if errors >= 3:
            interval *= Decimal("1") + min(Decimal(errors) / Decimal("10"), Decimal("1.5"))
            reasons.append(f"{errors} API errors in the last minute")

        # --- speeding up: only from a position of strength ---------------------
        # Deliberately last, and deliberately conditional. Requires profit,
        # benign flow, a healthy API and evidence that our quotes are actually
        # trading. Anything else and we stay where we are.
        if (not losing and self.pnl_trend() > 0 and ratio < Decimal("0.4")
                and errors == 0 and not self.is_rate_limited()
                and self.fill_rate() > Decimal("0.1")
                and inventory_utilisation < Decimal("0.5")):
            interval *= Decimal("0.75")
            reasons.append("profitable with benign flow — increasing quote frequency")

        return Adjustment(
            edge_multiplier=_clamp(edge, MIN_EDGE_MULT, MAX_EDGE_MULT),
            interval_multiplier=_clamp(interval, MIN_INTERVAL_MULT, MAX_INTERVAL_MULT),
            reasons=reasons,
            metrics={
                "fillRate": dec_str(self.fill_rate().quantize(Decimal("0.001"))),
                "adverseBps": dec_str(adverse.quantize(Decimal("0.01"))),
                "adverseRatio": dec_str(ratio.quantize(Decimal("0.01"))),
                "pnlTrend": _money(self.pnl_trend()),
                "apiErrors60s": errors,
                "rateLimited": self.is_rate_limited(),
            },
        )


@dataclass
class Adjustment:
    edge_multiplier: Decimal
    interval_multiplier: Decimal
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def slowing_down(self) -> bool:
        return self.interval_multiplier > 1

    def describe(self) -> str:
        head = (f"edge x{dec_str(self.edge_multiplier.quantize(Decimal('0.01')))}, "
                f"interval x{dec_str(self.interval_multiplier.quantize(Decimal('0.01')))}")
        if not self.reasons:
            return head + " (nominal)"
        return head + " — " + "; ".join(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "edgeMultiplier": dec_str(self.edge_multiplier.quantize(Decimal("0.01"))),
            "intervalMultiplier": dec_str(self.interval_multiplier.quantize(Decimal("0.01"))),
            "reasons": self.reasons,
            **self.metrics,
        }


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def _bps(value: Decimal) -> str:
    return dec_str(value.quantize(Decimal("0.01")))


def _money(value: Decimal) -> str:
    return dec_str(value.quantize(Decimal("0.01")))
