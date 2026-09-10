# Strategy and economics

Why the bot quotes the way it does, and how "is this actually profitable?" was
measured rather than assumed.

---

## 1. The problem

> Generate maximum volume on testnet **without losing funds** — the captured
> spread must cover the fees that volume incurs.

Those two goals pull in opposite directions. Volume wants tight quotes and fast
fills; not-losing wants wide quotes and few fills. The entire design is the
management of that tension.

### The unit of account

Judge everything in **basis points of volume traded**, not dollars:

```
netBpsOfVolume = 10_000 × netPnl / volumeUsd
```

$50 of profit on $10M of volume (0.05 bps) is a rounding error that will invert
on the next tick. $2 on $10k (2 bps) is a real edge. Dollar PnL tells you what
happened; bps tells you whether it repeats.

### The economics of one round trip

A complete cycle is buy then sell (or the reverse):

```
gross_edge_bps  = captured spread between the two legs
fee_cost_bps    = fee(open) + fee(close)
net_edge_bps    = gross_edge_bps − fee_cost_bps
```

With maker fees `m` and taker fees `t`:

| Cycle | Fee cost | Comment |
| --- | --- | --- |
| maker in, maker out | `2m` | the target — cheapest possible |
| maker in, taker out | `m + t` | acceptable when inventory must go |
| taker in, taker out | `2t` | pure fee donation; never deliberate |

At the published Base tier of **1.5 bps maker / 4.5 bps taker**, a maker/maker
cycle needs
**> 3 bps** of captured spread just to break even, and a maker/taker cycle needs
**> 6 bps**. This is why `BOT_SPREAD_BPS` defaults to **10** and why the code
physically refuses to quote a pair tighter than
`PnLTracker.edge_required_bps()`. Fee tiers are read live from `/v1/feetiers` —
at high volume, maker fees go **negative** (rebates), which flips the arithmetic
in the maker's favour.

---

## 2. Why a naive market maker loses money

Three distinct failure modes, each with a specific defence in the code.

### 2.1 Adverse selection

Your resting quote is an option you wrote for free. It fills when someone wants
the other side — which is disproportionately when they are right and you are
wrong. You buy just before the price falls. The spread you captured is smaller
than the move you got run over by.

**Defence:** widen with realised volatility.
`MarketState.observe_vol` maintains an EWMA of bps returns and
`edge_bps()` adds `BOT_VOL_EDGE_MULTIPLIER × vol_bps`. A static spread in a
moving market is the classic way to bleed.

### 2.2 Inventory risk

Fills arrive one-sided. You accumulate a position, and the position's directional
PnL swamps the spread capture — a $150 position moving 1% is $1.50, which is
sixty maker fills at 25×10 bps.

**Defence:** three layers.
- `BOT_INVENTORY_SKEW_BPS` shifts both quotes against inventory, making the
  reducing side more attractive so the book flattens you for free.
- `BOT_MAX_INVENTORY_NOTIONAL_USD` → flatten-only mode.
- `BOT_INVENTORY_MAX_AGE_S` → cross the spread and pay the taker fee.
  Paying a known, bounded 4.5 bps beats carrying unbounded directional risk.

### 2.3 Fee drag

Even with perfect symmetry, if quoted spread < round-trip fees, every fill is a
guaranteed loss. Volume then actively destroys equity.

**Defence:** `edge_bps()` takes a `max()` against the fee floor, and — critically
— the check is **repeated after tick snapping**. Rounding to a coarse tick tier
can silently erase 1–2 bps of margin, turning a profitable quote into a losing
one between calculation and placement.

---

## 3. `volume-maker`

The default. Post-only, two-sided, continuously.

```
required_edge = max(
    BOT_SPREAD_BPS,                                  # operator floor
    fee_maker + fee_maker + BOT_FEE_BUFFER_BPS,      # fee floor
    fee_floor + BOT_VOL_EDGE_MULTIPLIER × vol_bps,   # adverse-selection premium
)

bid = mid × (1 − required_edge/2/10_000) − skew
ask = mid × (1 + required_edge/2/10_000) − skew
```

