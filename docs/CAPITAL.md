# Capital management

How to tell the bot how much of your money it may use, and what it does with
that number.

---

## The two sizing modes

### Fixed (default)

You state the dollar amounts directly:

```bash
BOT_ORDER_NOTIONAL_USD=25          # per clip
BOT_MAX_POSITION_NOTIONAL_USD=150  # hard cap per market
BOT_MAX_INVENTORY_NOTIONAL_USD=75  # soft cap -> stop opening, keep closing
```

Predictable and reproducible — good for testing and for comparing runs. Its
weakness is that it ignores your balance: `$25` clips are cautious on a
$100,000 account and reckless on a $100 one, and the numbers go stale as the
account moves.

### Capital (opt-in)

Set **one** budget knob and everything else is derived:

```bash
BOT_CAPITAL_USD=500        # an absolute budget
# or
BOT_CAPITAL_PCT=25         # a share of equity
```

If you set both, the **lower** wins — a percentage is a growth rule, an
absolute is a hard ceiling, and honouring both is the safe reading.

---

## How a budget becomes order sizes

```
deployable      = min(budget, equity − reserve)
exposure_budget = deployable × leverage × utilisation
per_market      = exposure_budget ÷ number_of_markets
clip            = per_market ÷ BOT_CAPITAL_CLIPS
max_position    = per_market
max_inventory   = per_market × BOT_CAPITAL_INVENTORY_FRACTION
```

Worked example — $1,000 equity, `BOT_CAPITAL_PCT=30`, `BOT_RESERVE_USD=150`,
3× leverage, 2 markets:

| Step | Value |
| --- | --- |
| budget (30% of $1,000) | $300 |
| deployable (after $150 reserve) | $300 |
| exposure budget ($300 × 3 × 0.5) | $450 |
| per market ($450 ÷ 2) | $225 |
| clip ($225 ÷ 3) | **$75** |
| max position / market | **$225** |
| max inventory / market (×0.5) | **$112.50** |

Check any configuration before running it:

```bash
python -m arcusbot preflight --capital-pct 30 --reserve 150
```

---

## The knobs

| Variable | Default | What it does |
| --- | --- | --- |
| `BOT_CAPITAL_USD` | `0` | Absolute budget. `0` = capital mode off |
| `BOT_CAPITAL_PCT` | `0` | Budget as % of equity. `0` = off |
| `BOT_RESERVE_USD` | `0` | Equity the bot may never touch |
| `BOT_CAPITAL_UTILISATION` | `0.5` | Share of available leverage actually used |
| `BOT_CAPITAL_CLIPS` | `3` | Clips per market inside the position cap |
| `BOT_CAPITAL_INVENTORY_FRACTION` | `0.5` | Soft cap ÷ hard cap |
| `BOT_CAPITAL_RESIZE_PCT` | `20` | Equity drift before re-sizing (`0` = never) |
| `RISK_MAX_DRAWDOWN_PCT` | `0` | Loss limit as % of deployed capital |

CLI equivalents: `--capital`, `--capital-pct`, `--reserve`, `--leverage`.

### `BOT_CAPITAL_UTILISATION` — the one to understand

This is the deliberate gap between what the margin engine would *allow* and
what the bot actually deploys. At `1.0` you run at full available leverage, so
a single adverse move liquidates you. The `0.5` default trades some volume for
the ability to survive a bad hour. Raise it only if you have measured the
drawdown you actually experience.

### `BOT_RESERVE_USD` — a floor that is actually enforced

The reserve is subtracted from equity *before* the budget is computed, **and**
becomes the minimum-free-collateral floor. That second part is what makes it
real: the bot stops opening positions before it can eat into the reserve,
rather than merely starting out below it.

---

## Re-sizing as the account moves

In capital mode the bot re-derives its sizing when equity drifts past
`BOT_CAPITAL_RESIZE_PCT` (default 20%).

Why a threshold rather than every tick: re-sizing constantly makes position
caps jitter underneath the strategy, and caps that move while inventory is open
produce confusing flatten decisions. Never re-sizing is worse in the other
direction — an account that doubled keeps trading tiny, and one that halved
keeps trading too large. Set it to `0` to pin sizing for a session.

Each re-size is logged and counted in `status.json` as `capitalResizes`.

---

## Guard rails

The allocator refuses to produce an unusable plan.

