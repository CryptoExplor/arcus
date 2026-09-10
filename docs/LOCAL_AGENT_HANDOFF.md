# Handoff to a local agent — testnet validation

You are running this bot on a laptop that **can** reach the Arcus API. The
environment it was built in could not, so no real testnet session has ever run.
Your job is to produce the first real evidence — including the possibility that
the evidence is bad.

Read [`docs/VALIDATION.md`](VALIDATION.md) for the full protocol. This page is
the short operational path.

---

## ⚠️ The mistake that ruins this exercise

`.env.example` ships with **`ARCUS_VENUE=sim`**.

With that setting the bot runs an **offline simulator** and never contacts
Arcus — *even with `--mode live --network testnet`*. The logs still say
`mode=live`, fills appear, PnL accrues, and a report is written. All of it is
synthetic.

```
# THIS DOES NOT TOUCH ARCUS if ARCUS_VENUE=sim
python -m arcusbot run --network testnet --mode live --duration 900
```

Two safeguards now exist, but **check the setting yourself**:

1. The engine prints a loud banner when `venue=sim`.
2. `arcusbot evidence` refuses to count simulated sessions and lists them as a
   blocker — perfect sim numbers can never produce `POSITIVE-EDGE`.

**Required before any real session:**

```bash
grep '^ARCUS_VENUE' .env        # must be: ARCUS_VENUE=arcus
```

Or pass `--venue arcus` explicitly on every run.

---

## Step 1 — verify the build

```bash
git pull
python -m pytest tests -q                 # expect 330 passed
python -m arcusbot selftest --venue sim   # expect SELFTEST OK
```

`selftest` is *supposed* to use `sim` — it's a wiring check, not evidence.

## Step 2 — credentials, testnet only

Fill in `.env`:

```ini
ARCUS_VENUE=arcus            # <-- the important one
ARCUS_NETWORK=testnet
ARCUS_ADDRESS_T1=0x...
ARCUS_API_SECRET_T1=...      # Ed25519 private key hex
ARCUS_NETWORK_T1=testnet
```

**Do not put mainnet credentials on this machine yet.** No mainnet private key,
no mainnet API secret. There is nothing to gain from having them present during
a phase that must not touch mainnet.

Trading needs only the API secret. A wallet private key is required *only* to
create an API key via `tools/onboard.py` — or not at all if you create the key
in the web UI.

## Step 3 — confirm you can actually reach Arcus

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.testnet.arcus.xyz/health
# expect 200. If you get 000, you have the same egress problem the sandbox had.

python -m arcusbot markets --venue arcus
python -m arcusbot preflight --wallet t1 --venue arcus
```

`preflight` must report `gateway ... reachable` and `READY`. If the `venue`
check says "offline simulator", stop — you are still on `sim`.

Fund the testnet account before trading; `GET /v1/account` 404s until funded.
The web app has a **Testnet Deposit** button, which is easier than the
mint/approve/`initiateDeposit` path.

## Step 4 — one market, short sessions, in order

Do **not** start with a 24-hour run or with multiple markets.

### Session 1 — mechanics (~15 min)

```bash
python -m arcusbot run --venue arcus --wallet t1 --mode live \
    --markets BTC-USD --duration 900 --regime normal-vol
```

Verifying: orders place and cancel, fills are ingested, PnL moves sensibly,
reconciliation is clean, and the bot **confirms flat** at exit. Ignore the PnL
number — 15 minutes proves nothing economically.

### Session 2 — inventory and fees (~30–60 min)

Same command, `--duration 3600`. Now watch `max_inventory_usd`,
`max_inventory_age_s`, and whether `feeCoverageRatio` is anywhere near 1.

### Session 3 — economics (several hours)

Only after 1 and 2 are clean. This is the first run whose PnL means anything.

### Then — the regime matrix

Work through A–K in [`VALIDATION.md` §5](VALIDATION.md#5-test-matrix), tagging
each with `--regime`. Scenarios I (websocket interruption), J (API timeout) and
K (restart with live orders) test **correctness**, not profit.

**Do not re-tune parameters between short sessions.** Collect raw results first.

## Step 5 — read the verdict, don't eyeball the logs

```bash
python -m arcusbot evidence
python -m arcusbot evidence --json > testnet-evidence.json
```

Exit code 0 only when the data genuinely supports proceeding. Report the raw
aggregate, not a summary adjective:

```
sessions          N   regimes: ...
volume            $...
fills             ...  (maker ...)
gross PnL / fees / NET PnL
netBpsOfVolume    ... bps (stderr ..., t=...)
feeCoverageRatio  ...x
max inventory / max drawdown
reconciliation discrepancies / duplicate exposure / kill-switch failures
VERDICT           ...
```

---

## What "success" means

A session counts only when **realized + unrealized − all fees > 0**, and a
session that is profitable *only* on unrealized marks does not count at all —
it is flagged `unrealizedDependent` and excluded.

The aggregate bar is: 5+ sessions, 400+ fills, positive net, positive
**realized-only** net, fee coverage > 1, **t ≥ 2.0**, and zero operational
blockers.

For calibration: the round-trip fee floor at Base tier is **3 bps maker/maker**.
The best simulator result ever recorded here was `+0.82 bps ±2.06` — noise — and
a 20-minute sim run finished at **−0.83 bps** with fee coverage **0.49x**. A
negative testnet result would be unsurprising and is a legitimate finding.

## If the result is negative

**Do not change accounting or risk logic to improve the number.** Use the
diagnostic table in [`VALIDATION.md` §7](VALIDATION.md#7-if-the-result-is-negative)
to identify the cause — adverse selection, fee drag, fill quality, inventory,
exit execution, market selection, latency, or a wrong strategy assumption — then
change **one** thing and re-run the matrix.

## Out of scope for this phase

- **Mainnet.** Not until `evidence` says `POSITIVE-EDGE` with no blockers.
- **Spot / RFQ.** Needs an EIP-712 wallet signature per trade; the bot emits
  intents only. Perps must be proven first.
- **Multiple markets.** One market until the economics are understood.
