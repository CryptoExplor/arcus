# Pre-mainnet audit

Findings from auditing the repository against the requirement to run real money
on mainnet. Ordered by severity. Each entry records the defect, why it matters
with real funds, and the fix.

---

## A1 — Lost acknowledgement creates untracked exposure (CRITICAL)

**Was:** `Engine._do_place` treated any exception as "the order did not happen".
A REST timeout, a dropped connection, or a 5xx after the matching engine
accepted the order left the exchange holding a live order the bot had no record
of — so it never cancelled it, never counted it, and quoted *again* on the same
side.

On testnet that is a confusing log. On mainnet it is uncontrolled exposure that
grows every time the network hiccups, and the position cap cannot see it.

**Fixed:** placement failures are now classified.

- **Definitive rejections** (400/422 with a rejection reason, tick errors,
  min-notional) → the order certainly does not exist. Continue.
- **Indeterminate failures** (timeout, connection reset, 5xx, no response) →
  the order *may* exist. The client ID is recorded in
  `Engine.pending_unknown` and the engine enters **reconcile-before-open**: it
  queries authoritative exchange state (`/v1/openOrders` + `/v1/positions`) and
  refuses to place any new opening order until local state matches.

`docs/BOT_OPERATIONS.md` §"Uncertain execution" documents the operator view.
Tests: `tests/test_execution.py::test_indeterminate_place_blocks_new_opens`,
`::test_reconciliation_adopts_an_unknown_live_order`.

---

## A2 — Blanket mainnet refusal, no gating machinery (CRITICAL for the goal)

**Was:** `config.validate()` refused `mode=live` on mainnet unconditionally.
Safe, but it meant there was no mainnet path at all — and no structure a user
could accidentally *half*-satisfy either.

**Fixed:** a deliberate, multi-key gate (`arcusbot/mainnet.py`). Every one of
these must be explicitly true, and any malformed value fails closed:

```env
ARCUS_NETWORK=mainnet
BOT_MODE=live
BOT_MAINNET_ENABLED=true          # explicit opt-in, no default
BOT_MAINNET_CAPITAL_USD=20        # hard cap, must be >0 and <= BOT_MAINNET_MAX_CAPITAL_USD
BOT_MAINNET_ACK=i-understand-the-risk   # typed acknowledgement
```

Missing *or malformed* → refuse with the exact reason. `BOT_MAINNET_ENABLED=1`,
`yes`, `TRUE` are accepted as true; anything unparseable is false, never true.
The cap is enforced by the capital planner and re-asserted by the risk manager
each loop, not merely documented.

---

## A3 — Order-rate and open-order caps were local-only (HIGH)

**Was:** `RiskManager` counted orders it *sent*. Orders resting from a previous
process, or adopted during reconciliation, were invisible to the cap.

**Fixed:** `reconcile()` seeds the live-order view from the exchange, and the
open-order cap is evaluated against reconciled state.

---

## A4 — No mainnet-specific risk floor (HIGH)

**Was:** the same defaults applied to a $100k testnet faucet and a $20 mainnet
account. `RISK_MAX_DRAWDOWN_USD=25` on a $20 account is not a limit.

**Fixed:** `mainnet.mainnet_risk_floor()` clamps limits to a fraction of the
mainnet capital cap whenever the mainnet gate is active, and the clamp only
ever tightens. See `docs/CAPITAL.md` §"Mainnet".

---

## A5 — `min_edge_bps` defined but never read (HIGH — fixed previously)

At a rebate tier the fee term goes negative and the required edge collapsed to
`0.0`, which would quote both sides at mid. Now floored by `BOT_MIN_EDGE_BPS`
(default 1). Covered by `tests/test_fee_tiers.py`.

---

## A6 — `.env` inline comments crashed parsing (MEDIUM — fixed previously)

`ARCUS_ACCOUNT_INDEX=0  # subaccount` raised `ValueError` on `int()`. Parser now
strips unquoted trailing comments. Covered by `tests/test_dotenv.py`.

---

## A7 — No market eligibility screen (MEDIUM)

**Was:** every market in `BOT_MARKETS` was quoted regardless of whether its
spread could clear fees, or whether one clip even met its minimum notional. On
a $20 account most markets are simply not tradable.

**Fixed:** `arcusbot/selection.py` scores markets on spread-vs-required-edge,
volatility, depth, and affordability, and quotes only those that pass. Re-scored
periodically. See `docs/STRATEGY.md` §"Market selection".

---

## A8 — Strategy did not react to its own results (MEDIUM)

**Was:** spread adapted to volatility but not to *measured* outcomes. A market
losing money kept being quoted at the same width.

**Fixed:** `MarketWorker` tracks a rolling per-market realised edge and adverse
fill rate, and widens or stands down when recent round trips are negative.
Never the reverse — the bot does not tighten because it is losing.

---

## A9 — Sizing was not floored at the venue minimum per market (LOW)

Handled inside `capital.plan_capital`, but the per-market minimum notional
varies. Now checked per market in `selection.py` so an unaffordable market is
skipped rather than generating rejections.

---

## Non-defects — deliberate designs confirmed correct

- **Reduce-only bypasses halt/throttle.** Correct: halting must never trap the
  bot in a position it cannot close.
- **Positions are not persisted locally.** Correct: the exchange is the
  authority on inventory.
- **`round_trip_bps()` may return negative.** Correct: a rebate is real income.
- **Spot is RFQ, not a CLOB.** The repository models it as intents; unchanged.
