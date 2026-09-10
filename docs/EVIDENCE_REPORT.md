# Testnet validation — evidence report

**Date:** 2026-09-10
**Phase:** controlled testnet validation (perpetuals only)
**Outcome:** **BLOCKED — validation could not be executed. Mainnet remains NO.**

---

## 1. Headline

The validation phase you asked for **did not run**, and no substitute for it was
invented. The environment this repository is built in cannot reach the Arcus
API, so there are **zero real testnet fills** to report.

Every quantity that follows is either (a) infrastructure evidence about the
blocker, or (b) explicitly-labelled simulator output that is **not** evidence of
profitability. The requested table of session results is empty because the
sessions did not happen.

| Requested figure | Value |
| --- | --- |
| Total runtime (live testnet) | **0 s** |
| Executed volume | **$0** |
| Fills | **0** |
| Gross PnL | **n/a** |
| Fees | **n/a** |
| Net PnL | **n/a** |
| `netBpsOfVolume` | **n/a** |
| Maker/taker ratio | **n/a** |
| Max drawdown | **n/a** |
| Max inventory | **n/a** |
| Reconciliation discrepancies | **n/a** |
| Regimes covered (A–K) | **0 of 11** |
| Evidence sufficient for a $20 mainnet experiment? | **NO** |

---

## 2. The blocker, and how it was established

`api.testnet.arcus.xyz` resolves and accepts a TCP connection, then closes the
connection during the TLS handshake:

```
$ getent hosts api.testnet.arcus.xyz
104.18.12.22    api.testnet.arcus.xyz                  # DNS works

$ nc -vz api.testnet.arcus.xyz 443
Connection to api.testnet.arcus.xyz 443 port [tcp/https] succeeded!   # TCP works

$ openssl s_client -connect api.testnet.arcus.xyz:443
CONNECTED(00000003)
SSL handshake has read 0 bytes and written 334 bytes                  # TLS dies
error: unexpected eof while reading
```

The server reads the ClientHello and sends back nothing at all — not a TLS
alert, not a certificate.

Ruling out the obvious alternatives:

| Hypothesis | Test | Result |
| --- | --- | --- |
| Broken DNS | `getent hosts` | resolves (A + AAAA) |
| IPv6 path failure | `curl -4` to 104.18.12.22 | fails identically |
| Proxy misconfiguration | `env \| grep -i proxy` | no proxy variables set |
| Broken local TLS stack | `curl https://github.com` | **HTTP 200** |
| Arcus is down | — | other Cloudflare hosts fail the same way |

Egress comparison across hosts:

| Host | HTTP status |
| --- | --- |
| `github.com` | **200** |
| `cloudflare.com` | 000 |
| `docs.arcus.xyz` | 000 |
| `api.testnet.arcus.xyz` | 000 |
| `app.arcus.xyz` | 000 |

**Conclusion:** this sandbox has an egress allowlist that permits GitHub and
blocks Cloudflare-fronted hosts, including all Arcus endpoints. This is an
environment restriction, not a bug in the bot. It cannot be worked around from
inside the sandbox, and I did not try to disguise it.

---

## 3. What I did instead

Rather than fabricate results, I built the thing that was actually missing: the
apparatus that will make the validation phase *conclusive* when it runs, and
that makes it hard to fool yourself about the outcome.

### 3.1 The measurement gap I found

Auditing the existing reports against your 22 required metrics, the bot recorded
about 13 of them. **Four were missing entirely** — `fill_rate`, `avg_inventory`,
`ws_disconnects`, `flatten_success`. Notably, two of those four are exactly the
metrics that reveal the failure modes you care about most: inventory that never
gets worked off, and a flatten that silently did not complete.

### 3.2 `arcusbot/evidence.py`

A new module recording all 22 metrics per session to an append-only
`state/evidence.jsonl`, plus an aggregate that renders a verdict. Its design
rules are deliberately adversarial toward optimistic reporting:

* **Net PnL is recomputed, never trusted.** `realized + unrealized + funding +
  rebates − fees` is calculated independently of whatever the engine reported,
  so an accounting bug shows up as a mismatch rather than as profit.
* **Unrealized profit is quarantined.** A session whose net is positive only
  because of open marks is flagged `unrealizedDependent`, warned about, and
  blocked from counting. This directly implements your "never report profitable
  on unrealized PnL alone".
* **No claim below 5 sessions and 400 fills.** Smaller samples return
  `INSUFFICIENT-DATA`, not a number with a hopeful sign.
* **A t-statistic ≥ 2.0 is required.** The mean edge must sit two standard
  errors above zero. The previous best simulator result, `+0.82 bps ±2.06`,
  fails this test — which is the entire reason the threshold exists.
* **Fee coverage must exceed 1.** `(gross + rebates) / fees > 1`. Being bailed
  out by funding is not a market-making edge.
* **Operational blockers veto everything.** Residual positions, a failed
  flatten, or any reconciliation discrepancy in *any* session blocks readiness
  regardless of how good the PnL looks.

