# Bot operations runbook

How to run this bot, what each knob does, what to watch, and what to do when it
misbehaves. For the venue's own rules see [`ARCUS_PLATFORM.md`](ARCUS_PLATFORM.md);
for why the strategy is shaped the way it is see [`STRATEGY.md`](STRATEGY.md).

---

## 1. The operating ladder

Never skip a rung. Each one catches a class of failure the next one would make
expensive.

| Rung | Command | Catches |
| --- | --- | --- |
| 1 | `python -m pytest tests -q` | signing, tick math, PnL identity, risk logic |
| 2 | `python -m arcusbot selftest --venue sim` | end-to-end loop, flatten, reporting |
| 3 | `python -m arcusbot sweep` | whether your spread clears fees at all |
| 4 | `python -m arcusbot preflight` | credentials, balance, market grid, live fees |
| 5 | `python -m arcusbot quote` | the exact prices it would post, right now |
| 6 | `python -m arcusbot run --mode dry-run` | full loop against live data, no orders |
| 7 | `python -m arcusbot run --mode live --duration 900` | the real thing, time-boxed |
| 8 | `python -m arcusbot history` | cumulative totals and loss carry-over across runs |
| 9 | **testnet live for days, profitably** | whether the strategy actually works |
| 10 | `--network mainnet` (section 8) | real money — only after rung 9 is green |

---

## 1b. Absolute beginner: five minutes, no keys, no money

If you have never run this before, do exactly this. It needs no API key, no
wallet, no funds and no internet access to the exchange — it runs against a
built-in paper exchange.

```bash
git clone <this repo> && cd arcus
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m arcusbot selftest --venue sim
```

You should see `SELFTEST OK` after about 40 seconds. That means the maths,
the risk guards and the accounting all work on your machine.

**Then watch it trade, with a live dashboard:**

```bash
.venv/bin/python -m arcusbot run --venue sim --mode dry-run --duration 120 --port 8080
```

Open <http://localhost:8080>. The big word at the top tells you what is
happening: `PROFITABLE`, `LOSING`, `WARMING-UP`, and so on. Nothing here can
cost you anything — `--venue sim` never touches the network.

**What the four safety levels mean** (this is the thing to understand before
anything else):

| | Command | Touches network? | Can lose money? |
| --- | --- | --- | --- |
| 1 | `--venue sim` | No | **No** — a fake exchange on your laptop |
| 2 | `--venue arcus --mode dry-run` | Reads only | **No** — prints orders instead of sending |
| 3 | `--venue arcus --mode live` on **testnet** | Yes | No — testnet funds are worthless |
| 4 | `--venue arcus --mode live` on **mainnet** | Yes | **YES — real money** |

Levels 1 and 2 are free to experiment with. Level 3 needs an API key. Level 4
additionally needs the deliberate opt-in described in section 8, and is capped.

**If something goes wrong**, the bot tries to tell you what to do next — for
example a mistyped market prints `did you mean BTC-USD?`, and a network failure
points you back to `--venue sim`. If you get stuck, `--log-level DEBUG` shows
the full detail.

---

## 2. Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in credentials
```

`.env` holds secrets and is gitignored; `.env.example` is the shareable
template with the same keys. Values may carry inline `# comments`, and real
environment variables always override the file — `BOT_SPREAD_BPS=12 python -m
arcusbot run` works as expected.