1. **Quote both sides** with ALO (post-only). ALO can never pay a taker fee — if
   it would cross, the exchange rejects it for free (`POST_ONLY_WOULD_CROSS`).
   That single TIF choice makes the worst case "no fill" instead of "bad fill".
2. **Join or improve the touch** when the book is wider than the required edge
   (`BOT_JOIN_BBO`), but never inside our own profitability.
3. **Snap to the grid, then re-verify** the pair still clears the edge.
4. **On a fill**, keep quoting two-sided rather than going passive:
   - the **reducing leg** becomes `reduce_only`, sized to cover the whole
     position, priced at `entry ± required_edge` (`close_price()`), so if it
     fills the round trip is booked profitable by construction;
   - the **adding leg** stays live but capped by remaining notional headroom, so
     the bot keeps earning volume instead of stalling on one-sided inventory.
5. **Escalate to taker** (reduce-only IOC with a clamped protective price) only
   for: shutdown, position over the notional cap, inventory older than
   `BOT_INVENTORY_MAX_AGE_S`, or `ping-pong` mode.

**Mere quote staleness must never trigger a taker close.** An earlier version
conflated "this quote is old" with "this inventory is old" and escalated on
`BOT_MAX_QUOTE_AGE_S` (20s), paying taker fees constantly. Splitting the two
concepts — stale quote → re-quote; stale *inventory* → flatten — moved net PnL
from −4.82 to −3.26 bps in one change. They are separate clocks.

### Variants
- **`ping-pong`** — same skeleton, always taker-closes immediately after a maker
  fill. More volume per unit time, worse edge per unit volume. Use only when
  volume is the sole objective and fees are rebated.
- **`spot-rfq`** — Stock Token spot is **zero-fee**, so *any* non-negative
  execution is volume for free. There is no book to rest in; the strategy emits
  round-trip intents gated on quoted slippage.

---

## 3b. Fee tiers change the economics, not just the cost

Fees are tiered on trailing 30-day volume. The bot reads them live and reports
how far the next tier is and what it is worth:

```
next fee tier VIP1 at $1000000 volume (0.03% there): saves 2 bps per round trip
```

`savingBpsPerRoundTrip` is the concrete prize — how much cheaper a maker/maker
cycle becomes — which is what decides whether pushing for the next tier is
worth the volume it costs to get there.

At high tiers **maker fees go negative**: you are paid to rest liquidity. That
inverts the arithmetic. `round_trip_bps()` legitimately returns a negative
number, and the code must not clamp it to zero — a rebate is real income and
correctly lowers the spread the quoter needs.

But it must not lower it to *nothing*. With fees at −1 bps round trip and a
zero buffer, the fee-derived floor becomes negative, and a naive quoter would
post both sides at mid: maximum fill rate, zero edge, maximum adverse
selection. **`BOT_MIN_EDGE_BPS` (default 1) is the absolute floor that survives
any tier.** The relevant test is
`test_rebate_tier_never_drops_the_edge_to_zero`.

The practical consequence: the edge widens as you climb tiers, so a spread that
is marginal at Base may be comfortably profitable at VIP1. Re-measure after a
tier change rather than assuming the old setting still holds.

## 3c. Adapting to what the market does back

A fixed spread is a bet that conditions never change. `arcusbot/adaptive.py`
turns the exchange's feedback into two multipliers — one on the required edge,
one on the re-quote interval — and applies them every loop.

