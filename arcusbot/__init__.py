"""Arcus testnet volume/market-making bot.

Package layout
--------------
config      typed configuration loaded from env / .env
signing     Ed25519 request signing (Scheme 1 typed payload, Scheme 2 legacy)
scaling     decimal <-> engine integer (ticks / quantums) conversion, snapping
rest        signed REST client (orders, reads)
ws          WebSocket market-data + account stream client
book        local L2 order book / BBO state
pnl         fee-aware PnL + volume ledger
risk        hard risk guards and the kill switch
strategy    maker-first volume strategy (perp) + spot RFQ strategy
engine      the async trading loop that wires everything together
sim         offline paper exchange used by `--venue sim` (no network)
"""

__version__ = "1.0.0"
