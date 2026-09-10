# Arcus platform reference

A working integrator's reference for the Arcus exchange, distilled from
<https://docs.arcus.xyz> and from what this bot's implementation actually needs.
Emphasis on the rules that cause **silent** failures.

Canonical docs: <https://docs.arcus.xyz/llms.txt> (index; append `.md` to any
page for markdown). An MCP server is available at `https://docs.arcus.xyz/mcp`.

---

## 1. What Arcus is

A hybrid exchange with two very different products behind one API:

| | Perpetuals | Spot (Stock Tokens) |
| --- | --- | --- |
| Model | Central limit order book | RFQ + AMM router |
| Leverage | Yes, cross-margin | No |
| Funding | Yes | No |
| Fees | Tiered maker/taker | **Zero at launch** |
| Hours | 24/7 (RWAs have RTH) | 24/7 |
| Settlement | Off-chain matching | On-chain, atomic |

Collateral is USDG. The venue settles on **Robinhood Chain** (testnet chain ID
`46630`, RPC `https://rpc.testnet.chain.robinhood.com`).

Base URLs:

| | REST | WebSocket |
| --- | --- | --- |
| Testnet | `https://api.testnet.arcus.xyz` | `wss://api.testnet.arcus.xyz/v1/ws` |
| Mainnet | `https://api.arcus.xyz` | `wss://api.arcus.xyz/v1/ws` |

---

## 2. Accounts and keys

- A **master wallet address** owns numbered **subaccounts** (`accountIndex`,
  uint16). Each subaccount has independent margin *and independent rate-limit
  pools*.
- An **API key is the public half of an Ed25519 keypair**. Registration binds it
  to `(address, accountIndex)` with an expiry (1–180 days).
- API keys authorize **trading only** — never withdrawals.
- `GET /v1/account` returns **404 until the account has activity**. That is the
  normal empty state, not an error.

### Registration
`POST /v1/createApiKey` (REST only — not available over WS) with an EIP-712
wallet signature over `{apiWalletName, apiWalletPublicKey, validUntil}`. Then
poll `GET /v1/apiKeys?address=…` until it appears. The web app at
`/api-keys` does the same thing and shows the signing key **once**.

---

## 3. Authentication

Three headers on every mutating request:

| Header | Value |
| --- | --- |
| `X-API-Key` | hex Ed25519 public key |
| `X-Timestamp` | Unix time in **nanoseconds**, within ±30s of server time |
| `X-Signature` | 128 lowercase hex chars |

Read-only endpoints need only `X-API-Key` (or nothing).

### Scheme 1 — typed canonical payload

Used by `placeOrder` (op 1), `cancelOrder` (op 2), `modifyOrder` (op 3),
untriggered TPSL (op 4), and **every element of a batch**.

The signed message *is* the payload: compact JSON, keys sorted, `None` omitted,
no prefix and no concatenation.

```json
{"ad":"0x…","ai":0,"c":"q-1","ct":1736200000000000000,"g":1738792000000000000,
 "m":1,"op":1,"p":6400000,"q":1000,"r":0,"s":0,"t":3,"v":1}
```

| Field | Meaning | Trap |
| --- | --- | --- |
| `ad` | master address | **lowercased** — the only case-folded field |
| `ai` | accountIndex | |
| `c` | clientId | **omit when empty**; signed byte-for-byte in the case you send |
| `ct` | signing timestamp, ns | must equal `X-Timestamp` exactly |
| `g` | goodTilTime, **ns** | body sends **µs**; `g = µs × 1000` |
| `m` | marketId | integer (BTC-USD = 1) |
| `op` | 1 place / 2 cancel / 3 modify / 4 TPSL | |
| `p` | price in ticks | `price / tickSize`, must divide **exactly** |
| `q` | size in quantums | `size / stepSize`, must divide **exactly** |
| `r` | reduce-only | integer `0`/`1`, **never a JSON boolean** |
| `s` | side | 0 BUY / 1 SELL |
| `t` | TIF | 0 GTT / 1 FOK / 2 IOC / 3 ALO |
| `v` | version | always `1` |

Cancel (`op:2`) carries `{ad, ai, ct, m, op, v}` plus **exactly one** of `id`
(orderId) or `c` (clientId). Modify (`op:3`) is the place field set plus a
required `id`, and `g`/`r`/`s`/`t` must **echo the resting order** —
changing an immutable field gives `MODIFY_CHANGED_IMMUTABLE_FIELD`.