`ready_for_mainnet()` is true only for `POSITIVE-EDGE` **and** zero blockers.

### 3.3 A real bug this surfaced

Wiring the harness exposed a live defect: `pnl.py` emits `makerShare` as a
**percentage (0–100)**, while every consumer treated it as a 0–1 fraction. The
first aggregate run reported a **maker share of 8,685.7%**. Fixed, clamped, and
regression-tested — this would have silently corrupted the maker/taker ratio in
the very report you asked for.

### 3.4 Tests

`tests/test_evidence.py` — 23 tests covering the PnL identity, the
unrealized-dependence trap, refusal to rule on small samples, rejection of a
noisy positive mean, fee-coverage failure, each operational blocker, JSONL round
trips including corrupt-line tolerance, and the `makerShare` regression.

Full suite: **324 tests passing.**

---

## 4. Simulator status — explicitly NOT evidence

Listed only so nothing is hidden, and because the sign is unflattering.

A 20-minute simulated session (BTC-USD + ETH-USD, 187 fills, 95.4% maker) ended
at **−0.83 bps of volume**, gross $0.42 against $0.85 of fees — fee coverage
**0.49x**. Two short tagged sessions through the new harness returned **−0.39
bps** and **−0.40 bps**, fee coverage ~0.79x.

The pattern is consistent and worth stating plainly: **the simulated gross edge
does not cover the simulated fees.** Earlier sweeps that looked positive
(`+0.82 bps ±2.06` at a 16 bps spread) were noise around zero, and the more
fills accumulate, the more the result converges toward the fee drag.

Against the published Base-tier round-trip costs — maker/maker **3 bps**,
maker/taker **6 bps**, taker/taker **9 bps** — this is the expected outcome for
a strategy without a demonstrated informational or queue-position advantage.

I did not tune the simulator to change these numbers.

These files are named to prevent later confusion:
`state/evidence-simulator-not-testnet.jsonl`.

---

## 5. Risk-sizing correction

Your point about `$20 capital / $30 position` was correct and is now fixed in
code, not just prose. The mainnet risk floor was:

| Limit | Was | Now | On a $20 budget |
| --- | --- | --- | --- |
| `max_drawdown_usd` | 15% | **5%** | **$1.00** |
| `max_daily_loss_usd` | 20% | **10%** | **$2.00** |
| `max_position_notional_usd` | 150% | **75%** | **$15.00** |
| `max_inventory_notional_usd` | 100% | **75%** | **$15.00** |

Position notional is now capped **at or below** account equity, so leverage
cannot turn a "$20 experiment" into a $30 exposure. `docs/CAPITAL.md` documents
the five distinct quantities — account equity, margin allocated, position
notional, liquidation exposure, and maximum loss — with the worked example
showing how a $30 notional at 3x loses $3 (15% of a $20 account) on a 10% move.

Two new tests assert notional never exceeds the cap and loss limits stay a small
fraction of capital.

---

## 6. Spot

Unchanged and unstarted, per your instruction. Arcus spot is RFQ-based and
requires an EIP-712 wallet signature per trade; the bot currently produces
*intents* only. It is not automated, and the README no longer implies otherwise.
Spot stays parked until perps are proven.

---

## 7. Conclusions

1. **Validation is blocked, not skipped.** Network egress prevents any real
   testnet execution from this environment. It must be run from a machine that
   can reach `api.testnet.arcus.xyz`.
2. **No trading edge has been demonstrated.** Zero live fills. The simulator —
   which does not count as evidence — currently shows a *negative* net edge
   after fees, so the honest prior going into validation is skeptical.
3. **The bot is not ready for mainnet, and I am not recommending the $20
   experiment.** Not because the risk controls are weak, but because there is no
   evidence the strategy is profitable, and risking money on an unmeasured edge
   is exactly the mistake the controls exist to prevent.
4. **The apparatus is ready.** Metrics, regime tagging, statistical gate, and a
   CI-usable exit code are built and tested, so the validation run will produce
   a defensible answer rather than an anecdote.
5. **The most likely finding, when it does run, is that fees dominate.** If so,
   the diagnostic table in `docs/VALIDATION.md` §7 is the place to start, and
   the response should be a strategy change tested separately — not an
   adjustment to how PnL is counted.

**Is the evidence sufficient for a $20 mainnet experiment? No.**
Not "not yet, but probably" — there is simply no evidence at all.

---

## 8. To resume

On a machine with network access:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.testnet.arcus.xyz/health   # need 200
python -m arcusbot preflight --wallet t1
python -m arcusbot run --wallet t1 --mode live --markets BTC-USD \
       --duration 1800 --regime low-vol
python -m arcusbot evidence          # exit 0 only if the data supports proceeding
```

Then work the A–K matrix in `docs/VALIDATION.md` §5 and re-read the verdict.
**Stop here until that data exists.**
