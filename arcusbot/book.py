"""Local market state: BBO / L2 book, mark prices and staleness tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .scaling import D

__all__ = ["Level", "BookState", "MarketState"]


@dataclass(slots=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(slots=True)
class BookState:
    """Top-of-book plus the last L2 snapshot for one market."""

    market: str
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    last_sequence_id: int = 0
    global_sequence_id: int = 0
    updated_at: float = 0.0

    # ------------------------------------------------------------- ingest ---
    def apply_snapshot(self, contents: dict[str, Any]) -> None:
        self.bids = [Level(D(p), D(s)) for p, s in (contents.get("bids") or [])]
        self.asks = [Level(D(p), D(s)) for p, s in (contents.get("asks") or [])]
        self.last_sequence_id = int(contents.get("lastSequenceId") or 0)
        self.global_sequence_id = int(contents.get("globalSequenceId") or 0)
        self.updated_at = time.time()

    def apply_bbo(self, contents: dict[str, Any]) -> None:
        best_bid = contents.get("bestBid")
        best_ask = contents.get("bestAsk")
        self.bids = [Level(D(best_bid["price"]), D(best_bid["size"]))] if best_bid else []
        self.asks = [Level(D(best_ask["price"]), D(best_ask["size"]))] if best_ask else []
        self.last_sequence_id = int(contents.get("lastSequenceId") or self.last_sequence_id)
        self.global_sequence_id = int(contents.get("globalSequenceId") or self.global_sequence_id)
        self.updated_at = time.time()

    # ------------------------------------------------------------- reads ----
    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    def top_depth_usd(self, levels: int = 5) -> Decimal | None:
        """Notional resting within the top N levels of both sides.

        Used to judge whether our clip is a small part of the book or most of
        it — quoting a clip comparable to the whole top of book means our own
        order is the liquidity, and fills are far more likely to be adverse.
        """
        if not self.bids and not self.asks:
            return None
        total = Decimal(0)
        for side in (self.bids[:levels], self.asks[:levels]):
            for level in side:
                total += level.price * level.size
        return total or None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid
        if mid is None or mid == 0 or self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_ask - self.best_bid) / mid * Decimal(10_000)

    def age_s(self) -> float:
        return time.time() - self.updated_at if self.updated_at else float("inf")

    def is_crossed(self) -> bool:
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid >= self.best_ask
        )


@dataclass(slots=True)
class MarketState:
    """Everything the strategy needs to know about one market, live."""

    market: str
    market_id: int
    meta: dict[str, Any]
    book: BookState
    mark_price: Decimal | None = None
    oracle_price: Decimal | None = None
    mark_epoch_ns: int = 0
    mark_updated_at: float = 0.0
    funding_rate: Decimal = Decimal(0)
    is_outside_rth: bool = False
    upper_trading_bound: Decimal | None = None
    lower_trading_bound: Decimal | None = None
    status: str = "ONLINE"
    # EWMA of |mid return| in bps — the adverse-selection cost proxy.
    vol_bps: Decimal = Decimal(0)
    _last_mid: Decimal | None = None
    vol_samples: int = 0

    def observe_vol(self) -> None:
        """Update the volatility estimate from the current reference price.

        Adverse selection is the dominant cost for a maker: the wider the mid
        moves between placing a quote and it being filled, the more the fill is
        worth *less* than the quoted price. Quoting a fixed spread while
        volatility rises is the classic way a market maker bleeds, so the
        strategy sizes its spread off this number.
        """
        mid = self.reference_price
        if mid is None or mid <= 0:
            return
        if self._last_mid is not None and self._last_mid > 0:
            ret_bps = abs(mid - self._last_mid) / self._last_mid * Decimal(10_000)
            alpha = Decimal("0.15")
            self.vol_bps = (alpha * ret_bps) + ((Decimal(1) - alpha) * self.vol_bps)
            self.vol_samples += 1
        self._last_mid = mid

    @property
    def vol_ready(self) -> bool:
        return self.vol_samples >= 8

    def apply_oracle(self, entry: dict[str, Any]) -> None:
        price = entry.get("price")
        mark = entry.get("markPrice")
        if price not in (None, "", "0"):
            self.oracle_price = D(price)
        # markPrice "0" means unavailable — never fall back to oraclePrice.
        if mark not in (None, "", "0"):
            self.mark_price = D(mark)
            self.mark_epoch_ns = int(entry.get("markEpochNanos") or 0)
            self.mark_updated_at = time.time()

    def apply_market_row(self, row: dict[str, Any]) -> None:
        self.status = str(row.get("status", self.status))
        if row.get("markPrice") not in (None, "", "0"):
            self.mark_price = D(row["markPrice"])
            self.mark_updated_at = time.time()
        if row.get("oraclePrice") not in (None, "", "0"):
            self.oracle_price = D(row["oraclePrice"])
        if row.get("fundingRate") not in (None, ""):
            self.funding_rate = D(row["fundingRate"])
        if row.get("isOutsideRth") is not None:
            self.is_outside_rth = bool(row["isOutsideRth"])
        for key, attr in (
            ("upperTradingBound", "upper_trading_bound"),
            ("lowerTradingBound", "lower_trading_bound"),
        ):
            value = row.get(key)
            setattr(self, attr, D(value) if value not in (None, "") else None)

    @property
    def reference_price(self) -> Decimal | None:
        """Book mid when available, else the engine mark price."""
        return self.book.mid or self.mark_price

    def price_age_s(self) -> float:
        candidates = [self.book.age_s()]
        if self.mark_updated_at:
            candidates.append(time.time() - self.mark_updated_at)
        return min(candidates)

    def within_bounds(self, price: Decimal) -> bool:
        """Off-hours trading band check (RWA markets outside RTH)."""
        if self.upper_trading_bound is not None and price >= self.upper_trading_bound:
            return False
        if self.lower_trading_bound is not None and price <= self.lower_trading_bound:
            return False
        return True
