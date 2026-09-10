# Testnet validation protocol

> **Status: NOT STARTED — blocked on network access.**
> No real Arcus testnet execution has happened. Every number produced so far
> comes from the offline simulator and is **not** evidence of a trading edge.

This document is the procedure that decides whether the strategy is worth
putting real money behind. It is deliberately written so that the *answer* is
allowed to be "no".

---

## 0. Why this phase exists

The code is in reasonable shape: risk limits are enforced, lost acknowledgements
are reconciled, the kill switch is tested. None of that establishes that the
strategy *makes money*.

At the published Base tier the fee floor is:

| Round trip | Cost |
| --- | --- |
| maker → maker | **3.0 bps** |
| maker → taker | **6.0 bps** |
| taker → taker | **9.0 bps** |

The strategy must beat that *after* slippage and adverse selection. The best
simulator result so far is **+0.82 bps with a ±2.06 standard deviation** — an
error bar more than twice the signal. That is not an edge; it is noise that
happens to be positive.

**The question this phase answers:** across enough real fills and enough market
regimes, is `netBpsOfVolume` credibly greater than zero?

---

## 1. Blocker

The sandbox this repo was built in cannot reach Arcus:

```
$ openssl s_client -connect api.testnet.arcus.xyz:443
CONNECTED(00000003)
SSL handshake has read 0 bytes and written 334 bytes
error: unexpected eof while reading
```

TCP connects, then the connection is closed on ClientHello. `github.com`
resolves and serves fine, so this is an egress allowlist, not a broken TLS
stack. **Validation must be run from a machine with real network access.**

Running elsewhere? See [`LOCAL_AGENT_HANDOFF.md`](LOCAL_AGENT_HANDOFF.md).

**Set `ARCUS_VENUE=arcus` first.** `.env.example` ships `sim`, and with `sim`
the bot never contacts Arcus even under `--mode live --network testnet`. The
`evidence` command refuses to count such sessions, but check the setting anyway.

Verify before starting:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.testnet.arcus.xyz/health   # expect 200
python -m arcusbot preflight --wallet t1                                          # expect READY
```

---

## 2. Rules for this phase

* **Simulator results are not evidence.** Do not cite them as live performance.
* **Never tune the simulator to produce a positive number.**
* **Do not enable mainnet.** No mainnet keys, no raising mainnet limits.
* **Do not modify accounting or risk logic to improve a result.** If the number
  is bad, the number is the finding.
* **Do not re-tune parameters after every short session.** Collect raw results
  first, then change one thing at a time.
* **Spot comes later.** Perps must be shown to execute and account correctly
  first. Spot RFQ currently emits *intents* and is not an executing path — do
  not describe it as automated until it is.

---

## 3. Starting configuration

Begin deliberately small: one market, maker-only, tight inventory.

```bash
BOT_MARKETS=BTC-USD
BOT_CAPITAL_USD=200          # testnet funds; enough for meaningful fills
BOT_ORDER_NOTIONAL_USD=25
BOT_MAX_POSITION_NOTIONAL_USD=50
BOT_MAX_INVENTORY_NOTIONAL_USD=50
BOT_SPREAD_BPS=16            # best simulator spread; re-derive from live data
RISK_MAX_DRAWDOWN_USD=10
RISK_MAX_DAILY_LOSS_USD=20
```

Session length: **15–30 minutes** at first, then extend to 2–4 hours once the
execution path is proven clean.

---

## 4. Running a session

Tag every session with its market regime so results can be grouped:

```bash
python -m arcusbot run --wallet t1 --mode live \
    --markets BTC-USD --duration 1800 --regime low-vol --port 8080
