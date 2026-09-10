"""WebSocket client: market-data + account streams, and the order fast path.

One socket multiplexes every channel subscription and every RPC (`post`/`get`).
Subscriptions are never authenticated — even the account-scoped channels — so
the socket only needs signatures for order writes.

Important operational facts encoded here:
  * There is NO cancel-on-disconnect. Dropping the socket leaves resting
    orders alive; the engine's kill switch cancels explicitly on shutdown.
  * `subscribed` frames carry the initial snapshot in `contents` — there is no
    separate bare ack, so readiness counts snapshot-bearing channels.
  * Connections are auto-closed after 24h; the reconnect loop handles that.
  * Limits per IP: 50 connections, 100 subscriptions/socket, 1,000 outbound
    subscribe messages/min, 50 in-flight `post`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable, Iterable

import websockets

from .config import Config
from .signing import Signer

log = logging.getLogger("arcusbot.ws")

Handler = Callable[[str, str, dict[str, Any]], Awaitable[None] | None]

MAX_INFLIGHT_POSTS = 40  # documented cap is 50 per connection


class ArcusWS:
    """Resilient WebSocket wrapper with request/response correlation."""

    def __init__(self, cfg: Config, signer: Signer | None = None) -> None:
        self.cfg = cfg
        self.signer = signer
        self.url = cfg.ws_url
        self._ws: Any = None
        self._subscriptions: list[dict[str, Any]] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._handlers: list[Handler] = []
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self.snapshots_seen: set[str] = set()
        self.reconnects = 0
        self.messages_in = 0
        self.last_message_at = 0.0

    # ------------------------------------------------------------ wiring ----
    def on_message(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def subscribe(self, channel: str, sub_id: str | None = None, **params: Any) -> None:
        """Register a subscription (replayed automatically after reconnect)."""
        frame: dict[str, Any] = {"type": "subscribe", "channel": channel}
        if sub_id:
            frame["id"] = sub_id
        frame.update(params)
        self._subscriptions.append(frame)

    # -------------------------------------------------------- connection ----
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="arcus-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                self._task.cancel()
                await self._task

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=2048,
                ) as ws:
                    self._ws = ws
                    self.connected.set()
                    backoff = 1.0
                    log.info("ws connected: %s", self.url)
                    for frame in self._subscriptions:
                        await ws.send(json.dumps(frame))
                    async for raw in ws:
                        self.messages_in += 1
                        self.last_message_at = time.time()
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reconnect on anything
                if self._stop.is_set():
                    return
                self.reconnects += 1
                log.warning("ws disconnected (%s: %s); reconnecting in %.1fs",
                            type(exc).__name__, exc, backoff)
            finally:
                self.connected.clear()
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("websocket closed"))
                self._pending.clear()
            if self._stop.is_set():
                return
            await asyncio.sleep(backoff + random.uniform(0, 0.4))
            backoff = min(self.cfg.reconnect_max_s, backoff * 2)

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            log.debug("non-JSON ws frame dropped")
            return

        # RPC responses carry the echoed numeric id and a `method` field.
        if "method" in msg and isinstance(msg.get("id"), int):
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        mtype = str(msg.get("type", ""))
        channel = str(msg.get("channel", ""))
        contents = msg.get("contents")
        if mtype == "subscribed" and contents:
            self.snapshots_seen.add(f"{channel}:{msg.get('id', '')}")
        if mtype in {"subscribed", "channel_data"} and contents is not None:
            sub_id = str(msg.get("id", ""))
            for handler in self._handlers:
                try:
                    result = handler(channel, sub_id, contents if isinstance(contents, dict) else {"data": contents})
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("ws handler failed on channel %s", channel)
        elif mtype == "error":
            log.warning("ws error frame: %s", msg)

    # --------------------------------------------------------------- rpc ----
    async def _rpc(self, kind: str, request: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
        if self._ws is None:
            raise ConnectionError("websocket not connected")
        if len(self._pending) >= MAX_INFLIGHT_POSTS:
            raise RuntimeError("too many in-flight websocket posts")
        req_id = self._next_id
        self._next_id = (self._next_id % 1_000_000) + 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps({"type": kind, "id": req_id, "request": request}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def get(self, request_type: str, payload: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
        return await self._rpc("get", {"type": request_type, "payload": payload}, timeout)

    async def post_signed(
        self,
        request_type: str,
        payload: dict[str, Any],
        *,
        timestamp_ns: int,
        signature: str,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """Signed order RPC. The envelope carries apiKey/timestamp/signature."""
        if not self.signer:
            raise RuntimeError("signed ws request without signer")
        request = {
            "type": request_type,
            "payload": payload,
            "apiKey": self.signer.api_key,
            "timestamp": str(timestamp_ns),
            "signature": signature,
        }
        return await self._rpc("post", request, timeout)

    # ------------------------------------------------------------- health ---
    def health(self) -> dict[str, Any]:
        return {
            "connected": self.connected.is_set(),
            "reconnects": self.reconnects,
            "messages_in": self.messages_in,
            "seconds_since_message": round(time.time() - self.last_message_at, 2)
            if self.last_message_at
            else None,
            "snapshot_channels": sorted(self.snapshots_seen),
        }


def market_channels(markets: Iterable[str], n_levels: int = 10) -> list[tuple[str, str | None, dict[str, Any]]]:
    """Standard per-market subscription set for a quoting bot."""
    out: list[tuple[str, str | None, dict[str, Any]]] = []
    for market in markets:
        out.append(("bbo", market, {}))
        out.append(("l2Orderbook", market, {"nLevels": n_levels}))
        out.append(("trades", market, {}))
    out.append(("oraclePrices", None, {}))
    out.append(("markets", None, {}))
    return out


def account_channels(address: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        ("account", address, {}),
        ("positions", address, {}),
        ("orders", address, {"nRecentClosed": 20}),
        ("userFills", address, {"nFills": 100}),
        ("funding", address, {}),
    ]
