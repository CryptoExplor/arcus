# arcus — automated trading bot

An automated market-making bot for the [Arcus](https://docs.arcus.xyz) exchange
(perpetuals + Stock Token spot). It quotes, executes, monitors, and accounts for
its own PnL, with one objective:

> **generate as much volume as possible while net PnL after fees stays ≥ 0.**

Runs on **testnet** by default. Mainnet is supported but **gated**: live trading
with real funds requires five explicit opt-ins and is hard-capped by the risk
engine, not by documentation ([details](docs/BOT_OPERATIONS.md#8-going-live-on-mainnet-with-a-20-budget)).

Python 3.11+, stdlib-only except `cryptography` and `websockets`.
256 tests, no network required.

---

## New here? Start with this

No API key, no wallet, no money, no internet needed — this runs against a
built-in paper exchange:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m arcusbot selftest --venue sim          # ~40s, prints SELFTEST OK
.venv/bin/python -m arcusbot run --venue sim --mode dry-run --duration 120 --port 8080
```

Then open <http://localhost:8080> and watch it quote. The big word at the top
(`PROFITABLE` / `LOSING` / `WARMING-UP` / `HALTED`) is the whole status in one
glance.

**The four safety levels** — understand these before anything else:

| | Command | Network | Can lose money? |
| --- | --- | --- | --- |
| 1 | `--venue sim` | none | **No** — fake exchange on your laptop |
| 2 | `--venue arcus --mode dry-run` | read-only | **No** — prints orders, sends nothing |
| 3 | `--mode live` on testnet | yes | No — testnet funds are worthless |
| 4 | `--mode live` on **mainnet** | yes | **YES — real money**, and gated |

Full beginner walkthrough: [`docs/BOT_OPERATIONS.md` §1b](docs/BOT_OPERATIONS.md).

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. Offline: prove the machinery works. No keys, no network, deterministic.
.venv/bin/python -m arcusbot selftest --venue sim

# 2. Offline: find a spread that clears fees (multi-seed measurement).
.venv/bin/python -m arcusbot sweep --sweep-spreads 6,10,16,24 --sweep-seeds 4

# 3. Credentials: https://testnet.arcus.xyz/api-keys, or:
cp .env.example .env
.venv/bin/pip install -r requirements-onboard.txt
.venv/bin/python tools/onboard.py --private-key 0x<wallet key>

# 4. Live testnet data, read-only.
.venv/bin/python -m arcusbot preflight
.venv/bin/python -m arcusbot quote

# 5. Real orders, small and time-boxed.
.venv/bin/python -m arcusbot run --mode live --duration 900 --notional 25 --port 8080
```

Mainnet is a separate, deliberate step — see
[the runbook](docs/BOT_OPERATIONS.md#8-going-live-on-mainnet-with-a-20-budget).
The short version: `ARCUS_NETWORK=mainnet`, `BOT_MODE=live`,
`BOT_MAINNET_ENABLED=true`, `BOT_MAINNET_CAPITAL_USD=20` and
`BOT_MAINNET_ACK=i-understand-the-risk` must **all** be set. Anything missing or
malformed refuses the run and says exactly what is wrong.

Fund the account with the **Testnet Deposit** button in the web app (~$1,000
USDG), or `tools/fund_testnet.py` for more.

---

## Capital management

Tell the bot how much of your balance it may use, and it derives every sizing
knob from that one number:

```bash
BOT_CAPITAL_PCT=30      # deploy 30% of equity …
BOT_RESERVE_USD=150     # … but never touch this much
```

```
deployable      = min(budget, equity − reserve)
exposure_budget = deployable × leverage × utilisation   (utilisation 0.5 by default)
per_market      = exposure_budget ÷ markets
clip            = per_market ÷ BOT_CAPITAL_CLIPS
```

Preview the plan before committing to it:

```bash
python -m arcusbot preflight --capital-pct 30 --reserve 150
#  [PASS] sizing     capital — equity $1000, deploying $300 (30%), reserve $150
#                    | exposure budget $450 across 2 market(s) -> clip $75,
#                    max position $225/market
#  [PASS] lossLimit  kill switch at $25 drawdown (2.5% of equity)
```

The reserve is enforced as the free-collateral floor, not just subtracted up
front, so the bot stops opening before it can eat into it. Sizing re-derives
itself when equity drifts 20% (`BOT_CAPITAL_RESIZE_PCT`), and a budget too
small for one $5 clip refuses to start with an actionable message rather than
failing on every order. Leave the capital knobs at `0` to size by hand instead.
Details in [`docs/CAPITAL.md`](docs/CAPITAL.md).

## Multiple wallets

Define named profiles in `.env` and switch with `--wallet`:

```bash
ARCUS_ADDRESS_T1=0x...  ARCUS_API_SECRET_T1=<64 hex>   # testnet wallet 1
ARCUS_ADDRESS_T2=0x...  ARCUS_API_SECRET_T2=<64 hex>   # testnet wallet 2
ARCUS_ADDRESS_M1=0x...  ARCUS_API_SECRET_M1=<64 hex>   # mainnet
ARCUS_NETWORK_M1=mainnet
```

```bash
python -m arcusbot wallets                    # list them, secrets redacted
python -m arcusbot run --wallet t1 --mode live
```

The network follows the profile name (`t*` testnet, `m*` mainnet) and switches
the API hosts with it. Selecting a mainnet wallet still does **not** bypass the
mainnet gate.

> **Wallet private keys** (`ARCUS_PRIVATE_KEY_*`) are optional and **not needed
> to trade**. The API signing key authorises trading only; a wallet key can
> withdraw your funds. If stored, it is never loaded during trading, a mainnet
> one needs `ARCUS_ALLOW_MAINNET_PRIVATE_KEY=true`, and it is redacted from all
> output. Prefer passing it once to `tools/onboard.py --private-key`.

---

## Commands

| Command | Network | Sends orders | Purpose |
| --- | --- | --- | --- |
| `selftest` | none | simulator | End-to-end run with hard assertions on the accounting |
| `sweep` | none | simulator | Net bps across spreads × seeds, with stdev |
| `preflight` | read-only | no | Credentials, markets, fees, balance, sizing |
| `markets` | read-only | no | Tick/step/notional grid |
| `quote` | read-only | no | Exactly what it would post right now |
| `run --mode dry-run` | read-only | no | Full loop, logs intents |
| `run --mode live` | yes | **yes** | The real thing |
| `report` | none | no | Reprint the last session's PnL |
| `history` | none | no | Lifetime totals, today's loss, recent sessions |

Flags: `--venue sim|arcus`, `--markets`, `--notional`, `--spread-bps`,
`--duration`, `--volume-target`, `--port`, `--json`.

---

## How it stays profitable

A round trip only makes money if the captured spread beats the fees it pays:

```
net_edge_bps = captured_spread − fee(open) − fee(close)
```

At the published Base tier of 1.5 bps maker / 4.5 bps taker, a maker/maker cycle
needs **> 3 bps** and a maker/taker cycle needs **> 6 bps**. So the bot:

- quotes **ALO (post-only)** on both sides — it is structurally incapable of
  accidentally paying a taker fee;
- computes a required edge from **live fee tiers + realised volatility**, and
  refuses to quote any pair tighter than that — re-checking *after* tick
  snapping, because coarse ticks can silently eat 1–2 bps;
- keeps both sides live while holding inventory, with the reducing leg
  `reduce_only` and priced so the round trip books positive by construction;
- crosses the spread only when inventory is too big or too old — bounded, known
  fees beat unbounded directional risk;
- halts on drawdown, daily loss, error storms, or a volume target, and always
  exits via cancel-all → reduce-only flatten → report.

The headline metric is **`netBpsOfVolume`** (net PnL per bps of volume traded),
not dollars. Measured in-sim over 3 seeds: **+0.5 to +1.0 bps at a 10 bps spread
under balanced flow, ~92% maker**. Under near-pure adverse
selection it loses ~1.5–2.6 bps — which is the *correct* behaviour of a market
maker facing purely toxic flow, not a bug. Full methodology, the two simulator
modelling bugs that initially inverted this conclusion, and the tuning procedure
are in [`docs/STRATEGY.md`](docs/STRATEGY.md).

---

## State that survives restarts

A bot that forgets its losses on restart has no risk limits. Halt on a $25
drawdown, get restarted by `systemd`, lose $25 again — every session reports
itself healthy while the account bleeds.

So lifetime volume, today's loss and the drawdown baseline persist to
`state/session.json` and are reloaded on boot:

```
carrying $38.05 of loss already booked today toward the $40 daily limit
```

Past the limit the bot **refuses to start** rather than spending it twice. The
daily counter rolls at UTC midnight; lifetime totals do not. Open positions are
deliberately *not* persisted — the exchange is the authority on inventory.

```bash
python -m arcusbot history        # lifetime, today, recent sessions
```

`RISK_MAX_RESTART_CRASHES=3` adds a circuit breaker that refuses to start after
three unclean exits in a row. Details in
[`docs/PERSISTENCE.md`](docs/PERSISTENCE.md).

## Monitoring

`--port 8080` serves a live dashboard (PnL, volume, risk, capital, VIP
progress, per-market table) plus `/api/status`, `/api/report`, `/healthz`. On
disk: `state/status.json`, `state/fills.jsonl` (append-only),
`state/report-*.json`, `logs/bot-*.log`.

Watch `netBpsOfVolume` ≥ 0, `feeCoverageRatio` ≥ 1, high `makerShare`,
`risk.state == OK`. Volume climbing while net PnL falls means stop.

## The $1B VIP milestone

Both networks offer VIP status at $1B of volume, so the bot tracks it — and
prints the arithmetic that matters:

```
VIP progress: $234.28 of $1000000000 (0.000023%) — 1079.5 days at the current
rate, costing ~$694297.25 in net PnL
```

That "costing" clause is the point. Chasing $1B at a negative edge has a
six-figure price tag; the milestone is only worth pursuing once
`netBpsOfVolume` is positive, at which point the line reads "earning". Get the
edge right first, then scale clip size — quoting faster mostly burns rate limit.

## Referral

Signing up through these links credits this tool:

- testnet — <https://testnet.arcus.xyz/ref/ARCUS>
- mainnet — <https://app.arcus.xyz/ref/IN>

Attribution happens at **signup**, in a browser; it never touches an order or
affects execution. Forking? Put your own codes in `ARCUS_REFERRAL_TESTNET` /
`ARCUS_REFERRAL_MAINNET`, or set `BOT_SHOW_REFERRAL=false` to hide the banner.

---

## Layout

```
arcusbot/    signing scaling config capital mainnet selection adaptive referral
             session rest ws book pnl risk strategy sim sweep engine
             dashboard cli
tools/       onboard.py (API key registration), fund_testnet.py (on-chain deposit)
tests/       256 offline tests — signing, tick math, PnL identity, risk,
             capital sizing, execution/reconciliation, mainnet gate, market
             selection, adaptive control, persistence, fee tiers, .env parsing
docs/        BOT_OPERATIONS.md · ARCUS_PLATFORM.md · STRATEGY.md
             CAPITAL.md · PERSISTENCE.md · AUDIT.md
llms.txt     single-file agent guide to the bot and the venue
.env.example template for .env (which is gitignored)
```

## Documentation

| File | Contents |
| --- | --- |
| [`llms.txt`](llms.txt) | Agent-readable guide: objective, wire rules, architecture, invariants |
| [`docs/BOT_OPERATIONS.md`](docs/BOT_OPERATIONS.md) | Runbook: setup, config reference, monitoring, troubleshooting |
| [`docs/ARCUS_PLATFORM.md`](docs/ARCUS_PLATFORM.md) | Venue reference: auth schemes, order rules, rate limits, channels |
| [`docs/STRATEGY.md`](docs/STRATEGY.md) | The economics, the failure modes, and how the edge was measured |
| [`docs/CAPITAL.md`](docs/CAPITAL.md) | Budget → order sizing, reserves, re-sizing, guard rails |
| [`docs/PERSISTENCE.md`](docs/PERSISTENCE.md) | State across restarts, loss carry-over, crash-loop breaker |
| [`docs/AUDIT.md`](docs/AUDIT.md) | Pre-mainnet audit: defects found, impact, and how each was fixed |

---

## Safety

- **Mainnet live is gated, not open.** Five conditions must all hold
  (`ARCUS_NETWORK`, `BOT_MODE`, `BOT_MAINNET_ENABLED`, `BOT_MAINNET_CAPITAL_USD`,
  `BOT_MAINNET_ACK`); a malformed value denies rather than defaults. The capital
  cap is enforced by the risk engine on every re-size, and it tightens the loss
  limits to fractions of itself. A `$100` ceiling catches fat-fingered amounts.
- **An order of unknown status is never blind-retried.** A timeout or 5xx after
  sending marks the order indeterminate, blocks new exposure (never closes), and
  reconciles against the exchange's own open-order list before continuing.
- **The bot never increases its trading frequency while losing money.**
- API keys authorize **trading only**, never withdrawals.
- Wallet private keys are used only locally by `tools/`, never transmitted or
  stored.
- Arcus has **no cancel-on-disconnect** — a crashed bot leaves orders resting.
  Set `RISK_DEAD_MANS_SWITCH_S` for unattended runs.
- Testnet contract addresses change on every redeploy; verify before funding.

Run `.venv/bin/python -m pytest tests -q` (256 tests, no network) after any change.