- **Clip below the $5 venue minimum** → raised to $5 (fewer clips per market)
  with a warning. Better a few legal clips than many rejected ones.
- **Budget too small for even one $5 clip** → the kill switch fires at startup
  with an actionable message, instead of the bot failing on every order:
  ```
  KILL SWITCH: insufficient capital: $4 deployable across 2 market(s) at 3x
  cannot fund even one $5 clip. Increase BOT_CAPITAL_USD/PCT, lower
  BOT_RESERVE_USD, or trade fewer markets.
  ```
- **Reserve larger than equity** → deployable clamps to `0`, plan marked
  insufficient.
- **Equity unknown** (unfunded account, `/v1/account` 404) → falls back to the
  fixed notionals in mode `unfunded` and says so. It does not guess a balance.
- **Caps below one clip** → raised so `max_position ≥ clip`, keeping the config
  internally consistent for the risk manager.

Capital sizing produces the *inputs* to the risk manager; it never bypasses it.
Every derived number still passes the same per-order gate.

---

## Loss limits

```bash
RISK_MAX_DRAWDOWN_USD=25      # absolute
RISK_MAX_DRAWDOWN_PCT=5       # or 5% of deployed capital (capital mode)
```

`RISK_MAX_DRAWDOWN_PCT` overrides the USD figure when both are set and capital
mode is on, so the kill switch scales with the account instead of going stale.
Note it is a percentage of **deployed capital**, not total equity — the money
actually at risk.

---

## Account size drives how many markets you trade

Capital does not just set the clip size — it decides how many markets the bot
is *allowed* to touch. Spreading a small account across several markets is a
quiet way to lose: each market needs room for a few clips, otherwise every fill
is all-or-nothing and there is nothing left to average or scale out with.

`selection.max_markets_for_capital` enforces this:

| Deployable | Max markets | Why |
| --- | --- | --- |
| < $5 | 0 | Below the venue minimum notional — refuses to run |
| $20 | **1** | Room for ~4 clips in one market, none in two |
| $50 | 3 | |
| $100 | 6 | |
| $1,000+ | 8 | Capped: attention, subscriptions and rate-limit weight grow faster than the edge of the Nth-best market |

On top of the count, `markets --rank` scores each candidate on spread,
liquidity, volatility, affordability and tick granularity, and **vetoes** ones
that cannot work — offline, no mark price, spread below the required edge, tick
coarser than the edge, or a minimum clip that eats more than half the
per-market budget. Asking for a market with `--markets` does not override a
veto; it only narrows the universe.

```bash
python -m arcusbot markets --rank --capital 20
# deployable $20 -> at most 1 market(s)
# -> ETH-USD  0.406  ...
# selected: ETH-USD
```

---

## The $20 mainnet case

`BOT_MAINNET_CAPITAL_USD` is a **separate, harder** limit from `BOT_CAPITAL_USD`.
It is enforced by the risk engine on every allocation and re-size, and it
tightens the loss limits to fractions of itself (drawdown 15%, daily loss 20%).
See `docs/BOT_OPERATIONS.md` section 8. The general sizing knobs below still
apply — the mainnet cap simply puts a ceiling under all of them that config
cannot raise.

---

## Recommended starting points

**First live testnet run** — deliberately small:

```bash
BOT_CAPITAL_USD=100
BOT_RESERVE_USD=0
BOT_CAPITAL_UTILISATION=0.5
BOT_MARKETS=BTC-USD
RISK_MAX_DRAWDOWN_PCT=10
BOT_MAX_RUNTIME_S=900
```

**Scaling up**, once `netBpsOfVolume` has been positive across several runs:

```bash
BOT_CAPITAL_PCT=40
BOT_RESERVE_USD=200
BOT_CAPITAL_UTILISATION=0.6
BOT_MARKETS=BTC-USD,ETH-USD
RISK_MAX_DRAWDOWN_PCT=8
```

Scale the **budget** before the utilisation, and the utilisation before the
leverage. Each step increases risk faster than the last.

---

## Monitoring

`GET /api/status` carries a `capital` block, and the dashboard has a **Capital**
card showing deployed amount, reserve, clip, position cap, loss limit and
re-size count. The end-of-run summary prints the final plan.

```bash
curl -s localhost:8080/api/status | python3 -c \
  "import json,sys; print(json.dumps(json.load(sys.stdin)['capital'], indent=2))"
```
