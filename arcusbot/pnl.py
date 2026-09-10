"""Fee-aware PnL, volume and inventory accounting.

The bot's success criterion is not "made money on direction" — it is
**generate volume while net PnL after fees stays >= 0**. That means fees have
to be first-class, not an afterthought:

  * Every fill is booked against a per-market average-cost inventory, so
    realized PnL is exact and independent of how fills interleave.
  * Fees are read from the live fee tier table (`GET /v1/feetiers`, ppm) and
    charged per fill; maker rebates (negative ppm) are credited.
  * `edge_required_bps` tells the strategy the minimum round-trip spread that
    still clears fees plus the configured buffer. The quoter refuses to place
    a pair tighter than that, which is what keeps volume from bleeding equity.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .scaling import D, dec_str

__all__ = ["FeeSchedule", "Fill", "MarketPnL", "PnLTracker"]

PPM = Decimal(1_000_000)


@dataclass(slots=True)
class FeeSchedule:
    """Maker/taker fees in parts-per-million, as published by the exchange."""

    maker_ppm: Decimal = Decimal(200)   # conservative placeholder: 2 bps
    taker_ppm: Decimal = Decimal(500)   # conservative placeholder: 5 bps
    level: int = 0
    name: str = "assumed-base"
    source: str = "default"

    @classmethod
    def from_fee_tiers(cls, payload: Any, volume_30d_usd: Decimal | None = None) -> "FeeSchedule":
        """Pick the applicable tier from `GET /v1/feetiers`.

        The table is sorted ascending by level; a tier applies once trailing
        30d volume reaches its `volumeThreshold`. Falls back to the base tier.
        """
        tiers: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for key in ("tiers", "feeTiers", "feetiers", "data"):
                if isinstance(payload.get(key), list):
                    tiers = payload[key]
                    break
        elif isinstance(payload, list):
            tiers = payload
        if not tiers:
            return cls()

        chosen = tiers[0]
        if volume_30d_usd is not None:
            for tier in tiers:
                threshold = D(tier.get("volumeThreshold", 0))
                if volume_30d_usd >= threshold:
                    chosen = tier
        return cls(
            maker_ppm=D(chosen.get("makerFeePpm", 200)),
            taker_ppm=D(chosen.get("takerFeePpm", 500)),
            level=int(chosen.get("level", 0)),
            name=str(chosen.get("name", "tier")),
            source="exchange",
        )

    @property
    def maker_bps(self) -> Decimal:
        return self.maker_ppm / Decimal(100)

    @property
    def taker_bps(self) -> Decimal:
        return self.taker_ppm / Decimal(100)

    def fee_for(self, notional: Decimal, liquidity: str) -> Decimal:
        """Signed fee in USD. Negative = rebate earned."""
        ppm = self.maker_ppm if liquidity == "MAKER" else self.taker_ppm
        return (D(notional) * ppm) / PPM

    def round_trip_bps(self, maker_legs: int = 2) -> Decimal:
        """Cost in bps of a full open+close cycle.

        maker_legs=2 -> both legs rest (maker/maker)
        maker_legs=1 -> maker open, taker close (the realistic default)
        maker_legs=0 -> taker/taker
        """
        legs = [self.maker_bps] * maker_legs + [self.taker_bps] * (2 - maker_legs)
        return sum(legs, Decimal(0))

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "name": self.name,
            "maker_ppm": str(self.maker_ppm),
            "taker_ppm": str(self.taker_ppm),
            "maker_bps": str(self.maker_bps),
            "taker_bps": str(self.taker_bps),
            "source": self.source,
        }


@dataclass(slots=True)
class Fill:
    trade_id: str
    market: str
    side: str            # BUY | SELL
    price: Decimal
    size: Decimal
    liquidity: str       # MAKER | TAKER
    fee: Decimal
    ts: float
    order_id: str = ""
    client_id: str = ""

    @property
    def notional(self) -> Decimal:
        return self.price * self.size

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.ts)),
            "tradeId": self.trade_id,
            "market": self.market,
            "side": self.side,
            "price": dec_str(self.price),
            "size": dec_str(self.size),
            "notional": dec_str(self.notional),
            "liquidity": self.liquidity,
            "fee": dec_str(self.fee),
            "orderId": self.order_id,
            "clientId": self.client_id,
        }


@dataclass(slots=True)
class MarketPnL:
    """Average-cost inventory book for one market."""

    market: str
    position: Decimal = Decimal(0)        # + long, - short (base units)
    avg_entry: Decimal = Decimal(0)
    realized: Decimal = Decimal(0)        # gross realized, before fees
    fees: Decimal = Decimal(0)
    rebates: Decimal = Decimal(0)
    volume: Decimal = Decimal(0)          # USD notional traded
    maker_volume: Decimal = Decimal(0)
    taker_volume: Decimal = Decimal(0)
    fill_count: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    funding: Decimal = Decimal(0)
    last_price: Decimal = Decimal(0)

    def apply(self, fill: Fill) -> Decimal:
        """Book a fill; returns the gross realized PnL it produced."""
        signed = fill.size if fill.side == "BUY" else -fill.size
        realized = Decimal(0)

        if self.position == 0 or (self.position > 0) == (signed > 0):
            # opening or adding — weighted average entry
            new_pos = self.position + signed
            if new_pos != 0:
                self.avg_entry = (
                    (self.avg_entry * abs(self.position)) + (fill.price * abs(signed))
                ) / abs(new_pos)
            self.position = new_pos
        else:
            closing = min(abs(signed), abs(self.position))
            direction = Decimal(1) if self.position > 0 else Decimal(-1)
            realized = (fill.price - self.avg_entry) * closing * direction
            self.realized += realized
            remaining = abs(signed) - closing
            self.position += signed
            if remaining > 0:
                # flipped through zero: the remainder opens the other way
                self.avg_entry = fill.price
            elif self.position == 0:
                self.avg_entry = Decimal(0)

        self.fees += max(fill.fee, Decimal(0))
        if fill.fee < 0:
            self.rebates += -fill.fee
        self.volume += fill.notional
        self.fill_count += 1
        if fill.liquidity == "MAKER":
            self.maker_volume += fill.notional
            self.maker_fills += 1
        else:
            self.taker_volume += fill.notional
            self.taker_fills += 1
        self.last_price = fill.price
        return realized

    def unrealized(self, mark: Decimal | None) -> Decimal:
        if not self.position or mark is None:
            return Decimal(0)
        return (D(mark) - self.avg_entry) * self.position

    def net(self, mark: Decimal | None = None) -> Decimal:
        return self.realized + self.rebates - self.fees + self.funding + self.unrealized(mark)

    def as_dict(self, mark: Decimal | None = None) -> dict[str, Any]:
        return {
            "market": self.market,
            "position": dec_str(self.position),
            "avgEntry": dec_str(self.avg_entry),
            "realized": dec_str(self.realized),
            "unrealized": dec_str(self.unrealized(mark)),
            "fees": dec_str(self.fees),
            "rebates": dec_str(self.rebates),
            "funding": dec_str(self.funding),
            "net": dec_str(self.net(mark)),
            "volume": dec_str(self.volume),
            "makerVolume": dec_str(self.maker_volume),
            "takerVolume": dec_str(self.taker_volume),
            "fills": self.fill_count,
            "makerFills": self.maker_fills,
            "takerFills": self.taker_fills,
        }


class PnLTracker:
    """Aggregates per-market books, fee schedule and the fill journal."""

    def __init__(self, fees: FeeSchedule | None = None, journal_path: Path | None = None) -> None:
        self.fees = fees or FeeSchedule()
        self.books: dict[str, MarketPnL] = {}
        self.started_at = time.time()
        self.seen_trade_ids: set[str] = set()
        self.journal_path = journal_path
        self.equity_start: Decimal | None = None
        self.equity_now: Decimal | None = None
        self.peak_net: Decimal = Decimal(0)
        self.session_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        if journal_path:
            journal_path.parent.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- input ---
    def book(self, market: str) -> MarketPnL:
        if market not in self.books:
            self.books[market] = MarketPnL(market=market)
        return self.books[market]

    def record_fill(self, fill: Fill) -> bool:
        """Idempotent by tradeId. Returns True if the fill was new."""
        if fill.trade_id and fill.trade_id in self.seen_trade_ids:
            return False
        if fill.trade_id:
            self.seen_trade_ids.add(fill.trade_id)
        self.book(fill.market).apply(fill)
        self.peak_net = max(self.peak_net, self.net_pnl())
        if self.journal_path:
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(fill.as_dict()) + "\n")
        return True

    def record_funding(self, market: str, amount: Any) -> None:
        self.book(market).funding += D(amount)

    def set_equity(self, equity: Any) -> None:
        value = D(equity)
        if self.equity_start is None:
            self.equity_start = value
        self.equity_now = value

    def set_marks(self, marks: dict[str, Decimal | None]) -> None:
        self._marks = dict(marks)

    # ------------------------------------------------------------ outputs ---
    @property
    def marks(self) -> dict[str, Decimal | None]:
        return getattr(self, "_marks", {})

    def total_volume(self) -> Decimal:
        return sum((b.volume for b in self.books.values()), Decimal(0))

    def maker_volume(self) -> Decimal:
        return sum((b.maker_volume for b in self.books.values()), Decimal(0))

    def total_fees(self) -> Decimal:
        return sum((b.fees for b in self.books.values()), Decimal(0))

    def total_rebates(self) -> Decimal:
        return sum((b.rebates for b in self.books.values()), Decimal(0))

    def realized(self) -> Decimal:
        return sum((b.realized for b in self.books.values()), Decimal(0))

    def unrealized(self) -> Decimal:
        return sum((b.unrealized(self.marks.get(name)) for name, b in self.books.items()), Decimal(0))

    def funding(self) -> Decimal:
        return sum((b.funding for b in self.books.values()), Decimal(0))

    def gross_pnl(self) -> Decimal:
        return self.realized() + self.unrealized() + self.funding()

    def net_pnl(self) -> Decimal:
        return self.gross_pnl() + self.total_rebates() - self.total_fees()

    def fee_coverage(self) -> Decimal:
        """Gross edge captured per $1 of fees paid. >1 means fees are covered."""
        fees = self.total_fees()
        if fees == 0:
            return Decimal(0) if self.gross_pnl() == 0 else Decimal(999)
        return (self.gross_pnl() + self.total_rebates()) / fees

    def drawdown(self) -> Decimal:
        return max(Decimal(0), self.peak_net - self.net_pnl())

    def bps_per_volume(self) -> Decimal:
        vol = self.total_volume()
        if vol == 0:
            return Decimal(0)
        return self.net_pnl() / vol * Decimal(10_000)

    def edge_required_bps(self, buffer_bps: Decimal = Decimal(1), maker_legs: int = 1) -> Decimal:
        """Minimum round-trip spread (bps) the quoter must capture."""
        return self.fees.round_trip_bps(maker_legs=maker_legs) + D(buffer_bps)

    def open_positions(self) -> dict[str, Decimal]:
        return {name: b.position for name, b in self.books.items() if b.position != 0}

    def snapshot(self) -> dict[str, Any]:
        runtime = max(1e-9, time.time() - self.started_at)
        volume = self.total_volume()
        return {
            "sessionId": self.session_id,
            "runtimeSeconds": round(runtime, 1),
            "fees": self.fees.as_dict(),
            "volumeUsd": dec_str(volume),
            "makerVolumeUsd": dec_str(self.maker_volume()),
            "makerShare": dec_str((self.maker_volume() / volume * 100) if volume else Decimal(0)),
            "volumePerHourUsd": dec_str(volume / D(runtime) * Decimal(3600)),
            "fillCount": sum(b.fill_count for b in self.books.values()),
            "realizedPnl": dec_str(self.realized()),
            "unrealizedPnl": dec_str(self.unrealized()),
            "fundingPnl": dec_str(self.funding()),
            "feesPaid": dec_str(self.total_fees()),
            "rebatesEarned": dec_str(self.total_rebates()),
            "grossPnl": dec_str(self.gross_pnl()),
            "netPnl": dec_str(self.net_pnl()),
            "netBpsOfVolume": dec_str(self.bps_per_volume()),
            "feeCoverageRatio": dec_str(self.fee_coverage()),
            "drawdown": dec_str(self.drawdown()),
            "equityStart": dec_str(self.equity_start) if self.equity_start is not None else None,
            "equityNow": dec_str(self.equity_now) if self.equity_now is not None else None,
            "equityDelta": dec_str(self.equity_now - self.equity_start)
            if self.equity_now is not None and self.equity_start is not None
            else None,
            "openPositions": {k: dec_str(v) for k, v in self.open_positions().items()},
            "markets": [b.as_dict(self.marks.get(name)) for name, b in sorted(self.books.items())],
        }

    def write_report(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")
        return path

    def text_report(self) -> str:
        s = self.snapshot()
        lines = [
            f"session {s['sessionId']}  runtime {s['runtimeSeconds']}s",
            f"volume ${s['volumeUsd']} ({s['makerShare']}% maker, ${s['volumePerHourUsd']}/h) "
            f"over {s['fillCount']} fills",
            f"fees ${s['feesPaid']} paid / ${s['rebatesEarned']} rebates "
            f"(tier {s['fees']['level']} {s['fees']['maker_bps']}bps maker / "
            f"{s['fees']['taker_bps']}bps taker, {s['fees']['source']})",
            f"pnl gross ${s['grossPnl']} -> NET ${s['netPnl']} "
            f"({s['netBpsOfVolume']} bps of volume, fee coverage {s['feeCoverageRatio']}x)",
        ]
        if s["equityDelta"] is not None:
            lines.append(f"equity {s['equityStart']} -> {s['equityNow']} (delta {s['equityDelta']})")
        if s["openPositions"]:
            lines.append("open: " + ", ".join(f"{k} {v}" for k, v in s["openPositions"].items()))
        return "\n".join(lines)


def fills_from_ws(payload: dict[str, Any], fees: FeeSchedule) -> Iterable[Fill]:
    """Normalize a `userFills` frame into Fill objects.

    Field names vary slightly between the snapshot rows and streaming rows, and
    the venue may or may not stamp an explicit fee / liquidity flag, so both are
    derived defensively and fall back to the fee schedule.
    """
    rows: list[dict[str, Any]]
    if isinstance(payload.get("fills"), list):
        rows = payload["fills"]
    elif isinstance(payload.get("data"), list):
        rows = payload["data"]
    elif "fillPrice" in payload or "price" in payload:
        rows = [payload]
    else:
        rows = []

    for row in rows:
        price = row.get("fillPrice", row.get("price"))
        size = row.get("fillSize", row.get("size"))
        if price in (None, "") or size in (None, ""):
            continue
        liquidity = str(
            row.get("liquidity")
            or row.get("liquiditySide")
            or ("MAKER" if row.get("isMaker") else "TAKER" if row.get("isMaker") is not None else "")
            or ("TAKER" if row.get("takerOrderId") == row.get("orderId") else "MAKER")
        ).upper()
        if liquidity not in {"MAKER", "TAKER"}:
            liquidity = "TAKER"
        notional = D(price) * D(size)
        fee_raw = row.get("fee", row.get("feeUsd"))
        fee = D(fee_raw) if fee_raw not in (None, "") else fees.fee_for(notional, liquidity)
        ts_raw = row.get("timestamp", row.get("createdAt", 0)) or 0
        ts = float(ts_raw) / 1_000_000 if float(ts_raw) > 1e12 else (float(ts_raw) or time.time())
        yield Fill(
            trade_id=str(row.get("tradeId") or row.get("id") or f"{row.get('orderId','')}-{ts_raw}"),
            market=str(row.get("market") or row.get("marketDisplayName") or ""),
            side=str(row.get("side", "BUY")).upper(),
            price=D(price),
            size=D(size),
            liquidity=liquidity,
            fee=fee,
            ts=ts,
            order_id=str(row.get("orderId", "")),
            client_id=str(row.get("clientId", "")),
        )