> **The tick divisor is always the market's top-level `tickSize`**, even when
> the price sits in a coarser `tickTiers` band. Snap the *price* using the tier,
> then convert to ticks using the base `tickSize`.

### Scheme 2 — legacy

For `cancelAllOrders`, `setLeverage`, `scheduleCancelAllDeadMansSwitch`, and the
WebSocket `authenticate` message:

```
signature = ed25519( str(timestamp_ns) + action + canonical_json(body) )
```

No delimiters. `action` is the **camelCase final path segment**
(`/v1/cancelAllOrders` → `cancelAllOrders`).

### Canonical JSON

`json.dumps(obj, separators=(",", ":"), sort_keys=True)` — for the signed
message *and* the HTTP body. Decimals must never render in exponent form:
`format(Decimal(x).normalize(), "f")`.

### Batches
Every element is independently Scheme-1 signed, all sharing **one**
`X-Timestamp` as their `ct`. Body is `{"orders": [...]}`, each element carrying
its own `signature`. The envelope `X-Signature` header must still be
**present** (conventionally the first element's) — omitting it fails the whole
batch. A batch consumes **one** replay slot.

---

## 4. Trading rules

### Order placement — the rules that bite

- **`goodTilTime` is required on every order, including IOC and FOK**, and must
  be **≥ ~1 month in the future**. It is replay protection, not just expiry.
  Epoch **microseconds** as a string in the body.
- Body `price`/`quantity` are **decimal strings** and must correspond exactly to
  the integers you signed.
- **Minimum notional $5** (`quantity × price`). Reduce-only and TPSL are exempt.
- **`MARKET` requires `timeInForce: IOC`** *and* a `price` acting as a
  protective bound within **10%** of mark — otherwise
  `MarketPriceSlippageToleranceTooHigh`. Sending an aggressive LIMIT IOC is
  simpler and what this bot does.
- **Single-order TPSL returns 501.** TPSL must go through `batchPlaceOrders`
  with the documented groupings and `op=4`.
- `clientId` must be unique per subaccount for the lifetime of the order.

### Time in force

| TIF | Behaviour | Use |
| --- | --- | --- |
| `GTT` | rests until `goodTilTime` | plain limit |
| `IOC` | fill what you can, cancel the rest | taker / closes |
| `FOK` | all or nothing | rarely useful for a maker |
| `ALO` | **post-only**: rejected if it would cross | every maker quote |

**ALO is the single most important choice for a maker.** It makes accidentally
paying a taker fee *impossible* — the worst case is a free rejection.

### Async execution
Writes return **202 ACK** (200 only when already terminal). The HTTP response is
an acknowledgement, **not** an outcome. Definitive lifecycle arrives on the
`orders` and `userFills` WebSocket channels. Treat exchange `positions` as
inventory truth.

### Rejection reasons

| Reason | Meaning |
| --- | --- |
| `POST_ONLY_WOULD_CROSS` | ALO would have taken — free, just re-quote |
| `SELF_TRADE` | would match your own resting order |
| `UNDERCOLLATERALIZED` | insufficient free collateral |
| `REDUCE_ONLY_WOULD_INCREASE` | position already closed/flipped |
| `POSITION_SIZE_CAP_EXCEEDED` / `OPEN_INTEREST_CAP_EXCEEDED` | venue caps |
| `FILL_WILL_EXCEED_TRADING_BOUND` | outside the RWA price band |
| `ORDER_NOT_FOUND(_FOR_MODIFY)` | already terminal |
| `MODIFY_CHANGED_IMMUTABLE_FIELD` | cancel/replace instead |
| `MODIFY_SUPERSEDED_BY_CANCEL` | a cancel won the race |
| `OPEN_ORDER_CAP_EXCEEDED` | too many resting orders |
| `DUPLICATE_CLIENT_ID` | reused clientId |

`errorType` ∈ `{InvalidRequest, Tick, OracleDeviation,
MarketPriceSlippageToleranceTooHigh, OrderSizeTooLarge, ReduceOnly, Unavailable,
Unauthorized, Forbidden, NotImplemented, Transmission, Internal}`;
`errorSource` ∈ `{Order, Cancel}`.

### ⚠ No cancel-on-disconnect

**Dropping your WebSocket does not cancel resting orders.** A crashed bot leaves
live quotes in the book indefinitely. Either cancel explicitly on exit (this bot
does) or arm `POST /v1/scheduleCancelAllDeadMansSwitch` with a timeout the bot
refreshes on a heartbeat.

---

## 5. Market metadata

`GET /v1/markets` → per market: `marketId`, `marketDisplayName`, `status`,
`baseAsset`/`quoteAsset`, `tickSize`, `stepSize`, `tickTiers`,
`minOrderNotional` (~"5"), `minOrderSize`, `maxOrderSize`, margin fractions,
and optionally `oraclePrice`, `markPrice`, `fundingRate`, `openInterest(Cap)`.

- **`tickTiers`** is an ascending list of `{tick, upToPrice?}`; the last entry is
  unbounded. Higher prices use coarser ticks. Snap by walking to the first tier
  whose `upToPrice` ≥ your price.
- **`markPrice: "0"` means unavailable** — do **not** silently fall back to
  `oraclePrice`; skip the market instead.
- Quantities always round **down** to `stepSize`.
- Real-world-asset markets carry `regularTradingHours`; outside RTH they apply
  `offHoursInitialMarginFraction` and hard `upperTradingBound` /
  `lowerTradingBound` bands.

---

## 6. Rate limits

Two independent layers. A 429 body carries `reason`, `retryAfterMs`, and a
`Retry-After` header (seconds).

### Per IP
Token bucket: capacity **1,500**, refill **25/s**.

| Weight | Endpoints |
| --- | --- |
| **0** | health, **all order writes** (place/cancel/modify/batch) |
| 2 | `bbo`, `mids`, `account`, `positions`, `order`, `feeTiers`, `accountStats`, `rateLimit` |
| 20 | `prices`, `markets`, `trades`, `candles`, `openOrders`, `orders`, `fills`, `funding`, `apiKeys` |
| 125 | `setLeverage`, `withdraw`, `transfer` |
| `2 + floor(nLevels/20)` | `l2OrderBook` |
| `floor(N/40)` | batch writes of N orders |

**Order writes are free; reads are not.** If you are getting IP 429s, your
polling is the problem. Stream instead.

### Per subaccount
Order pool **20,000**, cancel pool **40,000**, both growing with lifetime volume
(+1 unit per $0.10 traded). `cancelAllOrders` charges a flat **1,000** against
the cancel pool. An empty/new account is throttled to roughly **1 action per
10s** until it has activity — expect this on a fresh testnet account.

### WebSocket, per IP
50 connections, 100 subscriptions per connection, 1,000 outbound messages/min,
50 in-flight `post` requests per connection, **24-hour connection lifetime**
(plan to reconnect).

---

## 7. WebSocket

Connect, then `{"type":"subscribe","channel":"…","id":N,…}` → `subscribed`
(with a snapshot) → a stream of `channel_data`. Options: `snapshot: false`,
`nLevels` 1–100, `nFills` ≤ 500.

**Subscribing is never authenticated** — even private channels. Authentication
applies to RPC only: `{"type":"post"|"get","id":N,"request":{…}}`, signed with
Scheme 2.

| Channel | Contents | Notes |
| --- | --- | --- |
| `l2Orderbook` / `l2OrderbookUpdates` | depth | snapshot then deltas |
| `bbo` | best bid/ask | cheapest price feed |
| `trades` | public prints | |
| `markets` / `marketAttributes` | metadata changes | |
| `oraclePrices` | oracle + mark | ns epochs; `markPrice "0"` = unavailable |
| `predictedFunding` | upcoming funding | |
| `exchangeAttributeUpdates` | `feeTierConfig` in **ppm** | fees can change live |
| `account` | balances | **re-snapshots every 5s** |
| `positions` | positions | see the trap below |
| `userFills` | your fills | authoritative for PnL |
| `orders` | order lifecycle | authoritative for state |
| `funding`, `candles` | | |

> **The `positions` channel trap.** The initial snapshot is an **object keyed by
> marketId string**; streaming updates are a **single bare position delta per
> frame**, and a closed position arrives as status `FLAT` with zeroed fields.
> Naively replacing local state with each frame will zero out every other
> position. Absence means flat **only** in a full snapshot. This bot passes an
> explicit `authoritative` flag (`engine._sync_positions`) to distinguish them.

---

## 8. Fees and funding

- Tiered maker/taker by trailing 30-day volume, quoted in **ppm**. Read
  `GET /v1/feetiers` at runtime; high tiers can be **negative** for makers
  (rebates). Never hard-code — this bot falls back to a conservative 2/5 bps
  only if the endpoint is unreachable.
- **Spot is free at launch.**
- **Funding** is periodic and paid between longs and shorts — a transfer, not an
  exchange fee. Track it separately in PnL (this bot does).

---

## 9. Spot (Stock Tokens)

Tokenized equities, on-chain, no leverage, no funding, zero fees, 24/7.
Metadata: `GET /v1/api-meta/spot/overview`.

Execution is **RFQ, not an order book**:

1. Client requests a quote for a `sellAmount`.
2. Client computes `minBuyAmount` locally (its own slippage floor — never trust
   a server-supplied bound).
3. Client signs an **EIP-712 intent** (Permit2).
4. Market makers quote the **whole** `sellAmount`; the best output wins.
5. Settlement is atomic on-chain.

Because every trade needs a real wallet signature, spot cannot be driven by an
API key alone. `SpotVolumeStrategy` therefore emits round-trip *intents* gated on
quoted slippage; wiring a signer is left deliberately manual.

---

## 10. Testnet funding

Simplest: the **Testnet Deposit** button in the web app (~$1,000 USDG).

On-chain path (`tools/fund_testnet.py`): `mint` → `approve` →
`initiateDeposit(owner, accountIndex, token, amount)`.

| | |
| --- | --- |
| Chain ID | `46630` |
| RPC | `https://rpc.testnet.chain.robinhood.com` |
| USDG (MockERC20, open `mint`, **6 decimals**) | `0x293b337712d4312776a3a2d292f44410e7873bad` |
| PaxosDepositProxy | `0xb872366eef371d4afb7c6d4d2abd53c17292a34d` |

$1,000 = `1000000000`. You need ~0.001 RH-testnet ETH for gas and there is **no
public faucet**. Verify credit with `GET /v1/account`.

> **These addresses change on every testnet redeploy or reset.** The *flow* is
> stable; the *values* are not. A stale address either reverts obviously or —
> worse — succeeds on-chain and never credits. Check
> <https://docs.arcus.xyz/guides/fund-testnet-account> first.

---

## 11. Endpoint index

**Read** — `/v1/markets`, `/v1/l2OrderbookSnapshot`, `/v1/l2OrderBook/{sym}`,
`/v1/bbo`, `/v1/livePrices`, `/v1/candles`, `/v1/feetiers`, `/v1/fundingRates`,
`/v1/openOrders`, `/v1/orderHistory`, `/v1/fills`, `/v1/positions`,
`/v1/account`, `/v1/accountStats`, `/v1/accountTransferUpdates`,
`/v1/currentRateLimitUsage`, `/v1/rateLimit`, `/v1/apiKeys`,
`/v1/api-meta/spot/overview`

**Write** — `/v1/placeOrder`, `/v1/batchPlaceOrders`, `/v1/cancelOrder`,
`/v1/batchCancelOrders`, `/v1/cancelAllOrders`, `/v1/modifyOrder`,
`/v1/batchModifyOrders`, `/v1/scheduleCancelAllDeadMansSwitch`,
`/v1/setLeverage`, `/v1/createApiKey`

---

## 12. Integration checklist

- [ ] `X-Timestamp` in **nanoseconds**, clock within 30s
- [ ] Scheme 1 for order ops, Scheme 2 for legacy ops — they are not interchangeable
- [ ] `ad` lowercased; `c`/`id` verbatim; `c` omitted when empty
- [ ] `r` as integer, not boolean
- [ ] `g` = body µs × 1000, ≥ 1 month ahead, **on every TIF**
- [ ] Price/size divide **exactly** into ticks/quantums; sizes round down
- [ ] Tier-aware price snapping, but ticks computed from the base `tickSize`
- [ ] Notional ≥ $5 unless reduce-only
- [ ] ALO for all maker quotes
- [ ] Batch elements each signed, sharing one `ct`; `X-Signature` still present
- [ ] Treat 202 as an ack; get truth from WS
- [ ] Handle the `positions` snapshot-vs-delta distinction
- [ ] Idempotent fills by `tradeId`
- [ ] Explicit cancel on exit, or a dead man's switch armed
- [ ] Client-side rate budget; stream instead of polling
