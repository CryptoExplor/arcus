# State that survives restarts

## The problem

A bot that forgets its losses on restart has no risk limits at all.

Consider a `RISK_MAX_DRAWDOWN_USD=25` kill switch on a bot inside a
`systemd` unit with `Restart=always`. It loses $25, halts correctly, exits —
and is immediately restarted with a fresh baseline. It loses $25 again. Every
individual session reports itself as healthy and correctly halted; the account
bleeds indefinitely.

The same hole applies to the daily loss limit (reset on every restart), the
lifetime volume that determines your fee tier, and the VIP milestone.

## What is persisted

`state/session.json`, written atomically on every clean shutdown:

| Block | Contents | Used for |
| --- | --- | --- |
| `lifetime` | volume, maker volume, fees, rebates, realized PnL, fills, session count | fee-tier estimate, VIP progress, long-run edge |
| `day` | UTC-day volume and PnL | `RISK_MAX_DAILY_LOSS_USD` |
| `risk` | peak net PnL, peak equity, consecutive unclean exits | drawdown baseline, crash-loop breaker |
| `sessions` | last 50 runs with volume, net, bps and exit reason | `python -m arcusbot history` |

## What is deliberately **not** persisted

**Open positions.** The exchange is the authority on inventory, and the bot
re-reads positions at startup. Persisting them locally would risk a stored
belief contradicting the venue — the exact class of bug that makes a bot trade
against a position it does not actually have.

## Behaviour

```bash
BOT_PERSIST_STATE=true         # default; false for CI, sweeps, backtests
RISK_CARRY_DRAWDOWN=true       # measure drawdown from the all-time peak
RISK_MAX_RESTART_CRASHES=0     # >0 = circuit breaker for crash loops
```

### Daily loss carries over

```
carrying $38.05 of loss already booked today toward the $40 daily limit
```

With $38 already lost, only $2 of headroom remains. Past the limit, the bot
**refuses to start**:

```
KILL SWITCH: daily loss $41.10 >= $40 (includes $41.10 already lost today)
```

The counter resets automatically at UTC midnight — lifetime totals do not.

### Drawdown measures from the all-time peak

With `RISK_CARRY_DRAWDOWN=true` (default), a bot that was up $30 in a previous
session and starts flat is already $30 into its drawdown allowance. This is
usually what you want: drawdown is a property of the account, not of a process
lifetime. Set it to `false` to measure each session independently.

### Crash-loop breaker

An exit is *clean* if it was a runtime limit, a volume target, a signal, or a
cancellation. Anything else — an error storm, a kill switch, a killed process —
counts as unclean and increments the counter. A clean exit resets it.

With `RISK_MAX_RESTART_CRASHES=3`:

```
KILL SWITCH: 3 consecutive unclean exits >= RISK_MAX_RESTART_CRASHES (3);
last: 12 consecutive errors. Investigate before restarting, or clear
state/session.json.
```

Off by default (`0`), because it is only meaningful under a supervisor that
restarts automatically. Turn it on for unattended deployments.

## Inspecting it

```bash
python -m arcusbot history          # human readable
python -m arcusbot history --json   # machine readable
```

```
Lifetime (since 2026-09-10T11:49:32Z)
  sessions      3   fills 10
  volume        $279.12  (maker $139.59)
  net PnL       $-0.16  (-5.67 bps of volume)

Today (2026-09-10 UTC)
  loss so far   $0.16 of $40 limit

Risk carry-over
  peak net PnL  $0.0098
  unclean exits 0  last: max runtime 8s reached
```

Also exposed at `GET /api/status` under `session`, and on the dashboard's
**Lifetime** card.

## Durability

- **Atomic writes** — temp file plus `os.replace`. A half-written state file
  read on the next boot would be worse than none.
- **Corruption is survivable** — a malformed file is moved to
  `session.corrupt` and the bot starts fresh with a warning, rather than
  refusing to run.
- **Version-checked** — an unrecognised schema version starts fresh instead of
  misreading fields.
- **Bounded** — session history is capped at 50 entries.

## Resetting

```bash
rm state/session.json     # clears lifetime totals, daily loss and crash count
```

Do this deliberately: it also clears the loss history the kill switch relies on.

`selftest` and `sweep` set `persist_state=false` internally — a diagnostic must
never mutate the risk state that guards real trading.
