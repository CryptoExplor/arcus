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

---

## 2. Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in credentials
```

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
| `ARCUS_NETWORK` | `testnet` | `mainnet` + `live` is refused by design |
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

## 7. Tuning for more volume without losing money

In order of preference:

1. **Raise `BOT_ORDER_NOTIONAL_USD`.** Linear volume, unchanged edge per unit.
2. **Add markets.** Independent inventory; raise `RISK_MAX_OPEN_ORDERS` too.
3. **Lower `BOT_REQUOTE_INTERVAL_S`.** More fills, more rate-limit pressure.
4. **Raise `BOT_QUOTE_LEVELS`.** Ladder depth catches more sweeps.
5. **Tighten `BOT_SPREAD_BPS`** — *only* while `netBpsOfVolume` stays positive.
   This is the one that can quietly turn the bot into a fee donor.

Never tighten below the fee floor: the code refuses, and it is refusing for a
reason. Volume you paid for is not volume you earned.
