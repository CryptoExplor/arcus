# Code audit — dead code, defects, and coverage

Method: `vulture` (dead code), `ruff` (F/E9/B/SIM/UP), `coverage`, plus manual
verification of every candidate. Vulture produces false positives on dataclass
fields and JSON-serialized attributes, so nothing was deleted on its say-so
alone — each item was grepped across `arcusbot/`, `tools/`, `tests/` and `docs/`
first.

**Result: 2 real defects fixed, ~140 lines of dead code removed, 15 new tests,
`ws.py` coverage 20% → 61%. 345 tests passing.**

---

## 1. Defects found (not just tidiness)

### 1.1 Dropped WebSocket frames were invisible — **fixed**

`Engine._on_ws` wrapped all frame handling in `except Exception: log.exception(...)`.
A crash while ingesting a `userFills`, `orders` or `positions` frame therefore:

* **lost the frame** (a fill never reaches the PnL tracker), and
* **left `risk.total_errors` at zero**, so `SessionEvidence.api_errors` reported
  `0`, and the consecutive-error kill switch never counted toward its threshold.

The worst shape of failure: the bot's own position diverges from the exchange's
while every health metric says clean. Verified before the fix:

```
ws frame handler crashes -> totalErrors: 0   (should be > 0)
explicit note_error      -> totalErrors: 1
```

Now the handler calls `risk.note_error(...)` before logging. Regression test:
`test_engine_counts_a_dropped_frame_as_an_api_error`, confirmed to fail when the
fix is reverted.

### 1.2 No clock-skew check — **fixed**

Arcus rejects any signed request whose `X-Timestamp` is more than ±30 s from
server time. Nothing in the bot checked this, so a laptop with a drifting clock
would fail *every* order with an opaque `Unauthorized` and no diagnosis — a
likely first-run failure for a local operator, and exactly the "actionable
advice, never a raw traceback" requirement.

`preflight` now compares local time against `GET /v1/time`:

* < 20 s → PASS
* 20–30 s → warning with "sync with NTP"
* ≥ 30 s → fatal

This also gave `ArcusREST.server_time_ns()` a purpose; it was previously dead.

### 1.3 `messages_in` / `last_message_at` only counted in the read loop — fixed

Both counters lived in `_run`'s `async for`, not in `_dispatch`, so any frame
arriving by another path was invisible to the freshness signal that feeds the
STUCK health verdict. Moved into `_dispatch`.

---

## 2. Dead code removed

### 2.1 Unused REST methods (`rest.py` 580 → 445 lines)

| Method | Why it went |
| --- | --- |
| `batch_place` | never called; batch signing is per-element and untested — a trap for a future caller |
| `batch_cancel` | never called |
| `modify_order` | never called; the strategy cancel/replaces |
| `l2` | book data arrives over WebSocket |
| `live_prices` | superseded by `oraclePrices` on WS |
| `rate_limit_usage` | never called; limits handled reactively via `Retry-After` |
| `notional_to_size` | duplicate of the sizing logic in `capital.py` |

These were **untested, unused wire-format code**. Keeping them meant a future
caller could reach for `batch_place` and hit the per-element signature rules
with no test coverage behind them. Deleting is the safer default; they remain in
git history.

### 2.2 Dead config fields — `mainnet_enabled`, `mainnet_capital_usd`, `mainnet_ack`

Parsed into `Config` but **never read by anything**. `mainnet.py` deliberately
reads these variables straight from the environment with strict parsing, so a
malformed value is an error rather than a silent default.

The config copies used the *lax* `_bool()` parser. Two sources of truth for the
one decision that risks real money, where the unused one is more permissive, is
a genuine hazard — a future refactor reaching for `cfg.mainnet_enabled` would
silently weaken the gate. Replaced with a comment explaining why they are absent.

### 2.3 Other removals

| Item | Reason |
| --- | --- |
| `BookState.best_bid_size` / `best_ask_size` | unused; `top_depth_usd` is what the strategy needs |
| `BookState.depth_notional` | unused duplicate of `top_depth_usd` |
| `Config.sim_latency_ms` + `SIM_LATENCY_MS` | parsed, documented in `.env.example`, never used — a setting that silently does nothing |
| `net = self.pnl.net_pnl()` in `risk.evaluate` | computed, never used |
| 5 unused imports | `ruff --select F --fix` |

---

## 3. Coverage: the real finding

The modules that talk to Arcus were the least tested — inverted from where risk
actually lives.

| Module | Before | After | Note |
| --- | --- | --- | --- |
| `ws.py` | **20%** | **61%** | no test file existed; carries every fill |
| `rest.py` | 40% | 49% | dead code removed, remainder is live-only paths |
| `engine.py` | 35% | 37% | large; most paths need a live venue |
| `cli.py` | 34% | 34% | mostly output formatting |
| `sim.py` | 34% | 34% | test scaffolding itself |
| `sweep.py` | **0%** | 0% | see §4 |
| `evidence.py` | 97% | 97% | |

`tests/test_ws.py` (15 tests) now covers frame dispatch: `channel_data`,
`subscribed` snapshots, non-dict contents wrapping, malformed JSON, **a failing
handler not killing the socket or starving other handlers**, async handlers,
error frames, RPC correlation and unknown ids, subscription replay, and the
`reconnects` counter that feeds `ws_disconnects`.

---

## 4. Kept deliberately (candidates I did **not** remove)

| Item | Why it stays |
| --- | --- |
| `sweep.py` (0% coverage) | offline parameter exploration. Not dead — but note it tunes against the **simulator**, and the standing rule is that simulator tuning is not evidence. Useful for generating hypotheses only. |
| `dashboard.py` (22%) | an HTTP server; the untested part is socket plumbing |
| `book.py` oracle/funding/RTH fields | populated from live frames and serialized into reports; vulture cannot see JSON consumers |
| `adaptive.slowing_down` | small, documented, part of a coherent public surface |
| `evidence.reconciliations` | one of the 22 required metrics; reported even at zero |
| `ws.post_signed` | the only signed-WS path; needed for `scheduleCancelAllDeadMansSwitch`, which matters because Arcus has **no cancel-on-disconnect** |
| 14 `except Exception` sites | all log; the two that matter now also count errors |

---

## 5. Remaining known gaps

Honest list of what this audit did **not** resolve:

1. **`engine.py` is 742 statements** — the largest module by far, mixing
   orchestration, WS ingestion, reconciliation and shutdown. It would benefit
   from splitting, but not while it is the code path awaiting live validation:
   refactoring untested-against-reality code adds risk without adding evidence.
2. **`rest.py` remains 49% covered.** The uncovered paths are live-only
   (retries, 429 handling, indeterminate 5xx). They need a mock HTTP layer or a
   real testnet run.
3. **No integration test against a real endpoint** — impossible from the build
   sandbox (egress blocked). This is the same blocker as `docs/EVIDENCE_REPORT.md`.
4. **Remaining ruff findings** are cosmetic: `UP035`/`UP006` (typing style),
   `B904` (`raise ... from`), `SIM102/103`. Left alone to keep this diff
   reviewable.

---

## 6. What this does and does not change

It does **not** change the conclusion of `docs/EVIDENCE_REPORT.md`. The audit
improved correctness and removed hazards; it produced no evidence of a trading
edge, and mainnet readiness is still **NO**.

The one defect that could have affected a *future* validation run is §1.1: a
testnet session that silently dropped fills would have produced clean-looking
evidence with a diverged position. That class of bug is worth more than the
tidying.