| Signal | Effect | Reasoning |
| --- | --- | --- |
| Net PnL falling over the window | edge x1.35, interval x1.5 | The strategy is being beaten; demand more and trade less |
| >60% of fills move against us | edge x(1 + excess) | Direct measurement of adverse selection |
| Volatility exceeds the spread | edge up to x2 | The spread cannot pay for the risk being taken |
| Top-of-book depth < 3x our clip | edge x1.2, interval x1.3 | We *are* the liquidity; fills will be informed |
| Inventory > 70% of cap | interval x1.4 | Prioritise reducing over adding |
| Inventory older than 5 min | edge x1.15 | Stale risk should cost more to add to |
| Rate limited | interval x2.5 | Back off rather than collect 429s |
| >=3 API errors in 60s | interval up to x2.5 | Something is wrong; slow down |

**Speeding up is the only adjustment with preconditions.** The interval is
reduced (x0.75) *only* when net PnL is rising, fewer than 40% of fills are
adverse, there have been no API errors, the bot is not rate limited, quotes are
actually filling (>10%) and inventory is under half the cap. Any single one of
those failing leaves the frequency alone.

This is the rule that keeps "generate volume" honest: **the bot never trades
faster while it is losing money.** Volume is only worth having if it is not
being bought with capital.

Both multipliers are bounded (edge 0.85x–4x, interval 0.6x–6x) so stacked bad
news cannot produce an absurd quote or a frozen bot, and the adaptive edge is
always floored at the fee-derived minimum — no market condition makes an
unprofitable fill acceptable.

Live values are in `status.json` under `adaptive`, per market.

---

## 4. Measuring the edge honestly

### The simulator's job

`arcusbot/sim.py` is a **machinery test, not a market model**. It proves the bot
quotes, fills, closes, accounts, and halts correctly, offline and deterministic
(seeded). It does *not* prove that a given spread is profitable on real Arcus —
that is a property of real flow.

It models what matters for the maker's economics:
- **Crossing-only fills.** Makers do not fill on a mere touch. An earlier version
  filled on touch, which hid adverse selection completely and made every spread
  look profitable.
- **Two flow types**: informed (trades *with* the next price move — toxic) and
  uninformed (random — the flow makers actually earn from), mixed by
  `SIM_UNINFORMED_RATE`.
- **Real fee arithmetic**, maker vs taker, from the same `FeeSchedule` as live.
- **Time-scaled volatility** — see below.

### Two modelling bugs that inverted the conclusion

Both are worth recording, because both produced confident, wrong answers.

**1. Fill-on-touch.** Filling makers whenever price touched the quote removed
adverse selection entirely. Every spread looked profitable. Fixed: fills require
a strict cross.

**2. Per-iteration volatility.** `sim.step_prices()` applied a volatility shock
*per loop iteration* rather than per unit of wall-clock time. This coupled
realised volatility to `BOT_LOOP_INTERVAL_S`: shrinking the loop for faster
iteration silently multiplied the volatility the strategy faced, so **no spread
could ever clear fees**, and it looked like a strategy failure. Fixed: shocks
scale by `sqrt(elapsed × sim_speed)` from a monotonic clock. The very next sweep
went positive.

The lesson generalises: when a backtest says "nothing works", suspect the
harness before the strategy.

### The sweep

One run of one spread on one seed is noise. `sweep` runs the full engine across
a grid of spreads × seeds and reports **mean net bps, stdev, and win count**:

```bash
python -m arcusbot sweep --sweep-spreads 6,10,16,24 --sweep-seeds 4 --sweep-duration 20
```

It forces `sim_speed ≥ 5`, a 0.2s loop, and silences per-run output. Exit code
`1` when no spread clears fees. Keep runs to ~12–20s per cell — 45s × 3 seeds ×
6 configs blew a 1000s timeout.

**Read the stdev.** A spread averaging +0.18 bps with a stdev of 5.57 has no
demonstrated edge; one averaging +0.97 with a stdev of 0.64 does.

### Measured results

3 seeds per cell, BTC-USD + ETH-USD, $25 clips:

| Flow regime | Spread (bps) | Net bps of volume | Profitable seeds |
| --- | --- | --- | --- |
| Mostly adverse (`SIM_UNINFORMED_RATE=0.25`, the default) | 10 | −1.46 | 1/3 |
| " | 18 | −0.21 | 1/3 |
| " | 20 | −1.78 … −2.56 | 0–1/3 |
| Balanced (`SIM_UNINFORMED_RATE=0.7`) | **10** | **+0.46 … +0.97** (± 0.6–1.0) | **2/3 – 3/3** |
| Balanced | 20 | +0.10 … +0.18 (± 5.5) | 2/3 |

Maker share 88–94%. Default `BOT_SPREAD_BPS` was set to **10** on this evidence.

The balanced-flow rows are given as ranges because the simulator advances on
**wall-clock** time (volatility scales by `sqrt(elapsed)`), so results vary a
little with machine load even at a fixed seed. Repeated runs of the 10 bps cell
land between +0.46 and +0.97 bps with 2–3 of 3 seeds profitable; the 20 bps cell
is positive on average but its stdev is larger than its mean, i.e. **no
demonstrated edge**. Only the 10 bps result is strong enough to act on, and only
as a starting point for live measurement.

Also measured: sweeping *tighter* (0.5–3 bps) is catastrophic (≈ −14 bps).
Near-touch quoting harvests almost exclusively adverse fills — it maximises fill
rate and minimises edge, which is exactly backwards.

### The honest caveat

**Under near-pure adverse selection the strategy loses ~1.5–2.6 bps, and this
is correct behaviour, not a bug.** No market maker can beat flow that is
systematically informed; the theoretical response is to widen until you stop
filling, which earns nothing. The strategy captures edge as soon as flow is not
purely toxic (`SIM_UNINFORMED_RATE ≥ 0.35`), which is the case on any venue with
real two-sided participation.

The temptation here is to tune the simulator until the bot looks good. That was
explicitly rejected. A simulator tuned to flatter the strategy tells you nothing
except that you can tune a simulator.

---

## 4b. The real fee schedule

Transcribed from the exchange's published **Perpetuals Fee Tiers** table. The
simulator ships these exact numbers (`sim.SIM_FEE_TIERS`, pinned by
`test_sim_schedule_matches_the_published_arcus_tiers`); the live bot always
reads `GET /v1/feetiers` at runtime and never hard-codes them.

| Tier | 30d volume | Maker | Taker | Maker/maker RT | Maker/taker RT |
| --- | --- | --- | --- | --- | --- |
| 0 | $0 | 1.5 bps | 4.5 bps | 3.0 bps | 6.0 bps |
| 1 | >= $5M | 1.2 | 3.8 | 2.4 | 5.0 |
| 2 | >= $20M | 0.8 | 3.2 | 1.6 | 4.0 |
| 3 | >= $100M | 0.4 | 2.7 | 0.8 | 3.1 |
| 4 | >= $400M | **0** | 2.3 | **0** | 2.3 |
| 5 | >= $1B | **-0.2** (rebate) | 2.0 | **-0.4** | 1.8 |
| 6 | >= $3B | **-0.3** (rebate) | 1.9 | **-0.6** | 1.6 |

Two consequences worth stating plainly:

* **Maker rebates begin at $1B of 30-day volume.** At the bot's realistic
  throughput that is unreachable, so any reasoning that leans on earning a
  rebate is fantasy. The rebate code paths exist and are tested, but they
  should be treated as unreachable in practice.
* **The base maker/taker round trip is 6 bps, not 5.5.** Every taker escalation
  (stale inventory, over-cap, shutdown) is more expensive than earlier drafts of
  this document assumed, which strengthens the existing rule that crossing the
  spread is a last resort.

---

## 4c. What the latest measurements actually show

Measured 2026-09-10 in the simulator, 3 seeds x 14s per configuration, using the
**corrected** fee schedule above. **These are simulator numbers and they do not
demonstrate profitability.**

Default (hostile) flow — `SIM_UNINFORMED_RATE=0.25`, i.e. mostly informed
counterparties:

