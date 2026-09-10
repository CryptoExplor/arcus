"""Signed REST client for the Arcus gateway (stdlib http only).

Only endpoints documented at https://docs.arcus.xyz/api-reference are used.
IP weights are tracked client-side (the gateway publishes no X-RateLimit-*
headers) so the bot can self-pace instead of discovering 429s the hard way:
one IP bucket of 1,500 weight refilling at 25/s.

Order writes cost 0 IP weight — they are governed by the per-subaccount order
and cancel pools instead (GET /v1/currentRateLimitUsage).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .config import Config
from .scaling import D, dec_str, snap_price, snap_size, to_quantums, to_ticks
from .signing import (
    Signer,
    cancel_payload,
    good_til_us,
    modify_payload,
    now_ns,
    place_payload,
)

log = logging.getLogger("arcusbot.rest")

# Documented IP weights (https://docs.arcus.xyz/api-reference/rate-limits)
ENDPOINT_WEIGHT: dict[str, int] = {
    "/health": 0,
    "/v1/placeOrder": 0,
    "/v1/cancelOrder": 0,
    "/v1/modifyOrder": 0,
    "/v1/cancelAllOrders": 0,
    "/v1/batchPlaceOrders": 0,
    "/v1/batchCancelOrders": 0,
    "/v1/batchModifyOrders": 0,
    "/v1/time": 1,
    "/v1/compliance": 1,
    "/v1/bbo": 2,
    "/v1/mids": 2,
    "/v1/account": 2,
    "/v1/positions": 2,
    "/v1/order": 2,
    "/v1/feetiers": 2,
    "/v1/leverages": 2,
    "/v1/accountStats": 2,
    "/v1/currentRateLimitUsage": 2,
    "/v1/rateLimit": 2,
    "/v1/l2OrderBook": 3,
    "/v1/markets": 20,
    "/v1/livePrices": 20,
    "/v1/prices": 20,
    "/v1/trades": 20,
    "/v1/candles": 20,
    "/v1/portfolio": 20,
    "/v1/openOrders": 20,
    "/v1/orders": 20,
    "/v1/orderHistory": 20,
    "/v1/fills": 20,
    "/v1/funding": 20,
    "/v1/fundingRates": 20,
    "/v1/accountTransferUpdates": 20,
    "/v1/apiKeys": 20,
    "/v1/createApiKey": 20,
    "/v1/setLeverage": 125,
    "/v1/withdraw": 125,
    "/v1/transfer": 125,
}

IP_BUCKET_CAPACITY = 1_500
IP_REFILL_PER_S = 25.0


class ArcusError(RuntimeError):
    """Non-2xx response from the gateway, with the parsed body attached."""

    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"HTTP {status} on {path}: {body[:400]}")
        self.status = status
        self.path = path
        self.raw = body
        try:
            self.body: dict[str, Any] = json.loads(body)
        except Exception:
            self.body = {"error": body}

    @property
    def error_type(self) -> str:
        return str(self.body.get("errorType") or "")

    @property
    def rejection_reason(self) -> str:
        return str(self.body.get("rejectionReason") or "")

    @property
    def rate_limited(self) -> bool:
        return self.status == 429

    @property
    def retry_after_s(self) -> float:
        ms = self.body.get("retryAfterMs")
        if ms is not None:
            return float(ms) / 1000.0
        return 1.0

    @property
    def limit_reason(self) -> str:
        return str(self.body.get("reason") or "ip")

    @property
    def indeterminate(self) -> bool:
        """True when the order MAY have reached the matching engine.

        A 4xx with a rejection reason is definitive: the order does not exist.
        A 5xx, a gateway timeout or a transport failure is NOT — the exchange
        may have accepted the order and lost the response. Treating those as
        "did not happen" is how a bot ends up with untracked live orders, so
        callers must reconcile instead of assuming.
        """
        if self.status >= 500 or self.status in {408, 425}:
            return True
        # 429 is definitive: rate-limited requests are rejected before matching.
        return False


class IPBudget:
    """Client-side mirror of the documented per-IP weight bucket."""

    def __init__(self, capacity: int = IP_BUCKET_CAPACITY, refill: float = IP_REFILL_PER_S) -> None:
        self.capacity = float(capacity)
        self.refill = refill
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.throttled_s = 0.0

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill)
        self.updated = now

    def take(self, weight: int) -> float:
        """Consume `weight`; returns seconds the caller should sleep first."""
        self._refill()
        if weight <= 0:
            return 0.0
        if self.tokens >= weight:
            self.tokens -= weight
            return 0.0
        deficit = weight - self.tokens
        wait = deficit / self.refill
        self.tokens = 0.0
        self.throttled_s += wait
        return wait

    def snapshot(self) -> dict[str, Any]:
        self._refill()
        return {
            "tokens": round(self.tokens, 1),
            "capacity": self.capacity,
            "self_throttled_s": round(self.throttled_s, 2),
        }


def _weight_for(path: str) -> int:
    base = path.split("?", 1)[0]
    if base in ENDPOINT_WEIGHT:
        return ENDPOINT_WEIGHT[base]
    # /v1/bbo/BTC-USD, /v1/l2OrderBook/BTC-USD, /v1/order/<id> ...
    parts = base.split("/")
    if len(parts) > 2:
        prefix = "/".join(parts[:3])
        if prefix in ENDPOINT_WEIGHT:
            return ENDPOINT_WEIGHT[prefix]
    return 20


class ArcusREST:
    """Thin, dependency-free REST client with signing and self-pacing."""

    def __init__(self, cfg: Config, signer: Signer | None = None, timeout: float = 12.0) -> None:
        self.cfg = cfg
        self.base = cfg.rest_url.rstrip("/")
        self.signer = signer
        self.timeout = timeout
        self.budget = IPBudget()
        self._markets: dict[str, dict[str, Any]] = {}
        self._markets_by_id: dict[int, dict[str, Any]] = {}
        self._markets_fetched = 0.0
        self.request_count = 0
        self.error_count = 0

    # ------------------------------------------------------------ transport --
    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        wait = self.budget.take(_weight_for(path))
        if wait > 0:
            log.debug("self-throttling %.2fs before %s (IP weight budget)", wait, path)
            time.sleep(wait)

        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        req_headers = {"Content-Type": "application/json", "User-Agent": "arcus-testnet-bot/1.0"}
        if self.signer:
            req_headers.setdefault("X-API-Key", self.signer.api_key)
        req_headers.update(headers or {})

        req = urllib.request.Request(self.base + path, data=data, method=method, headers=req_headers)
        self.request_count += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                return json.loads(payload or b"{}")
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - explicit mapping
            self.error_count += 1
            raise ArcusError(exc.code, exc.read().decode(errors="replace"), path) from None
        except urllib.error.URLError as exc:
            self.error_count += 1
            raise ArcusError(0, f"network error: {exc.reason}", path) from None

    def get(self, path: str, **params: Any) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._request("GET", path + ("?" + query if query else ""))

    # ------------------------------------------------------------- public ----
    def health(self) -> Any:
        return self.get("/health")

    def server_time_ns(self) -> int:
        resp = self.get("/v1/time")
        for key in ("time", "timestamp", "serverTime", "now"):
            if key in resp:
                return int(resp[key])
        return now_ns()

    def markets(self, refresh: bool = False, max_age_s: float = 300.0) -> dict[str, dict[str, Any]]:
        if refresh or not self._markets or (time.time() - self._markets_fetched) > max_age_s:
            resp = self.get("/v1/markets")
            rows = resp.get("markets", resp) if isinstance(resp, dict) else resp
            if isinstance(rows, dict):
                rows = list(rows.values())
            self._markets = {str(m["marketDisplayName"]): m for m in rows}
            self._markets_by_id = {int(m["marketId"]): m for m in rows}
            self._markets_fetched = time.time()
        return self._markets

    def market(self, key: str | int) -> dict[str, Any]:
        self.markets()
        if isinstance(key, int):
            if key in self._markets_by_id:
                return self._markets_by_id[key]
            raise KeyError(f"unknown marketId {key}")
        if key in self._markets:
            return self._markets[key]
        for m in self._markets.values():
            if str(m.get("baseAsset")) == key:
                return m
        raise KeyError(f"unknown market {key!r}")

    def bbo(self, market: str) -> dict[str, Any]:
        return self.get(f"/v1/bbo/{urllib.parse.quote(market)}")

    def l2(self, market: str, n_levels: int = 10) -> dict[str, Any]:
        return self.get(f"/v1/l2OrderBook/{urllib.parse.quote(market)}", nLevels=n_levels)

    def fee_tiers(self) -> Any:
        return self.get("/v1/feetiers")

    def live_prices(self) -> Any:
        return self.get("/v1/livePrices")

    # ------------------------------------------------- account (public reads) --
    def account(self) -> dict[str, Any]:
        return self.get("/v1/account", address=self.cfg.address, accountIndex=self.cfg.account_index)

    def positions(self) -> Any:
        return self.get("/v1/positions", address=self.cfg.address, accountIndex=self.cfg.account_index)

    def open_orders(self, market: str | None = None) -> Any:
        return self.get(
            "/v1/openOrders",
            address=self.cfg.address,
            accountIndex=self.cfg.account_index,
            market=market,
        )

    def fills(self, limit: int = 100, market: str | None = None, **params: Any) -> Any:
        return self.get(
            "/v1/fills",
            address=self.cfg.address,
            accountIndex=self.cfg.account_index,
            limit=limit,
            market=market,
            **params,
        )

    def account_stats(self, **params: Any) -> Any:
        return self.get("/v1/accountStats", address=self.cfg.address, **params)

    def rate_limit_usage(self) -> Any:
        return self.get(
            "/v1/currentRateLimitUsage",
            address=self.cfg.address,
            accountIndex=self.cfg.account_index,
        )

    def api_keys(self) -> Any:
        return self.get("/v1/apiKeys", address=self.cfg.address)

    # --------------------------------------------------------- order writes --
    def _require_signer(self) -> Signer:
        if not self.signer:
            raise RuntimeError("signed request attempted without ARCUS_API_SECRET")
        return self.signer

    def build_order(
        self,
        market: str | int,
        side: str,
        quantity: Any,
        price: Any,
        *,
        ts_ns: int,
        order_type: str = "LIMIT",
        tif: str = "GTT",
        reduce_only: bool = False,
        client_id: str | None = None,
        gtt_days: int = 45,
        gtt_us: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Returns (typed_payload_fields, request_body) for one order.

        Price/size are snapped to the market grid, then converted to ticks and
        quantums; the body carries the SAME decimal strings the integers were
        derived from (the two must agree or the signature check fails).
        """
        m = self.market(market)
        px = snap_price(m, price, side="BUY" if side == "BUY" else "SELL")
        qty = snap_size(m, quantity)
        if qty <= 0:
            raise ValueError(f"size {dec_str(quantity)} snapped to zero on {m['marketDisplayName']}")
        gtt = gtt_us if gtt_us is not None else good_til_us(gtt_days)

        fields = place_payload(
            address=self.cfg.address,
            account_index=self.cfg.account_index,
            ts_ns=ts_ns,
            gtt_us=gtt,
            market_id=int(m["marketId"]),
            price_ticks=to_ticks(px, m),
            size_quantums=to_quantums(qty, m),
            side=side,
            tif=tif,
            reduce_only=reduce_only,
            client_id=client_id,
        )
        body: dict[str, Any] = {
            "address": self.cfg.address,
            "accountIndex": self.cfg.account_index,
            "marketId": int(m["marketId"]),
            "orderSide": side,
            "orderType": order_type,
            "quantity": dec_str(qty),
            "price": dec_str(px),
            "timeInForce": tif,
            "goodTilTime": str(gtt),
            "timestamp": ts_ns,
        }
        if client_id:
            body["clientId"] = client_id
        if reduce_only:
            body["reduceOnly"] = True
        return fields, body

    def place_order(self, market: str | int, side: str, quantity: Any, price: Any, **kw: Any) -> dict[str, Any]:
        signer = self._require_signer()
        ts = now_ns()
        fields, body = self.build_order(market, side, quantity, price, ts_ns=ts, **kw)
        payload, signature = signer.sign_typed(fields)
        log.debug("placeOrder payload=%s", payload)
        return self._request("POST", "/v1/placeOrder", body, signer.headers(ts, signature))

    def batch_place(self, orders: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Each element is signed on its own; all share one X-Timestamp.

        `orders` elements are kwargs for build_order plus market/side/quantity/price.
        """
        signer = self._require_signer()
        ts = now_ns()
        elements: list[dict[str, Any]] = []
        for spec in orders:
            spec = dict(spec)
            market = spec.pop("market")
            side = spec.pop("side")
            quantity = spec.pop("quantity")
            price = spec.pop("price")
            fields, body = self.build_order(market, side, quantity, price, ts_ns=ts, **spec)
            _, signature = signer.sign_typed(fields)
            body["signature"] = signature
            elements.append(body)
        if not elements:
            return {"responses": []}
        # X-Signature must be PRESENT on a batch; its value is not verified.
        headers = signer.headers(ts, elements[0]["signature"])
        return self._request("POST", "/v1/batchPlaceOrders", {"orders": elements}, headers)

    def cancel_order(
        self,
        market: str | int,
        *,
        order_id: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        signer = self._require_signer()
        m = self.market(market)
        ts = now_ns()
        fields = cancel_payload(
            address=self.cfg.address,
            account_index=self.cfg.account_index,
            ts_ns=ts,
            market_id=int(m["marketId"]),
            order_id=order_id,
            client_id=client_id,
        )
        payload, signature = signer.sign_typed(fields)
        body: dict[str, Any] = {
            "address": self.cfg.address,
            "accountIndex": self.cfg.account_index,
            "marketId": int(m["marketId"]),
            "timestamp": ts,
        }
        if order_id:
            body["kind"] = "orderId"
            body["orderId"] = str(order_id)
        else:
            body["kind"] = "clientId"
            body["clientId"] = client_id
        log.debug("cancelOrder payload=%s", payload)
        return self._request("POST", "/v1/cancelOrder", body, signer.headers(ts, signature))

    def batch_cancel(self, cancels: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        signer = self._require_signer()
        ts = now_ns()
        elements: list[dict[str, Any]] = []
        for spec in cancels:
            m = self.market(spec["market"])
            fields = cancel_payload(
                address=self.cfg.address,
                account_index=self.cfg.account_index,
                ts_ns=ts,
                market_id=int(m["marketId"]),
                order_id=spec.get("order_id"),
                client_id=spec.get("client_id"),
            )
            _, signature = signer.sign_typed(fields)
            body: dict[str, Any] = {
                "address": self.cfg.address,
                "accountIndex": self.cfg.account_index,
                "marketId": int(m["marketId"]),
                "timestamp": ts,
                "signature": signature,
            }
            if spec.get("order_id"):
                body["kind"] = "orderId"
                body["orderId"] = str(spec["order_id"])
            else:
                body["kind"] = "clientId"
                body["clientId"] = spec["client_id"]
            elements.append(body)
        if not elements:
            return {"responses": []}
        headers = signer.headers(ts, elements[0]["signature"])
        return self._request("POST", "/v1/batchCancelOrders", {"cancels": elements}, headers)

    def modify_order(
        self,
        market: str | int,
        order_id: str,
        *,
        price: Any,
        quantity: Any,
        side: str,
        tif: str = "GTT",
        reduce_only: bool = False,
        client_id: str | None = None,
        gtt_us: int | None = None,
    ) -> dict[str, Any]:
        signer = self._require_signer()
        m = self.market(market)
        ts = now_ns()
        px = snap_price(m, price, side=side)
        qty = snap_size(m, quantity)
        gtt = gtt_us if gtt_us is not None else good_til_us()
        fields = modify_payload(
            address=self.cfg.address,
            account_index=self.cfg.account_index,
            ts_ns=ts,
            gtt_us=gtt,
            market_id=int(m["marketId"]),
            order_id=order_id,
            price_ticks=to_ticks(px, m),
            size_quantums=to_quantums(qty, m),
            side=side,
            tif=tif,
            reduce_only=reduce_only,
            client_id=client_id,
        )
        _, signature = signer.sign_typed(fields)
        body: dict[str, Any] = {
            "address": self.cfg.address,
            "accountIndex": self.cfg.account_index,
            "marketId": int(m["marketId"]),
            "orderId": str(order_id),
            "price": dec_str(px),
            "quantity": dec_str(qty),
            "goodTilTime": str(gtt),
            "timestamp": ts,
        }
        if client_id:
            body["clientId"] = client_id
        return self._request("POST", "/v1/modifyOrder", body, signer.headers(ts, signature))

    # ------------------------------------------------ scheme-2 (legacy) ops --
    def _legacy(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        signer = self._require_signer()
        ts = now_ns()
        signature = signer.sign_legacy(ts, action, body)
        return self._request("POST", f"/v1/{action}", body, signer.headers(ts, signature))

    def cancel_all(self, market: str | int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"address": self.cfg.address, "accountIndex": self.cfg.account_index}
        if market is not None:
            body["marketId"] = int(self.market(market)["marketId"])
        return self._legacy("cancelAllOrders", body)

    def set_leverage(self, market: str | int, leverage: int) -> dict[str, Any]:
        body = {
            "address": self.cfg.address,
            "accountIndex": self.cfg.account_index,
            "marketId": int(self.market(market)["marketId"]),
            "leverage": int(leverage),
        }
        return self._legacy("setLeverage", body)

    def arm_dead_mans_switch(self, timeout_s: int) -> dict[str, Any]:
        """Arm/refresh/disarm the per-subaccount dead man's switch (0 disarms)."""
        body = {
            "address": self.cfg.address,
            "accountIndex": self.cfg.account_index,
            "timeoutMs": int(timeout_s) * 1000,
        }
        return self._legacy("scheduleCancelAllDeadMansSwitch", body)

    # ------------------------------------------------------------- helpers ---
    def notional_to_size(self, market: str | int, notional_usd: Any, price: Any) -> Decimal:
        """USD notional -> base-asset size, snapped and floored to market limits."""
        m = self.market(market)
        px = D(price)
        if px <= 0:
            return Decimal(0)
        size = snap_size(m, D(notional_usd) / px)
        min_size = D(m.get("minOrderSize", "0"))
        max_size = D(m.get("maxOrderSize", "1e18"))
        if size < min_size:
            size = snap_size(m, min_size)
        if size > max_size:
            size = snap_size(m, max_size)
        return size