```

Each session automatically appends a full record to `state/evidence.jsonl`.

### The 22 recorded metrics

| # | Metric | Field |
| --- | --- | --- |
| 1 | executed volume | `volume_usd` |
| 2 | fills | `fills` |
| 3 | maker fills | `maker_fills` |
| 4 | taker fills | `taker_fills` |
| 5 | gross PnL | `gross_pnl` |
| 6 | total fees | `fees_paid` |
| 7 | slippage | `slippage_bps` |
| 8 | adverse selection | `adverse_selection_bps` |
| 9 | realized PnL | `realized_pnl` |
| 10 | unrealized PnL | `unrealized_pnl` |
| 11 | net PnL | `net_pnl` (derived) |
| 12 | netBpsOfVolume | `net_bps_of_volume` |
| 13 | feeCoverageRatio | `fee_coverage_ratio` |
| 14 | fill rate | `fill_rate` |
| 15 | average inventory | `avg_inventory_usd` |
| 16 | maximum inventory | `max_inventory_usd` |
| 17 | max inventory age | `max_inventory_age_s` |
| 18 | max drawdown | `max_drawdown_usd` |
| 19 | rejection rate | `rejection_rate` |
| 20 | API errors | `api_errors` |
| 21 | websocket disconnects | `ws_disconnects` |
| 22 | flatten success | `flatten_succeeded`, `residual_positions` |

---

## 5. Test matrix

Cover all of these before drawing any conclusion. Tag with `--regime`.

| | Scenario | How to reach it | Tag |
| --- | --- | --- | --- |
| A | Low volatility | quiet hours | `low-vol` |
| B | Normal volatility | typical session | `normal-vol` |
| C | High volatility | around a news event / US open | `high-vol` |
| D | Thin liquidity | off-hours, or a smaller market | `thin` |
| E | Wide spread | observe, do not force | `wide-spread` |
| F | Narrow spread | observe, do not force | `narrow-spread` |
| G | Increasing inventory | raise the inventory cap for one run | `inventory-build` |
| H | Forced flatten | `--duration` expiry with a position open | `forced-flatten` |
| I | Websocket interruption | drop the network mid-session | `ws-interrupt` |
| J | API timeout / retry | firewall the API briefly | `api-timeout` |
| K | Restart with live orders | SIGKILL, then restart | `restart` |

Scenarios I–K test **execution correctness**, not profitability. Success there
means: no duplicate exposure, no untracked orders, no reconciliation
discrepancies, and the position is what the exchange says it is.

---

## 6. Reading the evidence

```bash
python -m arcusbot evidence            # human-readable
python -m arcusbot evidence --json     # machine-readable
```

Exit code is **0 only when the evidence supports proceeding**, otherwise 1.

The verdict is one of:

| Verdict | Meaning |
| --- | --- |
| `NO-DATA` | nothing recorded |
| `INSUFFICIENT-DATA` | fewer than 5 sessions or 400 fills — no claim possible |
| `NEGATIVE-EDGE` | aggregate net PnL is not positive |
| `INCONCLUSIVE` | positive but not distinguishable from noise |
| `POSITIVE-EDGE` | positive, realized, fee-covering, and t >= 2.0 |

### Why the thresholds are what they are

* **400 fills / 5 sessions** — below this, a run of luck explains the result.
* **t >= 2.0** — the mean edge must sit at least two standard errors above
  zero. `+0.8 bps ±2.1` fails this; that is the point.
* **realized-only must also be positive** — a favourable mark on open inventory
  is not profit. A session that is only profitable on unrealized PnL is
  explicitly flagged `unrealizedDependent` and does not count.
* **feeCoverageRatio > 1** — the gross edge must pay for the fees. Being
  rescued by funding or rebates is not a market-making edge.

---

## 7. If the result is negative

That is a legitimate outcome. **Do not adjust accounting to improve it.**
Diagnose which cause fits the data:

| Symptom | Likely cause |
| --- | --- |
| high maker share, negative net, adverse bps > 0 | **adverse selection** — quotes are stale |
| very low fill rate | spread too wide, or wrong market |
| net ≈ −fees | **fee drag** — no real edge captured |
| large `max_inventory_usd`, old inventory | inventory not being worked off |
| taker fills > ~15% | **exit execution** — too many escalations |
| rejections > 10% | tick/size/price-band errors |
| negative only in `high-vol` | volatility term under-weighted |
| negative in every regime | the **strategy assumption** is wrong |

Then change **one** thing and re-run the matrix. Record both results.

---

## 8. Only then: mainnet

Proceed only when `python -m arcusbot evidence` reports `POSITIVE-EDGE`,
`readyForMainnet: true`, and an empty `blockers` list — meaning:

- positive aggregate `netBpsOfVolume`
- positive **realized** net PnL
- `feeCoverageRatio` > 1
- no persistent inventory accumulation
- no reconciliation discrepancies
- no kill-switch or flatten failures
- stable across at least 5 sessions and multiple regimes

The first mainnet run is then an **experiment, not a deployment**: $20, one
market, $15 max notional, $1 drawdown stop, watched live. See
[`BOT_OPERATIONS.md` §8](BOT_OPERATIONS.md).

---

## 9. Current status

| Item | State |
| --- | --- |
| Code / risk architecture | approaching ready |
| Evidence harness | **built and tested** (this document) |
| Live testnet validation | **not started — no network access** |
| Trading edge | **not demonstrated** |
| Mainnet readiness | **NO** |