| Spread | Volume | Net bps | Stdev | Maker% | Verdict |
| --- | --- | --- | --- | --- | --- |
| 8 | $1,884 | **-0.88** | 2.86 | 87.3% | loses |
| 14 | $1,658 | **-1.88** | 3.71 | 83.4% | loses |
| 22 | $1,513 | **-0.50** | 1.40 | 84.3% | loses |

Mixed flow — `SIM_UNINFORMED_RATE=0.7`:

| Spread | Volume | Net bps | Stdev | Maker% | Verdict |
| --- | --- | --- | --- | --- | --- |
| 10 | $1,764 | **-1.79** | 4.93 | 81.2% | loses |
| 16 | $1,890 | **+0.82** | 2.06 | 90.7% | **within noise** |
| 22 | $1,456 | **-3.52** | 5.67 | 85.4% | loses |

Read this carefully. The single positive result, +0.82 bps, has a standard
deviation of 2.06 across seeds — the error bar is more than twice the signal.
**That is not evidence of an edge.** The honest summary:

* Against predominantly informed flow the strategy loses at every spread
  tested. Adverse selection exceeds the captured spread. This is the expected
  result for a pure maker with no directional model, and tuning the simulator
  until it looked better would be self-deception.
* Against mixed flow at a wide (16 bps) spread it is approximately
  **break-even**, which is the stated objective — volume that pays for itself.
* Both too tight (10) and too wide (22) are worse: too tight gets picked off,
  too wide stops filling and the few fills that land are the informed ones.
* Maker share is consistently 81–91%, so the fee side is working as designed.

The practical conclusion: **the spread must be near 16 bps, and the flow must
not be predominantly informed.** Neither is something the bot controls, which is
exactly why the mainnet capital cap exists and why testnet must show a positive
`netBpsOfVolume` over days — not one 14-second sample — before real money is
committed.

Reproduce with:

```bash
python -m arcusbot sweep --sweep-spreads 8,14,22 --sweep-seeds 3 --sweep-duration 14
SIM_UNINFORMED_RATE=0.7 python -m arcusbot sweep --sweep-spreads 10,16,22 --sweep-seeds 3
```

---

## 5. Applying this on real testnet

The sim gives you a *starting* spread, not a validated one. Repeat the
measurement against real flow:

1. Run `--mode live` for 15 minutes at `BOT_SPREAD_BPS=10`, one market.
2. Read `netBpsOfVolume` and `makerShare` from the report.
3. If net > 0 and maker share is high, tighten by 2 bps and repeat.
4. If net < 0, widen by 4 bps, or check whether taker escalations are the cause
   (`makerShare` low → raise `BOT_INVENTORY_MAX_AGE_S`).
5. Stop tightening one step **before** the spread that first prints negative.

Testnet flow is typically far less informed than mainnet — mostly other bots and
scripted activity — so the realistic expectation is closer to the balanced-flow
row than the adverse row. Verify; don't assume.

---

## 6. Accounting

`pnl.py` implements **average-cost** inventory accounting:

- Same-direction fill → weighted-average entry price update, no realised PnL.
- Opposing fill → realise `(exit − entry) × closed_size × sign`, entry unchanged.
- A fill that **flips through zero** realises against the old position, then
  opens a fresh one at the fill price for the remainder. (Getting this wrong is
  the single most common PnL bug in trading bots.)
- Fees accumulate separately, in ppm, per fill. Negative fees are rebates.
- Fills are **idempotent by `tradeId`** — the snapshot and the stream overlap.

The identity that must always hold, asserted in `selftest` and in
`tests/test_pnl.py`:

```
net = realized + unrealized + funding + rebates − fees
```

Derived metrics: `netBpsOfVolume` (the headline), `feeCoverageRatio`
(gross ÷ fees; > 1 means the volume paid for itself), `makerShare`, and
`maxDrawdown` (peak-to-trough on the equity curve, which drives the kill switch).
