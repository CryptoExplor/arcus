"""Trading strategies.

`volume-maker` (default)
------------------------
A maker-first, inventory-flat volume engine. The economics it is built around:

    net_edge_bps = captured_spread_bps - fee_bps(open) - fee_bps(close)

so the quoter only ever posts a pair whose *theoretical* round trip clears the
live fee schedule plus a buffer. Mechanics per market:

 1. Post one ALO (post-only) bid and one ALO ask around the book mid, offset by
    max(target_spread/2, required_edge/2). ALO can never pay the taker fee — it
    is rejected with POST_ONLY_WOULD_CROSS instead, which is a free retry.
 2. When one side fills, the bot is left with inventory. It immediately cancels
    the stale opposite quote and posts a **reduce-only close** on the other
    side at entry ± required edge. If the close rests (maker) the round trip is
    maker/maker and the spread is pure profit over fees.
 3. If inventory ages past `max_quote_age_s` or exceeds the inventory cap, the
    close is escalated to an aggressive reduce-only IOC. That leg pays the
    taker fee — deliberately, because carrying unhedged inventory is the larger
    risk and unlike fees it is unbounded.
 4. Repeat. Each completed cycle is 2x order notional of volume.

`ping-pong`
-----------
Same skeleton but always closes with a taker IOC as soon as it is filled — more
volume per minute, lower (often negative) edge. Use when the objective is
throughput and the fee budget is explicitly funded.

`spot-rfq`
----------
Stock Tokens (spot) are quoted by an RFQ router rather than an order book, and
spot trading is **free at launch** (zero exchange fee). There is no resting
order to place, so the spot module quotes both directions and only executes a
round trip whose quoted round-trip slippage is inside the configured budget.
Since there is no CLOB, the strategy emits *intents* which the executor either
sends to the RFQ endpoint (when credentials/route are available) or records as
a dry-run intent.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Literal

from .book import MarketState
from .config import Config
from .pnl import PnLTracker
from .scaling import D, clamp_slippage_price, dec_str, snap_price, snap_size

log = logging.getLogger("arcusbot.strategy")

Action = Literal["place", "cancel", "flatten"]


@dataclass(slots=True)
class OrderIntent:
    """A single desired order action, venue-agnostic."""

    action: Action
    market: str
    side: str = "BUY"
    size: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    tif: str = "ALO"
    reduce_only: bool = False
    client_id: str = ""
    order_id: str = ""
    tag: str = ""

    @property
    def notional(self) -> Decimal:
        return self.price * self.size

    def describe(self) -> str:
        if self.action == "cancel":
            return f"cancel {self.market} {self.client_id or self.order_id}"
        return (
            f"{self.action} {self.side} {dec_str(self.size)} {self.market} @ {dec_str(self.price)} "
            f"[{self.tif}{' RO' if self.reduce_only else ''}] {self.tag}"
        )


@dataclass(slots=True)
class LiveQuote:
    client_id: str
    side: str
    price: Decimal
    size: Decimal
    reduce_only: bool
    placed_at: float
    order_id: str = ""
    acked: bool = False
    filled: Decimal = Decimal(0)

    @property
    def age_s(self) -> float:
        return time.time() - self.placed_at


@dataclass
class MarketWorker:
    """Per-market quoting state machine."""

    cfg: Config
    market: str
    state: MarketState
    pnl: PnLTracker
    quotes: dict[str, LiveQuote] = field(default_factory=dict)
    last_quote_at: float = 0.0
    last_flatten_at: float = 0.0
    cycles: int = 0
    seq: int = 0
    inventory_since: float = 0.0

    # ------------------------------------------------------------ helpers ---
    def _cid(self, prefix: str) -> str:
        self.seq += 1
        # Client ids are signed byte-for-byte; keep them short and lowercase.
        return f"{prefix}{self.seq:04d}{uuid.uuid4().hex[:6]}"

    @property
    def position(self) -> Decimal:
        return self.pnl.book(self.market).position

    @property
    def avg_entry(self) -> Decimal:
        return self.pnl.book(self.market).avg_entry

    def open_quotes(self, side: str | None = None, reduce_only: bool | None = None) -> list[LiveQuote]:
        return [
            q
            for q in self.quotes.values()
            if (side is None or q.side == side) and (reduce_only is None or q.reduce_only == reduce_only)
        ]

    def required_edge_bps(self, maker_legs: int = 2) -> Decimal:
        """Fee-derived floor, never below the operator's absolute minimum.

        At rebate tiers the fee term goes negative — resting both legs earns
        money — which correctly lowers the spread we need. But it must not
        collapse to zero: quoting both sides at mid would cross our own book,
        maximise adverse selection and hand the spread to whoever is informed.
        ``BOT_MIN_EDGE_BPS`` is the hard floor that survives any tier.
        """
        fee_edge = self.pnl.edge_required_bps(self.cfg.fee_buffer_bps, maker_legs=maker_legs)
        return max(fee_edge, self.cfg.min_edge_bps)

    def target_size(self, price: Decimal) -> Decimal:
        if price <= 0:
            return Decimal(0)
        raw = self.cfg.order_notional_usd / price
        min_size = D(self.state.meta.get("minOrderSize", "0"))
        size = snap_size(self.state.meta, max(raw, min_size))
        # Guarantee the notional floor after snapping down.
        min_notional = D(self.state.meta.get("minOrderNotional", "5"))
        step = D(self.state.meta["stepSize"])
        while size * price < min_notional:
            size += step
        return size

    # -------------------------------------------------------------- quote ---
    def edge_bps(self) -> Decimal:
        """The round-trip spread this market must earn, right now.

        Three terms, all of which must be covered or the volume is worthless:

          fees        maker+maker round trip from the live fee tier
          buffer      configured margin so a rounding tick cannot flip it
          adverse     a multiple of realised short-term volatility

        The adverse term is what stops the bot from quoting a static 6 bps into
        a market that is moving 10 bps a tick. Without it a volume bot posts
        tight quotes, gets picked off by whoever is right about direction, and
        pays fees for the privilege.
        """
        floor = self.required_edge_bps(maker_legs=2)
        base = max(self.cfg.spread_bps, floor)
        if self.state.vol_ready:
            base = max(base, floor + self.state.vol_bps * self.cfg.vol_edge_multiplier)
        return base

    def desired_quotes(self) -> list[tuple[str, Decimal, Decimal]]:
        """[(side, price, size)] for the passive ladder around mid."""
        book = self.state.book
        mid = self.state.reference_price
        if mid is None or mid <= 0:
            return []

        edge_bps = self.edge_bps()
        half = edge_bps / Decimal(2)
        out: list[tuple[str, Decimal, Decimal]] = []

        # Inventory skew: shift both quotes against the position so the side
        # that reduces exposure is more likely to fill. A flat book quotes
        # symmetrically; a long book quotes lower on both sides, making the ask
        # attractive and the bid unattractive.
        skew_bps = Decimal(0)
        position = self.position
        if position != 0 and self.cfg.max_inventory_notional_usd > 0:
            utilisation = (abs(position) * mid) / self.cfg.max_inventory_notional_usd
            utilisation = min(utilisation, Decimal(1))
            skew_bps = utilisation * self.cfg.inventory_skew_bps
            if position > 0:
                skew_bps = -skew_bps  # long -> push quotes down

        for level in range(max(1, self.cfg.quote_levels)):
            offset_bps = half + (self.cfg.level_step_bps * level)
            centre = mid * (Decimal(1) + skew_bps / Decimal(10_000))
            bid = centre * (Decimal(1) - offset_bps / Decimal(10_000))
            ask = centre * (Decimal(1) + offset_bps / Decimal(10_000))

            # Join / improve the book only when the resulting pair still clears
            # the fee-derived edge. Improving into a tight book is how a maker
            # ends up paying to trade.
            if self.cfg.join_bbo and level == 0 and book.best_bid and book.best_ask:
                spread_bps = book.spread_bps or Decimal(0)
                if spread_bps >= edge_bps:
                    tick = D(self.state.meta["tickSize"])
                    improved_bid = max(bid, book.best_bid + tick)
                    improved_ask = min(ask, book.best_ask - tick)
                    if improved_ask > improved_bid:
                        realised_bps = (improved_ask - improved_bid) / mid * Decimal(10_000)
                        if realised_bps >= edge_bps:
                            bid, ask = improved_bid, improved_ask
                        else:  # improving would eat the edge — sit at the touch
                            bid, ask = book.best_bid, book.best_ask

            bid = snap_price(self.state.meta, bid, side="BUY")
            ask = snap_price(self.state.meta, ask, side="SELL")

            # Final guard: after snapping, the pair must STILL clear the edge.
            # Rounding on a coarse tick can silently erase a 1-2 bps margin.
            if (ask - bid) / mid * Decimal(10_000) < edge_bps:
                tick = D(self.state.meta["tickSize"])
                need = (mid * edge_bps / Decimal(10_000) - (ask - bid)) / 2
                steps = max(Decimal(1), (need / tick).quantize(Decimal(1)) + 1)
                bid = snap_price(self.state.meta, bid - tick * steps, side="BUY")
                ask = snap_price(self.state.meta, ask + tick * steps, side="SELL")

            # Never cross: an ALO that would take is rejected (free but noisy).
            if book.best_ask and bid >= book.best_ask:
                bid = snap_price(self.state.meta, book.best_ask - D(self.state.meta["tickSize"]), side="BUY")
            if book.best_bid and ask <= book.best_bid:
                ask = snap_price(self.state.meta, book.best_bid + D(self.state.meta["tickSize"]), side="SELL")

            size = self.target_size(mid)
            if size <= 0:
                continue
            out.append(("BUY", bid, size))
            out.append(("SELL", ask, size))
        return out

    def _drifted(self, quote: LiveQuote, target_price: Decimal) -> bool:
        if quote.price <= 0:
            return True
        drift_bps = abs(quote.price - target_price) / quote.price * Decimal(10_000)
        return drift_bps >= self.cfg.requote_bps or quote.age_s >= self.cfg.max_quote_age_s

    # ------------------------------------------------------------- flatten --
    def close_price(self, side: str, urgent: bool = False) -> Decimal | None:
        """The passive price at which closing the inventory still clears fees.

        A close is only "good" if it is at least `required_edge` away from the
        average entry in the profitable direction. Anything tighter turns the
        round trip into a fee donation, so it is refused unless urgent.
        """
        mid = self.state.reference_price
        if mid is None:
            return None
        edge = self.required_edge_bps(maker_legs=2) / Decimal(10_000)
        entry = self.avg_entry or mid
        floor = entry * (Decimal(1) + edge) if side == "SELL" else entry * (Decimal(1) - edge)

        book = self.state.book
        # Prefer resting at the touch when that is already profitable.
        target = floor
        tick = D(self.state.meta["tickSize"])
        if side == "SELL" and book.best_ask:
            target = min(max(floor, book.best_bid + tick if book.best_bid else floor), book.best_ask)
            target = max(target, floor)
        elif side == "BUY" and book.best_bid:
            target = max(min(floor, book.best_ask - tick if book.best_ask else floor), book.best_bid)
            target = min(target, floor)
        if urgent:
            target = floor
        return snap_price(self.state.meta, target, side=side)

    def flatten_intents(self, urgent: bool = False) -> list[OrderIntent]:
        """Work the inventory back to flat.

        Order of preference:
          1. A resting passive quote on the closing side that is already at a
             profitable price — leave it alone. Both legs resting is the
             maker/maker round trip the whole strategy is built to capture.
          2. Otherwise post a reduce-only ALO at entry +/- required edge.
          3. Escalate to a reduce-only IOC (paying the taker fee) only when the
             inventory is old, oversized, or the engine is shutting down —
             carrying unhedged inventory is the bigger risk.
        """
        position = self.position
        if position == 0:
            return []
        side = "SELL" if position > 0 else "BUY"
        size = snap_size(self.state.meta, abs(position))
        if size <= 0:
            return []

        mid = self.state.reference_price
        if mid is None:
            return []

        intents: list[OrderIntent] = []
        # Any resting quote that would ADD to the position must go.
        for quote in self.open_quotes(reduce_only=False):
            if (quote.side == "BUY") == (position > 0):
                intents.append(
                    OrderIntent(action="cancel", market=self.market,
                                client_id=quote.client_id, order_id=quote.order_id, tag="flatten-cancel")
                )

        age = time.time() - self.inventory_since if self.inventory_since else 0.0
        inventory_usd = abs(position) * mid
        # Escalating to a taker close is expensive: it pays the taker fee AND
        # crosses the spread, which together usually exceed the edge the maker
        # leg just earned. So it is reserved for the cases where *not* closing
        # is worse — oversized or very old inventory, or shutdown. Ordinary
        # quote staleness is NOT a reason to cross the spread.
        force_taker = (
            urgent
            or self.cfg.strategy == "ping-pong"
            or age > self.cfg.inventory_max_age_s
            or inventory_usd > self.cfg.max_inventory_notional_usd
        )

        if force_taker:
            for quote in self.open_quotes():
                intents.append(
                    OrderIntent(action="cancel", market=self.market,
                                client_id=quote.client_id, order_id=quote.order_id, tag="pre-taker-cancel")
                )
            price = clamp_slippage_price(self.state.meta, side, mid, self.cfg.taker_slippage_bps)
            intents.append(
                OrderIntent(
                    action="place", market=self.market, side=side, size=size, price=price,
                    tif=self.cfg.flatten_tif, reduce_only=True,
                    client_id=self._cid("x"), tag="flatten-taker",
                )
            )
            return intents

        target = self.close_price(side)
        if target is None:
            return intents

        # (1) A passive quote already working the close at a good price.
        for quote in self.open_quotes(reduce_only=False):
            if quote.side == side and quote.size >= size:
                profitable = quote.price >= target if side == "SELL" else quote.price <= target
                if profitable:
                    return intents

        # (2) Post / reprice the dedicated reduce-only close.
        for quote in [q for q in self.open_quotes(reduce_only=True) if q.side == side]:
            if quote.size == size and not self._drifted(quote, target):
                return intents
            intents.append(
                OrderIntent(action="cancel", market=self.market,
                            client_id=quote.client_id, order_id=quote.order_id, tag="reprice-close")
            )
        intents.append(
            OrderIntent(
                action="place", market=self.market, side=side, size=size, price=target,
                tif="ALO", reduce_only=True, client_id=self._cid("c"), tag="flatten-maker",
            )
        )
        return intents

    # ---------------------------------------------------------------- tick --
    def tick(self, can_open: bool) -> list[OrderIntent]:
        """Compute the intents for this loop iteration."""
        intents: list[OrderIntent] = []

        # 1. Expire stale quotes regardless of state.
        for quote in list(self.quotes.values()):
            if quote.age_s > self.cfg.max_quote_age_s * 2:
                intents.append(
                    OrderIntent(action="cancel", market=self.market,
                                client_id=quote.client_id, order_id=quote.order_id, tag="expired")
                )

        # 2. Inventory handling.
        position = self.position
        if position != 0:
            if not self.inventory_since:
                self.inventory_since = time.time()
        else:
            self.inventory_since = 0.0

        mid = self.state.reference_price
        inventory_usd = abs(position) * mid if (position and mid) else Decimal(0)
        over_cap = inventory_usd >= self.cfg.max_inventory_notional_usd
        stale_inventory = (
            self.inventory_since and (time.time() - self.inventory_since) > self.cfg.inventory_max_age_s
        )

        # Hard cases go straight to the dedicated flatten path: stop quoting,
        # just get flat.
        if position != 0 and (over_cap or stale_inventory or not can_open
                              or self.cfg.strategy == "ping-pong"):
            return intents + self.flatten_intents()

        if not can_open:
            for quote in self.open_quotes(reduce_only=False):
                intents.append(
                    OrderIntent(action="cancel", market=self.market,
                                client_id=quote.client_id, order_id=quote.order_id, tag="stand-down")
                )
            return intents

        if time.time() - self.last_quote_at < self.cfg.requote_interval_s:
            return intents

        book = self.state.book
        if book.is_crossed() or self.state.price_age_s() > self.cfg.stale_price_s:
            return intents

        desired = self.desired_quotes()
        if not desired:
            return intents
        self.last_quote_at = time.time()

        by_side: dict[str, list[LiveQuote]] = {"BUY": [], "SELL": []}
        for quote in self.open_quotes():
            by_side[quote.side].append(quote)

        # Which side would REDUCE the current position? That leg must at least
        # cover the inventory, and it is priced to clear the entry (not the
        # mid), so every round trip books positive gross edge.
        reducing_side = "" if position == 0 else ("SELL" if position > 0 else "BUY")
        close_target = self.close_price(reducing_side) if reducing_side else None

        for side, price, size in desired:
            is_reducing = side == reducing_side
            if is_reducing:
                # Never quote a close inside the profitable price.
                if close_target is not None:
                    price = max(price, close_target) if side == "SELL" else min(price, close_target)
                    price = snap_price(self.state.meta, price, side=side)
                size = max(size, snap_size(self.state.meta, abs(position)))
            elif position != 0:
                # Adding side: cap the clip so a fill cannot breach the cap.
                headroom = self.cfg.max_inventory_notional_usd - inventory_usd
                if headroom <= 0:
                    continue
                mid_px = mid or price
                size = min(size, snap_size(self.state.meta, headroom / mid_px))
                if size <= 0 or size * price < D(self.state.meta.get("minOrderNotional", "5")):
                    continue

            resting = by_side[side]
            if resting:
                quote = resting[0]
                if not self._drifted(quote, price) and quote.size >= size:
                    continue
                intents.append(
                    OrderIntent(action="cancel", market=self.market,
                                client_id=quote.client_id, order_id=quote.order_id, tag="reprice")
                )
                by_side[side] = resting[1:]
            intents.append(
                OrderIntent(
                    action="place", market=self.market, side=side, size=size, price=price,
                    tif="ALO", reduce_only=is_reducing,
                    client_id=self._cid("c" if is_reducing else ("b" if side == "BUY" else "a")),
                    tag="close-quote" if is_reducing else "quote",
                )
            )
        return intents

    # ------------------------------------------------------- lifecycle IO ---
    def on_ack(self, client_id: str, order_id: str) -> None:
        quote = self.quotes.get(client_id)
        if quote:
            quote.order_id = order_id
            quote.acked = True

    def on_terminal(self, client_id: str) -> None:
        quote = self.quotes.pop(client_id, None)
        if quote and quote.reduce_only:
            self.cycles += 1

    def register(self, intent: OrderIntent) -> None:
        self.quotes[intent.client_id] = LiveQuote(
            client_id=intent.client_id,
            side=intent.side,
            price=intent.price,
            size=intent.size,
            reduce_only=intent.reduce_only,
            placed_at=time.time(),
        )

    def snapshot(self) -> dict[str, Any]:
        book = self.state.book
        return {
            "market": self.market,
            "mid": dec_str(self.state.reference_price) if self.state.reference_price else None,
            "bestBid": dec_str(book.best_bid) if book.best_bid else None,
            "bestAsk": dec_str(book.best_ask) if book.best_ask else None,
            "spreadBps": dec_str(book.spread_bps) if book.spread_bps is not None else None,
            "requiredEdgeBps": dec_str(self.edge_bps()),
            "feeFloorBps": dec_str(self.required_edge_bps(maker_legs=2)),
            "volBps": dec_str(self.state.vol_bps),
            "position": dec_str(self.position),
            "avgEntry": dec_str(self.avg_entry),
            "liveQuotes": len(self.quotes),
            "cycles": self.cycles,
            "priceAgeS": round(self.state.price_age_s(), 2),
        }


# --------------------------------------------------------------------------- #
# Spot (Stock Tokens / RFQ)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SpotIntent:
    """A spot RFQ round trip: buy then sell the same notional."""

    token: str
    side: str
    notional_usd: Decimal
    max_slippage_bps: Decimal
    client_ref: str
    tag: str = ""

    def describe(self) -> str:
        return (
            f"spot {self.side} ${dec_str(self.notional_usd)} {self.token} "
            f"(max slip {dec_str(self.max_slippage_bps)}bps) {self.tag}"
        )


class SpotVolumeStrategy:
    """Zero-fee spot volume via RFQ round trips.

    Spot on Arcus is a router (AMM + RFQ), not an order book, and it is free at
    launch — so the only cost of a round trip is the quoted spread/slippage.
    The strategy therefore:
      * requests an indicative quote in both directions,
      * computes the implied round-trip cost in bps,
      * executes only when that cost is within `max_slippage_bps`,
      * signs a `minBuyAmount` floor client-side on every leg.
    """

    def __init__(self, cfg: Config, pnl: PnLTracker) -> None:
        self.cfg = cfg
        self.pnl = pnl
        self.seq = 0
        self.last_trade_at = 0.0
        self.round_trips = 0

    def _ref(self) -> str:
        self.seq += 1
        return f"spot{self.seq:04d}{uuid.uuid4().hex[:6]}"

    def max_slippage_bps(self) -> Decimal:
        # Spot has no exchange fee; the budget is purely the quoted spread.
        return max(Decimal(2), self.cfg.fee_buffer_bps * Decimal(2))

    def tick(self, tokens: Iterable[str], can_open: bool) -> list[SpotIntent]:
        if not can_open or not self.cfg.enable_spot:
            return []
        if time.time() - self.last_trade_at < max(2.0, self.cfg.requote_interval_s * 2):
            return []
        self.last_trade_at = time.time()
        intents: list[SpotIntent] = []
        for token in tokens:
            ref = self._ref()
            intents.append(
                SpotIntent(
                    token=token,
                    side="BUY",
                    notional_usd=self.cfg.order_notional_usd,
                    max_slippage_bps=self.max_slippage_bps(),
                    client_ref=ref,
                    tag="rfq-open",
                )
            )
            intents.append(
                SpotIntent(
                    token=token,
                    side="SELL",
                    notional_usd=self.cfg.order_notional_usd,
                    max_slippage_bps=self.max_slippage_bps(),
                    client_ref=ref,
                    tag="rfq-close",
                )
            )
        return intents