New to Arcus? Signing up through <https://testnet.arcus.xyz/ref/ARCUS>
(mainnet: <https://app.arcus.xyz/ref/AIAGENT>) credits this tool.

### Credentials

Easiest: <https://testnet.arcus.xyz/api-keys> — connect your wallet, generate,
**copy the API Signing Key immediately** (shown once), pick a subaccount and
validity, authorize. Put the signing key in `ARCUS_API_SECRET` and the wallet
address in `ARCUS_ADDRESS`.

Scripted alternative:

```bash
.venv/bin/pip install -r requirements-onboard.txt
.venv/bin/python tools/onboard.py --private-key 0x<wallet key> --days 30
```

The wallet key is used **only** to sign the registration locally. It is never
transmitted and never written to disk. The API key authorizes **trading only**,
not withdrawals.

### Funding

Web app **Testnet Deposit** button credits ~$1,000 USDG — enough for everything
here. For more, or programmatically:

```bash
.venv/bin/python tools/fund_testnet.py --private-key 0x... --amount 5000
```

This does mint → approve → `initiateDeposit` on Robinhood Chain testnet. You
need a little RH-testnet ETH for gas and there is no public faucet.
**The contract addresses change on every testnet redeploy** — verify them
against <https://docs.arcus.xyz/guides/fund-testnet-account> first.

---

## 3. Running

```bash
# Offline, deterministic, no keys needed
.venv/bin/python -m arcusbot selftest --venue sim

# Find a spread that actually clears fees (4 seeds per spread)
.venv/bin/python -m arcusbot sweep --sweep-spreads 6,10,16,24 --sweep-seeds 4

# Live testnet, small and time-boxed, dashboard on :8080
.venv/bin/python -m arcusbot run --mode live \
  --markets BTC-USD,ETH-USD --notional 25 --spread-bps 10 \
  --duration 900 --port 8080
```

Stop with `Ctrl-C`: the bot cancels all orders, flattens with reduce-only IOCs,
writes the report, and exits. Exit code `0` means net PnL ≥ 0.

### Recommended first live session

```
BOT_MODE=live
BOT_MARKETS=BTC-USD
BOT_ORDER_NOTIONAL_USD=25
BOT_MAX_POSITION_NOTIONAL_USD=100
BOT_SPREAD_BPS=10
BOT_MAX_RUNTIME_S=900
RISK_MAX_DRAWDOWN_USD=10
RISK_MIN_FREE_COLLATERAL_USD=100
BOT_METRICS_PORT=8080
```

One market, small clips, 15 minutes, a $10 kill switch. Read the report, then
widen. Scale **notional** before you scale **markets** — more markets multiplies
inventory risk, not just volume.

---

## 4. Configuration reference

### Identity
| Variable | Default | Notes |
| --- | --- | --- |
| `ARCUS_NETWORK` | `testnet` | `mainnet` + `live` requires the gate (section 8) |
| `ARCUS_VENUE` | `arcus` | `sim` = offline paper exchange |
| `ARCUS_ADDRESS` | — | master wallet, `0x…` (42 chars) |
| `ARCUS_API_SECRET` | — | Ed25519 signing key, 64 hex chars |
| `ARCUS_ACCOUNT_INDEX` | `0` | subaccount; each has its own rate-limit pools |

### Behaviour
| Variable | Default | Notes |
| --- | --- | --- |
| `BOT_MODE` | `dry-run` | `live` signs and sends |
| `BOT_STRATEGY` | `volume-maker` | or `ping-pong`, `spot-rfq` |
| `BOT_MARKETS` | `BTC-USD,ETH-USD` | quoted markets |
| `BOT_ORDER_NOTIONAL_USD` | `25` | per clip; engine floor is $5 |
| `BOT_QUOTE_LEVELS` | `1` | ladder depth per side |
| `BOT_LEVEL_STEP_BPS` | `4` | spacing between ladder levels |
| `BOT_LEVERAGE` | `3` | set per market at startup |

### Capital management
Set a budget and let the bot derive every sizing knob from it — full detail in
[`CAPITAL.md`](CAPITAL.md).

| Variable | Default | Notes |
| --- | --- | --- |
| `BOT_CAPITAL_USD` | `0` | absolute budget; `0` = fixed sizing |
| `BOT_CAPITAL_PCT` | `0` | budget as % of equity; lower of the two wins |
| `BOT_RESERVE_USD` | `0` | equity never touched; also the collateral floor |
| `BOT_CAPITAL_UTILISATION` | `0.5` | share of available leverage deployed |
| `BOT_CAPITAL_CLIPS` | `3` | clips per market inside the position cap |
| `BOT_CAPITAL_INVENTORY_FRACTION` | `0.5` | soft cap ÷ hard cap |
| `BOT_CAPITAL_RESIZE_PCT` | `20` | equity drift before re-sizing (`0` = never) |
| `RISK_MAX_DRAWDOWN_PCT` | `0` | loss limit as % of deployed capital |

```bash
python -m arcusbot preflight --capital-pct 30 --reserve 150   # preview the plan
```

### Referral
| Variable | Default | Notes |
| --- | --- | --- |
| `ARCUS_REFERRAL_TESTNET` | `ARCUS` | code for `testnet.arcus.xyz/ref/<code>` |
| `ARCUS_REFERRAL_MAINNET` | `AIAGENT` | code for `app.arcus.xyz/ref/<code>` |
| `BOT_SHOW_REFERRAL` | `true` | `false` hides the banner everywhere |

Referral attribution happens at **signup**, in a browser — it never touches an
order. Replace the codes with your own if you fork this repo.

### Edge and quoting
| Variable | Default | Notes |
| --- | --- | --- |
| `BOT_SPREAD_BPS` | `10` | target round-trip edge |
| `BOT_FEE_BUFFER_BPS` | `1` | required margin over fees |
| `BOT_VOL_EDGE_MULTIPLIER` | `1.5` | widen per bps of realised volatility |
| `BOT_INVENTORY_SKEW_BPS` | `8` | max quote shift at full inventory |
| `BOT_JOIN_BBO` | `true` | improve the touch when the book is wide enough |
| `BOT_REQUOTE_BPS` | `2` | drift that triggers a re-quote |
| `BOT_REQUOTE_INTERVAL_S` | `1.5` | minimum time between re-quotes |
| `BOT_MAX_QUOTE_AGE_S` | `20` | re-quote a stale resting order |
| `BOT_INVENTORY_MAX_AGE_S` | `90` | when to cross the spread to get flat |
| `BOT_TAKER_SLIPPAGE_BPS` | `25` | protective bound on IOC closes (clamped <10%) |

### Risk
| Variable | Default | Notes |
| --- | --- | --- |
| `BOT_MAX_POSITION_NOTIONAL_USD` | `150` | hard per-market cap |
| `BOT_MAX_INVENTORY_NOTIONAL_USD` | `75` | soft cap → flatten-only |
| `RISK_MAX_DRAWDOWN_USD` | `25` | kill switch (70% → flatten-only) |
| `RISK_MAX_DAILY_LOSS_USD` | `40` | kill switch |
| `RISK_MIN_FREE_COLLATERAL_USD` | `50` | below → flatten-only |
| `RISK_MAX_OPEN_ORDERS` | `12` | per market |
| `RISK_MAX_ORDERS_PER_MIN` | `90` | self-pacing |
| `RISK_MAX_CONSECUTIVE_ERRORS` | `12` | kill switch |
| `RISK_STALE_PRICE_S` | `20` | refuse to quote on stale data |
| `RISK_CANCEL_ALL_ON_EXIT` | `true` | leave nothing resting |
| `RISK_DEAD_MANS_SWITCH_S` | `0` | arm the venue-side switch (0 = off) |

### State across restarts
See [`PERSISTENCE.md`](PERSISTENCE.md). Without this a crash-looping bot resets
its own kill switch on every restart and can lose its whole daily limit
repeatedly while each session reports itself healthy.

| Variable | Default | Notes |
| --- | --- | --- |
| `BOT_PERSIST_STATE` | `true` | write `state/session.json`; `false` for CI/backtests |
| `RISK_CARRY_DRAWDOWN` | `true` | measure drawdown from the all-time peak |
| `RISK_MAX_RESTART_CRASHES` | `0` | `>0` refuses to start after N unclean exits |

```bash
python -m arcusbot history      # lifetime totals, today's loss, recent runs
```

### Session limits
`BOT_VOLUME_TARGET_USD` (stop at volume), `BOT_MAX_RUNTIME_S` (stop at time),
both `0` = unlimited. **Always set at least one for an unattended run.**

---

## 5. Monitoring

`--port 8080` gives a live dashboard, `/api/status`, `/api/report`, `/healthz`.

Artifacts:
- `state/status.json` — live snapshot, rewritten each report tick
- `state/fills.jsonl` — append-only journal, one JSON fill per line
- `state/report-<session>.json`, `state/report-latest.json`
- `logs/bot-YYYYMMDD.log`

### What "healthy" looks like

| Metric | Healthy | If not |
| --- | --- | --- |
| `netBpsOfVolume` | ≥ 0 | widen `BOT_SPREAD_BPS`; check maker share |
| `feeCoverageRatio` | ≥ 1 | the volume is not paying for itself |
| `makerShare` | high (80%+) | too many taker closes → raise `BOT_INVENTORY_MAX_AGE_S` |
| `risk.state` | `OK` | read `risk.reasons` |
| `ws.connected` | `true` | check `reconnects` |
| `ipBudget.tokens` | well above 0 | you are REST-polling too hard |
| `rejects` | flat | see below |

A steady trickle of `POST_ONLY_WOULD_CROSS` is **normal and free** — it is an
ALO refusing to pay taker fees. Volume climbing while `netPnl` falls is the one
pattern that means *stop*: you are buying volume with equity.

### Quick queries

```bash
curl -s localhost:8080/api/status | python3 -m json.tool | head -40
python -m arcusbot report
# realised edge per fill
python3 -c "import json;[print(json.loads(l)['market'],json.loads(l)['liquidity'],json.loads(l)['fee']) for l in open('state/fills.jsonl')]"
```

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `401 Unauthorized` | timestamp not nanoseconds, or >30s clock drift | sync NTP; check `X-Timestamp` |
| `invalid order signature` | payload/body mismatch, or `X-Signature` missing on a batch | see §4.2 of `llms.txt`; batches still need the header |
| `403` | address doesn't match the key, or wrong `accountIndex` | check `ARCUS_ADDRESS`/`ARCUS_ACCOUNT_INDEX` |
| `404` on `/v1/account` | account has no activity yet | fund it — this is the expected empty state |
| `errorType: Tick` | price/size off the grid, or a coarser tick tier | `snap_price` handles tiers; check `markets` output |
| `goodTilTime is required` | missing/too near — needed even on IOC/FOK | must be ≥ ~1 month out |
| `InvalidRequest` on a small order | below the $5 notional floor | raise `BOT_ORDER_NOTIONAL_USD` |
| `MarketPriceSlippageToleranceTooHigh` | protective price >10% from mark | lower `BOT_TAKER_SLIPPAGE_BPS` |
| `UNDERCOLLATERALIZED` | not enough free collateral, or off-hours margin uplift | reduce size/leverage; RWA markets need more margin off-hours |
| `429` with `reason: ip` | read polling drained the weight bucket | order writes are free — it's your reads |
| `429` with `reason: account_empty` | subaccount pool exhausted, on the drip | slow the order loop, or use another subaccount |
| `REDUCE_ONLY_WOULD_INCREASE` | position already closed elsewhere | benign; positions resync next loop |
| Orders still resting after a crash | **no cancel-on-disconnect** on Arcus | `cancelAllOrders`, or arm `RISK_DEAD_MANS_SWITCH_S` |
| Bot won't run live on mainnet | deliberate guard | this is a testnet harness |

### Emergency flatten

```bash
python -m arcusbot run --mode live --duration 30 --volume-target 0.01
```

Startup cancels nothing on its own, but shutdown always runs
cancel-all → reduce-only IOC flatten. Or cancel from the web app.

---

## 6b. The $1B VIP milestone

Both networks advertise "Trade $1B volume to unlock VIP". The bot tracks
progress and, more usefully, prints the honest arithmetic every run:

```
VIP progress: $234.28 of $1000000000 (0.000023%) — 1079.5 days at the current
rate, costing ~$694297.25 in net PnL
```

Read that second clause carefully. At a negative edge, $1B of volume has a
price tag in the hundreds of thousands of dollars. The milestone is only
rational to chase once `netBpsOfVolume` is **positive** — then the same line
reads "earning" instead of "costing", and volume becomes the goal rather than
the cost.

Practical implications:
- Get the edge positive **first**. Volume at a negative edge just buys a badge.
- The ETA scales with clip size, not with quoting faster. Raising
  `BOT_ORDER_NOTIONAL_USD` (or the capital budget) moves it; shaving
  `BOT_REQUOTE_INTERVAL_S` mostly burns rate limit.
- Fee tiers improve with 30-day volume, so the edge tends to widen as you go —
  at high tiers maker fees can turn into rebates.

Live values are in `GET /api/status` under `vip`, and on the dashboard.

## 7. Tuning for more volume without losing money

In order of preference:

1. **Raise `BOT_ORDER_NOTIONAL_USD`** (or the capital budget). Linear volume,
   unchanged edge per unit.
2. **Add markets.** Independent inventory; raise `RISK_MAX_OPEN_ORDERS` too.
3. **Lower `BOT_REQUOTE_INTERVAL_S`.** More fills, more rate-limit pressure.
4. **Raise `BOT_QUOTE_LEVELS`.** Ladder depth catches more sweeps.
5. **Tighten `BOT_SPREAD_BPS`** — *only* while `netBpsOfVolume` stays positive.
   This is the one that can quietly turn the bot into a fee donor.

Never tighten below the fee floor: the code refuses, and it is refusing for a
reason. Volume you paid for is not volume you earned.


---

## 8. Going live on mainnet with a $20 budget

> Read this whole section before setting a single variable. Rungs 1–9 of the
> ladder must be green first: if the bot is not profitable on testnet over
> multiple sessions, mainnet will only lose money faster.

### 8.1 What protects you

The $20 limit is enforced by the **risk engine**, not by this document. Five
conditions must all hold, they are AND-ed, and each is read straight from the
environment so that a *malformed* value is distinguishable from an unset one.
Anything unrecognised denies the run:

| Variable | Required value | Why |
| --- | --- | --- |
| `ARCUS_NETWORK` | `mainnet` | Selects the real venue |
| `BOT_MODE` | `live` | Dry-run on mainnet is always allowed and never gated |
| `BOT_MAINNET_ENABLED` | `true` | Explicit opt-in; `ture`, `y`, `1.0` all **deny** |
| `BOT_MAINNET_CAPITAL_USD` | e.g. `20` | The hard cap. Must be > 0 and <= the ceiling |
| `BOT_MAINNET_ACK` | `i-understand-the-risk` | Cannot be set by accident |

`BOT_MAINNET_MAX_CAPITAL_USD` (default **$100**) is a fat-finger ceiling: typing
`2000` instead of `20` is rejected outright rather than deployed.

When the gate opens the bot **tightens** its risk limits to fractions of the
cap and logs every change:

| Limit | Fraction of cap | On a $20 cap |
| --- | --- | --- |
| `max_drawdown_usd` | 15% | $3.00 |
| `max_daily_loss_usd` | 20% | $4.00 |
| `max_position_notional_usd` | 150% | $30.00 |
| `max_inventory_notional_usd` | 100% | $20.00 |

This matters: the testnet default drawdown is `$25`, which on a $20 account
would allow losing more than the entire balance before halting. A limit you set
*stricter* than these is always kept — the gate only ever tightens.

The cap is re-checked on **every** capital re-size, so equity growth cannot
quietly increase deployed capital.

### 8.2 The sequence

```bash
# 1. Confirm the gate refuses everything by default. This MUST fail.
ARCUS_NETWORK=mainnet BOT_MODE=live python -m arcusbot preflight
#    -> "refusing to run live on mainnet — BOT_MAINNET_ENABLED is not true; ..."

# 2. Fund the mainnet account with EXACTLY what you intend to risk ($20).
#    The gate caps what the bot deploys; it cannot protect capital you
#    voluntarily leave in the account.

# 3. Read-only checks against mainnet.
ARCUS_NETWORK=mainnet python -m arcusbot preflight
ARCUS_NETWORK=mainnet python -m arcusbot markets --rank --capital 20
#    -> a $20 account should select exactly ONE market.

# 4. Dry run against real mainnet data. Signs nothing, sends nothing.
ARCUS_NETWORK=mainnet python -m arcusbot run --mode dry-run --duration 600 --port 8080

# 5. Only now, open the gate. Put these in .env, not in your shell history.
cat >> .env <<'EOF'
ARCUS_NETWORK=mainnet
BOT_MAINNET_ENABLED=true
BOT_MAINNET_CAPITAL_USD=20
BOT_MAINNET_ACK=i-understand-the-risk
EOF

# 6. First live run: small, time-boxed, watched. Do not walk away.
python -m arcusbot run --mode live --capital 20 --duration 900 \
    --max-drawdown 3 --markets BTC-USD --port 8080
```

Startup logs you should see, and must read:

```
MAINNET LIVE — real funds. Hard capital cap $20.
mainnet risk floor: max_drawdown_usd $25 -> $3
mainnet risk floor: max_daily_loss_usd $40 -> $4
```

If you do not see those lines, you are not on the gated path — stop.

### 8.3 Monitoring

Open `http://localhost:8080`. The **Status** card is the whole point: it shows a
single verdict, worst-condition-first, so you can tell in seconds whether to
intervene.

| Verdict | Meaning | Action |
| --- | --- | --- |
| `PROFITABLE` | Net PnL positive after fees | Leave it alone |
| `BREAKEVEN` | Volume with no net loss | Acceptable; watch `netBpsOfVolume` |
| `LOSING` | Net PnL negative | Watch; the bot is already widening + slowing |
| `WARMING-UP` | No volume yet | Normal for the first minute |
| `RECONCILING` | Order(s) of unknown status | Bot is not opening new exposure — expected to clear |
| `RATE-LIMITED` | Backing off the API | Self-corrects; persistent = reduce markets |
| `OVEREXPOSED` | Inventory over cap | Bot is flattening; if it persists, halt |
| `STUCK` | No fills for 2+ minutes | Check spread vs required edge |
| `HALTED` | Kill switch fired | **Read the reason before restarting** |

### 8.4 Shutting down

`Ctrl-C` once. The sequence is ordered so nothing is left dangling:

1. Stop opening new exposure (risk halt)
2. Cancel all resting orders
3. Reduce-only flatten the inventory
4. **Confirm** the final account state against the exchange
5. Persist the reason and the durable risk state
6. Print the final report

Step 4 is the one that matters most: if a position could not be closed, the bot
prints `OPEN POSITIONS REMAIN: {...} — close these manually` and records it in
`status.json`. It never exits pretending to be flat.

Do **not** `kill -9`. That skips cancel and flatten, and the venue has no
cancel-on-disconnect — orders would stay live with nothing managing them.

### 8.5 After a halt

A restart does **not** hand back a spent loss budget: today's booked loss and
the drawdown high-water mark are persisted (`state/session.json`) and carried
forward. If the bot halted on the daily loss limit, restarting it will halt
again — that is deliberate. Investigate the cause; do not delete the state file
to get around it.
