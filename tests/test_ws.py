"""WebSocket frame dispatch — the path every fill arrives on.

`ws.py` was the least-tested module in the codebase (20% coverage, no test
file) while being responsible for delivering fills, order updates and position
snapshots. A silent failure here is silent state divergence, which is the
expensive kind.

These tests drive `_dispatch` directly rather than opening a socket, so they
are fast and deterministic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.config import Config  # noqa: E402
from arcusbot.ws import ArcusWS, market_channels  # noqa: E402


def client() -> ArcusWS:
    return ArcusWS(Config())


def dispatch(ws: ArcusWS, frame) -> None:
    raw = frame if isinstance(frame, str) else json.dumps(frame)
    asyncio.run(ws._dispatch(raw))


def collect(ws: ArcusWS) -> list[tuple]:
    seen: list[tuple] = []
    ws.on_message(lambda ch, sid, contents: seen.append((ch, sid, contents)))
    return seen


# ------------------------------------------------------------ dispatch ----


def test_channel_data_reaches_handlers() -> None:
    ws = client()
    seen = collect(ws)
    dispatch(ws, {"type": "channel_data", "channel": "userFills",
                  "id": "1", "contents": {"fills": [{"id": "f1"}]}})
    assert seen == [("userFills", "1", {"fills": [{"id": "f1"}]})]


def test_subscribed_snapshot_is_delivered_and_recorded() -> None:
    ws = client()
    seen = collect(ws)
    dispatch(ws, {"type": "subscribed", "channel": "positions",
                  "id": "p", "contents": {"1": {"size": "2"}}})
    assert seen[0][0] == "positions"
    assert "positions:p" in ws.snapshots_seen


def test_non_dict_contents_are_wrapped_not_dropped() -> None:
    """A bare list of fills must still reach the handler."""
    ws = client()
    seen = collect(ws)
    dispatch(ws, {"type": "channel_data", "channel": "trades",
                  "id": "t", "contents": [{"px": "1"}]})
    assert seen[0][2] == {"data": [{"px": "1"}]}


def test_malformed_json_is_survivable() -> None:
    ws = client()
    seen = collect(ws)
    dispatch(ws, "{not json")
    assert seen == []          # dropped, but no exception escaped


def test_a_failing_handler_cannot_break_the_socket() -> None:
    """One bad consumer must not stop other consumers or kill the reader."""
    ws = client()
    good: list[str] = []

    def boom(channel, sub_id, contents):
        raise RuntimeError("handler bug")

    ws.on_message(boom)
    ws.on_message(lambda ch, sid, c: good.append(ch))
    dispatch(ws, {"type": "channel_data", "channel": "bbo",
                  "id": "b", "contents": {"bid": "1"}})
    assert good == ["bbo"]


def test_async_handlers_are_awaited() -> None:
    ws = client()
    seen: list[str] = []

    async def handler(channel, sub_id, contents):
        await asyncio.sleep(0)
        seen.append(channel)

    ws.on_message(handler)
    dispatch(ws, {"type": "channel_data", "channel": "orders",
                  "id": "o", "contents": {"x": 1}})
    assert seen == ["orders"]


def test_error_frames_do_not_reach_data_handlers() -> None:
    ws = client()
    seen = collect(ws)
    dispatch(ws, {"type": "error", "message": "bad subscription"})
    assert seen == []


def test_empty_contents_is_not_treated_as_data() -> None:
    ws = client()
    seen = collect(ws)
    dispatch(ws, {"type": "channel_data", "channel": "bbo", "id": "b"})
    assert seen == []


# ----------------------------------------------------------------- rpc ----


def test_rpc_response_resolves_its_future_and_skips_handlers() -> None:
    ws = client()
    seen = collect(ws)

    async def scenario():
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        ws._pending[7] = fut
        await ws._dispatch(json.dumps({"id": 7, "method": "post",
                                       "result": {"ok": True}}))
        return fut

    fut = asyncio.run(scenario())
    assert fut.done() and fut.result()["result"] == {"ok": True}
    assert seen == []


def test_unknown_rpc_id_is_ignored() -> None:
    ws = client()
    dispatch(ws, {"id": 999, "method": "post", "result": {}})   # no KeyError


# -------------------------------------------------------- subscriptions ---


def test_subscriptions_are_recorded_for_replay_after_reconnect() -> None:
    ws = client()
    ws.subscribe("bbo", "BTC-USD", marketId=1)
    ws.subscribe("userFills")
    assert ws._subscriptions[0] == {"type": "subscribe", "channel": "bbo",
                                    "id": "BTC-USD", "marketId": 1}
    assert ws._subscriptions[1] == {"type": "subscribe", "channel": "userFills"}


def test_market_channels_requests_book_and_quotes_per_market() -> None:
    chans = market_channels(["BTC-USD"], n_levels=5)
    names = {c[0] for c in chans}
    assert "bbo" in names
    assert any("l2" in n.lower() for n in names)


# -------------------------------------------------------------- health ---


def test_health_reports_disconnect_count_for_evidence() -> None:
    """`reconnects` feeds SessionEvidence.ws_disconnects."""
    ws = client()
    assert ws.health()["reconnects"] == 0
    ws.reconnects += 2
    assert ws.health()["reconnects"] == 2
    assert ws.health()["connected"] is False


def test_health_tracks_message_flow() -> None:
    ws = client()
    assert ws.health()["seconds_since_message"] is None
    dispatch(ws, {"type": "channel_data", "channel": "bbo",
                  "id": "b", "contents": {"bid": "1"}})
    assert ws.health()["messages_in"] == 1
    assert ws.health()["seconds_since_message"] is not None


# ------------------------- dropped frames must be visible in evidence ------
#
# Regression: Engine._on_ws_frame caught every exception and only logged it.
# A crash while ingesting a fill frame therefore lost the fill AND left
# risk.total_errors at zero, so `SessionEvidence.api_errors` reported 0 while
# state silently diverged, and the consecutive-error kill switch never fired.


def test_engine_counts_a_dropped_frame_as_an_api_error() -> None:
    from decimal import Decimal

    from arcusbot.engine import Engine
    from arcusbot.pnl import FeeSchedule, PnLTracker
    from arcusbot.risk import RiskManager

    cfg = Config()
    engine = Engine.__new__(Engine)          # no live connections needed
    engine.cfg = cfg
    engine.pnl = PnLTracker(FeeSchedule())
    engine.risk = RiskManager(cfg, engine.pnl)
    engine.states = {}

    def explode(_contents):
        raise RuntimeError("malformed orders frame")

    engine._on_orders = explode

    assert engine.risk.total_errors == 0
    engine._on_ws("orders", "1", {"orders": [{"bad": Decimal(1)}]})
    assert engine.risk.total_errors == 1, "a dropped frame must count as an API error"
    assert engine.risk.consecutive_errors == 1
