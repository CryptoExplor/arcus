"""Risk guards and the kill switch.

The bot is allowed to be aggressive about *volume*; it is never allowed to be
aggressive about *risk*. Every quote passes `RiskManager.check_order`, and the
engine calls `evaluate` once per loop to decide whether to keep trading, stop
opening (flatten only), or halt entirely.

Guard classes
-------------
HALT       stop trading, cancel everything, flatten if possible, exit
FLATTEN    stop opening new exposure; only reduce-only orders allowed
THROTTLE   pause new orders for a cool-down window
OK         normal operation
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .book import MarketState
from .config import Config
from .pnl import PnLTracker
from .scaling import D, dec_str

log = logging.getLogger("arcusbot.risk")

OK, THROTTLE, FLATTEN, HALT = "OK", "THROTTLE", "FLATTEN", "HALT"


@dataclass(slots=True)
class RiskVerdict:
    state: str = OK
    reasons: list[str] = field(default_factory=list)
    retry_after_s: float = 0.0

    @property
    def can_open(self) -> bool:
        return self.state == OK

    @property
    def can_trade(self) -> bool:
        return self.state in {OK, FLATTEN, THROTTLE}

    @property
    def halted(self) -> bool:
        return self.state == HALT


class RiskManager:
    def __init__(self, cfg: Config, pnl: PnLTracker, session: Any = None) -> None:
        self.cfg = cfg
        self.pnl = pnl
        self.session = session
        self.order_times: deque[float] = deque(maxlen=2_000)
        self.consecutive_errors = 0
        self.total_errors = 0
        self.rejections: dict[str, int] = {}
        self.halt_reason: str | None = None
        self.throttle_until = 0.0
        self.free_collateral: Decimal | None = None
        self.equity: Decimal | None = None
        self.started_at = time.time()
        self.day_start = time.time()
        self.day_start_net = Decimal(0)
        self.last_verdict = RiskVerdict()
        # Loss already booked today, before this process started. Without it a
        # restart resets the daily limit and the bot can lose it again.
        self.carried_daily_loss = Decimal(0)
        self.carried_drawdown = Decimal(0)
        if session is not None:
            self.carried_daily_loss = session.carried_daily_loss()
            if cfg.carry_drawdown:
                self.carried_drawdown = max(Decimal(0), session.risk.peak_net_pnl_usd)
            if cfg.max_restart_crashes and \
                    session.risk.consecutive_crashes >= cfg.max_restart_crashes:
                self.halt(
                    f"{session.risk.consecutive_crashes} consecutive unclean exits "
                    f">= RISK_MAX_RESTART_CRASHES ({cfg.max_restart_crashes}); "
                    f"last: {session.risk.last_exit_reason or 'unknown'}. "
                    "Investigate before restarting, or clear state/session.json."
                )

    # ------------------------------------------------- cross-restart totals --
    def effective_daily_loss(self) -> Decimal:
        """Today's loss including anything booked before this restart."""
        net = self.pnl.net_pnl()
        session_loss = -net if net < 0 else Decimal(0)
        return session_loss + self.carried_daily_loss

    def effective_drawdown(self) -> Decimal:
        """Drawdown measured from the all-time peak, not this session's peak."""
        live = self.pnl.drawdown()
        if not self.carried_drawdown:
            return live
        # Peak carried over from previous runs: measure the fall from there too.
        from_carried = max(Decimal(0), self.carried_drawdown - self.pnl.net_pnl())
        return max(live, from_carried)

    # ---------------------------------------------------------- accounting --
    def note_order(self) -> None:
        self.order_times.append(time.time())

    def note_error(self, exc: Exception | str) -> None:
        self.consecutive_errors += 1
        self.total_errors += 1
        log.debug("error #%d: %s", self.consecutive_errors, exc)

    def note_success(self) -> None:
        self.consecutive_errors = 0

    def note_rejection(self, reason: str) -> None:
        key = reason or "UNKNOWN"
        self.rejections[key] = self.rejections.get(key, 0) + 1
        # Reasons that mean "your sizing/margin is wrong", not "try again".
        if key in {"UNDERCOLLATERALIZED", "POSITION_SIZE_CAP_EXCEEDED"}:
            self.throttle(5.0, f"rejection {key}")

    def note_account(self, snapshot: dict[str, Any]) -> None:
        for key in ("freeCollateral", "free_collateral"):
            if snapshot.get(key) not in (None, ""):
                self.free_collateral = D(snapshot[key])
                break
        for key in ("accountEquity", "equity"):
            if snapshot.get(key) not in (None, ""):
                self.equity = D(snapshot[key])
                self.pnl.set_equity(self.equity)
                break

    def throttle(self, seconds: float, reason: str) -> None:
        self.throttle_until = max(self.throttle_until, time.time() + seconds)
        log.warning("throttling %.1fs: %s", seconds, reason)

    def halt(self, reason: str) -> None:
        if self.halt_reason is None:
            self.halt_reason = reason
            log.error("KILL SWITCH: %s", reason)

    def orders_last_minute(self) -> int:
        cutoff = time.time() - 60
        return sum(1 for t in self.order_times if t >= cutoff)

    # ------------------------------------------------------- per-order gate --
    def check_order(
        self,
        market_state: MarketState,
        side: str,
        size: Decimal,
        price: Decimal,
        *,
        reduce_only: bool,
        open_orders: int,
        position: Decimal,
    ) -> tuple[bool, str]:
        """Returns (allowed, reason_if_not).

        Reduce-only orders are ALWAYS permitted, including after the kill
        switch has fired: halting must never trap the bot in a position it is
        not allowed to close.
        """
        if self.halt_reason and not reduce_only:
            return False, f"halted: {self.halt_reason}"
        if time.time() < self.throttle_until and not reduce_only:
            return False, "throttled"
        if size <= 0:
            return False, "size snapped to zero"

        meta = market_state.meta
        if str(meta.get("status", "ONLINE")).upper() != "ONLINE":
            return False, f"market status {meta.get('status')}"

        notional = price * size
        min_notional = D(meta.get("minOrderNotional", "5"))
        if not reduce_only and notional < min_notional:
            return False, f"notional {dec_str(notional)} < min {dec_str(min_notional)}"
        max_size = D(meta.get("maxOrderSize", "1e18"))
        if size > max_size:
            return False, f"size {dec_str(size)} > maxOrderSize {dec_str(max_size)}"

        if not market_state.within_bounds(price):
            return False, "price outside off-hours trading band"

        if market_state.price_age_s() > self.cfg.stale_price_s:
            return False, f"stale price ({market_state.price_age_s():.1f}s)"

        if not reduce_only:
            if open_orders >= self.cfg.max_open_orders:
                return False, f"open orders {open_orders} >= cap {self.cfg.max_open_orders}"
            if self.orders_last_minute() >= self.cfg.max_orders_per_min:
                return False, "order rate cap reached"
            projected = position + (size if side == "BUY" else -size)
            ref = market_state.reference_price or price
            if abs(projected) * ref > self.cfg.max_position_notional_usd:
                return False, (
                    f"projected position ${dec_str(abs(projected) * ref)} > cap "
                    f"${dec_str(self.cfg.max_position_notional_usd)}"
                )
            if self.free_collateral is not None:
                if self.free_collateral < self.cfg.min_free_collateral_usd:
                    return False, (
                        f"free collateral ${dec_str(self.free_collateral)} < floor "
                        f"${dec_str(self.cfg.min_free_collateral_usd)}"
                    )

        return True, ""

    # ------------------------------------------------------- global verdict --
    def evaluate(self) -> RiskVerdict:
        verdict = RiskVerdict()
        now = time.time()

        if self.halt_reason:
            verdict.state = HALT
            verdict.reasons.append(self.halt_reason)
            self.last_verdict = verdict
            return verdict

        net = self.pnl.net_pnl()
        drawdown = self.effective_drawdown()
        daily_loss = self.effective_daily_loss()

        if drawdown >= self.cfg.max_drawdown_usd:
            carried = (f" (includes ${dec_str(self.carried_drawdown)} carried from "
                       f"previous runs)" if self.carried_drawdown else "")
            self.halt(f"drawdown ${dec_str(drawdown)} >= "
                      f"${dec_str(self.cfg.max_drawdown_usd)}{carried}")
        if daily_loss >= self.cfg.max_daily_loss_usd:
            carried = (f" (includes ${dec_str(self.carried_daily_loss)} already lost "
                       f"today)" if self.carried_daily_loss else "")
            self.halt(f"daily loss ${dec_str(daily_loss)} >= "
                      f"${dec_str(self.cfg.max_daily_loss_usd)}{carried}")
        if self.consecutive_errors >= self.cfg.max_consecutive_errors:
            self.halt(f"{self.consecutive_errors} consecutive errors")
        if self.cfg.max_runtime_s and (now - self.started_at) >= self.cfg.max_runtime_s:
            self.halt(f"max runtime {self.cfg.max_runtime_s}s reached")
        if self.cfg.volume_target_usd and self.pnl.total_volume() >= self.cfg.volume_target_usd:
            self.halt(f"volume target ${dec_str(self.cfg.volume_target_usd)} reached")

        if self.halt_reason:
            verdict.state = HALT
            verdict.reasons.append(self.halt_reason)
            self.last_verdict = verdict
            return verdict

        if self.free_collateral is not None and self.free_collateral < self.cfg.min_free_collateral_usd:
            verdict.state = FLATTEN
            verdict.reasons.append(
                f"free collateral ${dec_str(self.free_collateral)} below floor"
            )
        # Soft stop at 70% of the hard drawdown limit: stop opening, keep closing.
        if drawdown >= self.cfg.max_drawdown_usd * Decimal("0.7"):
            verdict.state = FLATTEN
            verdict.reasons.append(f"drawdown ${dec_str(drawdown)} near limit")

        inventory_usd = sum(
            (abs(b.position) * D(self.pnl.marks.get(name) or b.last_price or 0)
             for name, b in self.pnl.books.items()),
            Decimal(0),
        )
        if inventory_usd > self.cfg.max_inventory_notional_usd:
            verdict.state = FLATTEN
            verdict.reasons.append(f"inventory ${dec_str(inventory_usd)} over soft cap")

        if now < self.throttle_until and verdict.state == OK:
            verdict.state = THROTTLE
            verdict.retry_after_s = self.throttle_until - now
            verdict.reasons.append("cool-down")

        self.last_verdict = verdict
        return verdict

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.last_verdict.state,
            "reasons": self.last_verdict.reasons,
            "haltReason": self.halt_reason,
            "ordersLastMinute": self.orders_last_minute(),
            "consecutiveErrors": self.consecutive_errors,
            "totalErrors": self.total_errors,
            "rejections": dict(self.rejections),
            "freeCollateral": dec_str(self.free_collateral) if self.free_collateral is not None else None,
            "equity": dec_str(self.equity) if self.equity is not None else None,
            "throttledFor": round(max(0.0, self.throttle_until - time.time()), 2),
            "effectiveDrawdown": dec_str(self.effective_drawdown()),
            "effectiveDailyLoss": dec_str(self.effective_daily_loss()),
            "carriedDrawdown": dec_str(self.carried_drawdown),
            "carriedDailyLoss": dec_str(self.carried_daily_loss),
        }
