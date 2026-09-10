"""Offline paper exchange (`ARCUS_VENUE=sim`).

Purpose: exercise the *entire* bot — quoting, inventory flattening, fee math,
risk guards, reporting — with zero network access and deterministic output, so
strategy and accounting changes can be validated before a single testnet order
is signed. It is intentionally pessimistic:

  * maker fills are probabilistic and only happen when the random walk trades
    through the quote, so passive edge is never free;
  * taker fills always cross the spread and pay the taker fee;
  * fees use the same FeeSchedule the live path uses.

It is NOT a market-microstructure model. Use it to prove the machinery, then
run the real thing on testnet.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .config import Config
from .pnl import FeeSchedule, Fill
from .scaling import D, dec_str, snap_price

SIM_MARKETS: dict[str, dict[str, Any]] = {
    "BTC-USD": {
        "marketDisplayName": "BTC-USD", "marketId": 1, "status": "ONLINE",
        "baseAsset": "BTC", "quoteAsset": "USD",
        "tickSize": "0.1", "stepSize": "0.00001",
        "tickTiers": [{"tick": "0.1"}],
        "minOrderNotional": "5", "minOrderSize": "0.0001", "maxOrderSize": "50",
        "initialMarginFraction": "0.05", "maintenanceMarginFraction": "0.03",
        "type": "PERP", "category": "CRYPTO", "isOutsideRth": False,
        "regularTradingHours": None, "oraclePrice": "64000", "markPrice": "64000",
        "upperTradingBound": None, "lowerTradingBound": None,
    },
    "ETH-USD": {
        "marketDisplayName": "ETH-USD", "marketId": 2, "status": "ONLINE",
        "baseAsset": "ETH", "quoteAsset": "USD",
        "tickSize": "0.01", "stepSize": "0.001",
        "tickTiers": [{"tick": "0.01"}],
        "minOrderNotional": "5", "minOrderSize": "0.001", "maxOrderSize": "500",
        "initialMarginFraction": "0.05", "maintenanceMarginFraction": "0.03",
        "type": "PERP", "category": "CRYPTO", "isOutsideRth": False,
        "regularTradingHours": None, "oraclePrice": "3200", "markPrice": "3200",
        "upperTradingBound": None, "lowerTradingBound": None,
    },
    "SOL-USD": {
        "marketDisplayName": "SOL-USD", "marketId": 3, "status": "ONLINE",
        "baseAsset": "SOL", "quoteAsset": "USD",
        "tickSize": "0.001", "stepSize": "0.01",
        "tickTiers": [{"tick": "0.001"}],
        "minOrderNotional": "5", "minOrderSize": "0.01", "maxOrderSize": "5000",
        "initialMarginFraction": "0.1", "maintenanceMarginFraction": "0.05",
        "type": "PERP", "category": "CRYPTO", "isOutsideRth": False,
        "regularTradingHours": None, "oraclePrice": "150", "markPrice": "150",
        "upperTradingBound": None, "lowerTradingBound": None,
    },
}

# The REAL published Arcus perpetuals fee schedule, transcribed from the
# exchange's "Perpetuals Fee Tiers" table. Percentages -> ppm (0.0150% = 150ppm).
#
# Two things here matter enormously to this bot and were previously guessed
# wrong by the simulator:
#   1. Taker at the base tier is 0.0450% (450ppm), not 400ppm. The base
#      round trip is therefore MORE expensive than earlier sweeps assumed.
#   2. Maker rebates do not start until $1B of 30d volume. Any strategy that
#      quietly relies on earning a rebate is fantasy at realistic volumes.
#
# The live bot always prefers GET /v1/feetiers at runtime; this table only
# backs the offline simulator. Verify against the exchange before trusting it.
SIM_FEE_TIERS = {
    "tiers": [
        {"level": 0, "name": "Base",  "volumeThreshold": 0,             "makerFeePpm": 150, "takerFeePpm": 450},
        {"level": 1, "name": "Tier1", "volumeThreshold": 5_000_000,     "makerFeePpm": 120, "takerFeePpm": 380},
        {"level": 2, "name": "Tier2", "volumeThreshold": 20_000_000,    "makerFeePpm": 80,  "takerFeePpm": 320},
        {"level": 3, "name": "Tier3", "volumeThreshold": 100_000_000,   "makerFeePpm": 40,  "takerFeePpm": 270},
        {"level": 4, "name": "Tier4", "volumeThreshold": 400_000_000,   "makerFeePpm": 0,   "takerFeePpm": 230},
        {"level": 5, "name": "Tier5", "volumeThreshold": 1_000_000_000, "makerFeePpm": -20, "takerFeePpm": 200},
        {"level": 6, "name": "Tier6", "volumeThreshold": 3_000_000_000, "makerFeePpm": -30, "takerFeePpm": 190},
    ]
}


@dataclass
class SimOrder:
    order_id: str
    client_id: str
    market: str
    side: str
    price: Decimal
    size: Decimal
    tif: str
    reduce_only: bool
    placed_at: float
    remaining: Decimal


@dataclass
class SimExchange:
    """A minimal matching venue with a random-walk mid and synthetic depth."""

    cfg: Config
    fees: FeeSchedule = field(default_factory=lambda: FeeSchedule.from_fee_tiers(SIM_FEE_TIERS))
    equity: Decimal = Decimal("1000")
    orders: dict[str, SimOrder] = field(default_factory=dict)
    positions: dict[str, Decimal] = field(default_factory=dict)
    entry: dict[str, Decimal] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0
    trade_seq: int = 0

    def __post_init__(self) -> None:
        self.rng = random.Random(self.cfg.sim_seed)
        self.mids: dict[str, Decimal] = {
            name: D(meta["oraclePrice"]) for name, meta in SIM_MARKETS.items()
        }
        self.spread_bps: dict[str, Decimal] = {name: Decimal("4") for name in SIM_MARKETS}

    # ------------------------------------------------------------- market ---
    def markets(self) -> dict[str, dict[str, Any]]:
        out = {}
        for name, meta in SIM_MARKETS.items():
            row = dict(meta)
            row["oraclePrice"] = dec_str(self.mids[name])
            row["markPrice"] = dec_str(self.mids[name])
            out[name] = row
        return out

    def step_prices(self) -> None:
        """Advance the mid by a TIME-SCALED random walk.

        `SIM_VOL_BPS` is volatility per *second*, not per loop iteration.
        Scaling by sqrt(elapsed) is what makes the simulation independent of
        the bot's loop interval — otherwise a faster loop silently multiplies
        the volatility the strategy faces and no spread can ever be profitable.
        """
        now = time.monotonic()
        elapsed = now - getattr(self, "_last_step", now - 0.1)
        self._last_step = now
        elapsed = min(max(elapsed * max(self.cfg.sim_speed, 0.01), 1e-3), 5.0)
        scale = Decimal(str(elapsed ** 0.5))

        for name in self.mids:
            shock = Decimal(str(self.rng.gauss(0, self.cfg.sim_vol_bps))) * scale
            self.mids[name] = max(D("0.01"), self.mids[name] * (Decimal(1) + shock / Decimal(10_000)))
            self.spread_bps[name] = Decimal(str(round(self.rng.uniform(2.0, 12.0), 2)))

    def book(self, market: str) -> dict[str, Any]:
        mid = self.mids[market]
        meta = SIM_MARKETS[market]
        half = mid * self.spread_bps[market] / Decimal(20_000)
        bid = snap_price(meta, mid - half, side="BUY")
        ask = snap_price(meta, mid + half, side="SELL")
        tick = D(meta["tickSize"])
        bids = [[dec_str(bid - tick * i), dec_str(D("0.5") + D(i) / 10)] for i in range(5)]
        asks = [[dec_str(ask + tick * i), dec_str(D("0.5") + D(i) / 10)] for i in range(5)]
        return {
            "bids": bids,
            "asks": asks,
            "lastSequenceId": self.seq,
            "globalSequenceId": self.seq,
            "timestamp": int(time.time() * 1_000_000),
        }

    def bbo(self, market: str) -> dict[str, Any]:
        b = self.book(market)
        return {
            "bestBid": {"price": b["bids"][0][0], "size": b["bids"][0][1]},
            "bestAsk": {"price": b["asks"][0][0], "size": b["asks"][0][1]},
            "timestamp": b["timestamp"],
            "lastSequenceId": self.seq,
            "globalSequenceId": self.seq,
        }

    # -------------------------------------------------------------- orders --
    def place(
        self,
        market: str,
        side: str,
        size: Decimal,
        price: Decimal,
        tif: str,
        reduce_only: bool,
        client_id: str,
    ) -> dict[str, Any]:
        self.seq += 1
        order_id = f"sim-{self.seq}"
        meta = SIM_MARKETS[market]
        notional = price * size

        if not reduce_only and notional < D(meta["minOrderNotional"]):
            return {"status": "REJECTED", "rejectionReason": "InvalidRequest", "orderId": order_id,
                    "clientId": client_id}
        position = self.positions.get(market, Decimal(0))
        if reduce_only:
            would = position + (size if side == "BUY" else -size)
            if position == 0 or abs(would) > abs(position):
                return {"status": "REJECTED", "rejectionReason": "REDUCE_ONLY_WOULD_INCREASE",
                        "orderId": order_id, "clientId": client_id}

        bbo = self.bbo(market)
        best_bid, best_ask = D(bbo["bestBid"]["price"]), D(bbo["bestAsk"]["price"])
        crosses = (side == "BUY" and price >= best_ask) or (side == "SELL" and price <= best_bid)

        if tif == "ALO" and crosses:
            return {"status": "REJECTED", "rejectionReason": "POST_ONLY_WOULD_CROSS",
                    "orderId": order_id, "clientId": client_id}

        if tif in {"IOC", "FOK"}:
            if not crosses:
                reason = "FOK_FAILED" if tif == "FOK" else "IOC_CANCELED"
                return {"status": "CANCELED", "rejectionReason": reason,
                        "orderId": order_id, "clientId": client_id}
            fill_price = best_ask if side == "BUY" else best_bid
            self._fill(market, side, size, fill_price, "TAKER", order_id, client_id)
            return {"status": "FILLED", "orderId": order_id, "clientId": client_id}

        self.orders[order_id] = SimOrder(
            order_id=order_id, client_id=client_id, market=market, side=side,
            price=price, size=size, tif=tif, reduce_only=reduce_only,
            placed_at=time.time(), remaining=size,
        )
        self.events.append({"type": "ACK", "orderId": order_id, "clientId": client_id, "market": market})
        return {"status": "ACK", "orderId": order_id, "clientId": client_id}

    def cancel(self, client_id: str = "", order_id: str = "") -> dict[str, Any]:
        target = None
        for oid, order in self.orders.items():
            if (order_id and oid == order_id) or (client_id and order.client_id == client_id):
                target = oid
                break
        if target is None:
            return {"status": "REJECTED", "rejectionReason": "ORDER_NOT_FOUND"}
        order = self.orders.pop(target)
        self.events.append({"type": "CANCELED", "orderId": target, "clientId": order.client_id,
                            "market": order.market})
        return {"status": "CANCEL_ACKNOWLEDGED", "orderId": target}

    def cancel_all(self) -> dict[str, Any]:
        count = len(self.orders)
        for order in list(self.orders.values()):
            self.events.append({"type": "CANCELED", "orderId": order.order_id,
                                "clientId": order.client_id, "market": order.market})
        self.orders.clear()
        return {"status": "OK", "canceled": count}

    # --------------------------------------------------------------- match --
    def match(self) -> list[Fill]:
        """Advance the clock and fill resting orders.

        Two distinct flow types, because collapsing them makes market making
        look either impossible or free:

        1. **Adverse (informed) flow** — the mid walks *through* a resting
           quote. The maker is filled and the market keeps going against it.
           This is the cost of providing liquidity and it is why a quote that
           only just clears fees still loses money.

        2. **Uninformed (liquidity-taking) flow** — a random taker crosses the
           spread and lifts whatever is at the touch without the mid moving.
           This is what a maker actually earns: the quote is filled at its own
           price and the position can be closed back at mid.

        `SIM_UNINFORMED_RATE` sets how often (2) arrives per step. Set it to 0
        for a worst-case, pure-adverse-selection stress test.
        """
        fills: list[Fill] = []
        for order in list(self.orders.values()):
            bbo = self.bbo(order.market)
            best_bid, best_ask = D(bbo["bestBid"]["price"]), D(bbo["bestAsk"]["price"])

            # (1) adverse: the market traded through the quote
            through = (order.side == "BUY" and best_ask <= order.price) or (
                order.side == "SELL" and best_bid >= order.price
            )
            # (2) uninformed: our quote is at/inside the touch and gets lifted
            at_touch = (order.side == "BUY" and order.price >= best_bid) or (
                order.side == "SELL" and order.price <= best_ask
            )
            uninformed = at_touch and self.rng.random() < self.cfg.sim_uninformed_rate

            if not (through or uninformed):
                continue
            if through and self.rng.random() > self.cfg.sim_maker_fill_prob:
                continue

            fill = self._fill(order.market, order.side, order.remaining, order.price,
                              "MAKER", order.order_id, order.client_id)
            fills.append(fill)
            self.orders.pop(order.order_id, None)
        return fills

    def _fill(
        self,
        market: str,
        side: str,
        size: Decimal,
        price: Decimal,
        liquidity: str,
        order_id: str,
        client_id: str,
    ) -> Fill:
        self.trade_seq += 1
        notional = price * size
        fee = self.fees.fee_for(notional, liquidity)
        signed = size if side == "BUY" else -size
        position = self.positions.get(market, Decimal(0))
        entry = self.entry.get(market, Decimal(0))

        if position == 0 or (position > 0) == (signed > 0):
            new_pos = position + signed
            if new_pos != 0:
                self.entry[market] = ((entry * abs(position)) + (price * abs(signed))) / abs(new_pos)
            self.positions[market] = new_pos
        else:
            closing = min(abs(signed), abs(position))
            direction = Decimal(1) if position > 0 else Decimal(-1)
            self.equity += (price - entry) * closing * direction
            self.positions[market] = position + signed
            if self.positions[market] == 0:
                self.entry[market] = Decimal(0)
            elif (self.positions[market] > 0) != (position > 0):
                self.entry[market] = price
        self.equity -= fee

        fill = Fill(
            trade_id=f"simtrade-{self.trade_seq}",
            market=market, side=side, price=price, size=size,
            liquidity=liquidity, fee=fee, ts=time.time(),
            order_id=order_id, client_id=client_id,
        )
        self.events.append({"type": "FILL", "orderId": order_id, "clientId": client_id,
                            "market": market, "fill": fill})
        return fill

    # ------------------------------------------------------------- account --
    def account(self) -> dict[str, Any]:
        unrealized = Decimal(0)
        for market, position in self.positions.items():
            if position:
                unrealized += (self.mids[market] - self.entry.get(market, Decimal(0))) * position
        equity = self.equity + unrealized
        used = sum(
            (abs(p) * self.mids[m] * D(SIM_MARKETS[m]["initialMarginFraction"])
             for m, p in self.positions.items() if p),
            Decimal(0),
        )
        return {
            "address": self.cfg.address or "0xsim",
            "accountIndex": self.cfg.account_index,
            "accountEquity": dec_str(equity),
            "equity": dec_str(equity),
            "freeCollateral": dec_str(max(Decimal(0), equity - used)),
            "netQuoteBalance": dec_str(self.equity),
            "netDeposits": "1000",
            "positions": {
                str(SIM_MARKETS[m]["marketId"]): {
                    "marketId": SIM_MARKETS[m]["marketId"],
                    "marketDisplayName": m,
                    "side": "LONG" if p > 0 else "SHORT",
                    "size": dec_str(abs(p)),
                    "averageEntryPrice": dec_str(self.entry.get(m, Decimal(0))),
                }
                for m, p in self.positions.items()
                if p
            },
        }

    def drain_events(self) -> Iterable[dict[str, Any]]:
        events, self.events = self.events, []
        return events
