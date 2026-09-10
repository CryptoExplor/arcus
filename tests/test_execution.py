"""Execution-path robustness: the failures that cost real money.

These tests exercise the states a live venue actually produces — lost
acknowledgements, duplicate events, timeouts, rate limits and restarts — and
assert the engine never ends up with untracked exposure.
"""

from __future__ import annotations

import asyncio
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.book import BookState, MarketState  # noqa: E402
from arcusbot.config import Config  # noqa: E402
from arcusbot.engine import Engine  # noqa: E402
from arcusbot.pnl import Fill, PnLTracker  # noqa: E402
from arcusbot.rest import ArcusError  # noqa: E402
from arcusbot.sim import SIM_MARKETS  # noqa: E402
from arcusbot.strategy import MarketWorker, OrderIntent  # noqa: E402


# ------------------------------------------------------------------ helpers --


def make_engine(tmp_path: Path, **overrides) -> Engine:
    cfg = Config.from_env(
        venue="arcus", mode="live", markets=["BTC-USD"],
        state_dir=tmp_path, persist_state=False,
        address="0x" + "ab" * 20, api_key="k", api_secret="ab" * 32,
        **overrides,
    )
    eng = Engine(cfg)
    meta = dict(SIM_MARKETS["BTC-USD"])
    book = BookState(market="BTC-USD")
    book.apply_snapshot({"bids": [["63990", "1"]], "asks": [["64010", "1"]],
                         "lastSequenceId": 1, "globalSequenceId": 1})
    state = MarketState(market="BTC-USD", market_id=1, meta=meta, book=book)
    state.mark_price = Decimal("64000")
    state.mark_updated_at = time.time()
    eng.states["BTC-USD"] = state
    eng.workers["BTC-USD"] = MarketWorker(cfg=cfg, market="BTC-USD", state=state, pnl=eng.pnl)
    return eng


def intent(cid: str = "b0001abcdef", side: str = "BUY") -> OrderIntent:
    return OrderIntent(action="place", market="BTC-USD", side=side,
                       size=Decimal("0.001"), price=Decimal("63990"),
                       tif="ALO", reduce_only=False, client_id=cid, tag="quote")


# ------------------------------------------------- indeterminate placement --


def test_transport_failure_is_treated_as_maybe_live(tmp_path: Path) -> None:
    """A timeout after send must NOT be recorded as 'never happened'."""
    eng = make_engine(tmp_path)
    eng._mark_indeterminate(intent(), "TimeoutError")
    assert eng.needs_reconcile
    assert "b0001abcdef" in eng.pending_unknown


def test_5xx_is_indeterminate_but_4xx_is_definitive() -> None:
    assert ArcusError(503, "{}", "/v1/order").indeterminate
    assert ArcusError(500, "{}", "/v1/order").indeterminate
    assert ArcusError(408, "{}", "/v1/order").indeterminate
    # A rejection is authoritative: the order does not exist.
    assert not ArcusError(400, '{"errorType":"Tick"}', "/v1/order").indeterminate
    assert not ArcusError(429, '{"reason":"ip"}', "/v1/order").indeterminate


def test_rate_limit_does_not_trigger_reconciliation(tmp_path: Path) -> None:
    """429s are rejected before matching, so there is nothing to reconcile."""
    eng = make_engine(tmp_path)
    err = ArcusError(429, '{"retryAfterMs":250,"reason":"ip"}', "/v1/order")
    if err.indeterminate:
        eng._mark_indeterminate(intent(), "429")
    assert not eng.needs_reconcile
    assert err.retry_after_s == pytest.approx(0.25)


def test_lost_ack_order_is_adopted_from_exchange_state(tmp_path: Path) -> None:
    """The order WAS live: reconciliation must adopt, not forget it."""
    eng = make_engine(tmp_path)
    eng._mark_indeterminate(intent(), "TimeoutError")

    eng.rest.open_orders = lambda market=None: {"orders": [  # type: ignore[assignment]
        {"clientId": "b0001abcdef", "orderId": "EX-1", "marketDisplayName": "BTC-USD",
         "side": "BUY", "quantity": "0.001", "price": "63990", "reduceOnly": False}
    ]}
    eng._refresh_positions = _noop  # type: ignore[assignment]

    assert asyncio.run(eng.reconcile())
    worker = eng.workers["BTC-USD"]
    assert "b0001abcdef" in worker.quotes, "live order must be tracked after reconciliation"
    assert worker.quotes["b0001abcdef"].order_id == "EX-1"
    assert not eng.needs_reconcile and not eng.pending_unknown


def test_lost_ack_order_that_never_existed_is_dropped(tmp_path: Path) -> None:
    eng = make_engine(tmp_path)
    eng._mark_indeterminate(intent(), "TimeoutError")
    eng.rest.open_orders = lambda market=None: {"orders": []}  # type: ignore[assignment]
    eng._refresh_positions = _noop  # type: ignore[assignment]

    assert asyncio.run(eng.reconcile())
    assert "b0001abcdef" not in eng.workers["BTC-USD"].quotes
    assert not eng.needs_reconcile


def test_reconciliation_failure_keeps_the_engine_blocked(tmp_path: Path) -> None:
    """If we cannot read authoritative state we must not resume guessing."""
    eng = make_engine(tmp_path)
    eng._mark_indeterminate(intent(), "TimeoutError")

    def boom(market=None):
        raise TimeoutError("gateway down")

    eng.rest.open_orders = boom  # type: ignore[assignment]
    eng._refresh_positions = _noop  # type: ignore[assignment]

    assert asyncio.run(eng.reconcile()) is False
    assert eng.needs_reconcile, "must stay blocked until state is known"
    assert eng.pending_unknown, "the unknown order must not be silently discarded"


def test_orphan_exchange_order_is_adopted(tmp_path: Path) -> None:
    """Orders resting from a previous run must be taken over, not ignored."""
    eng = make_engine(tmp_path)
    eng.needs_reconcile = True
    eng.rest.open_orders = lambda market=None: [  # type: ignore[assignment]
        {"clientId": "a9999zzzzzz", "orderId": "EX-9", "marketDisplayName": "BTC-USD",
         "side": "SELL", "quantity": "0.002", "price": "64100", "reduceOnly": False}
    ]
    eng._refresh_positions = _noop  # type: ignore[assignment]

    assert asyncio.run(eng.reconcile())
    quotes = eng.workers["BTC-USD"].quotes
    assert "a9999zzzzzz" in quotes
    assert quotes["a9999zzzzzz"].size == Decimal("0.002")


def test_adopted_orders_are_cancellable_like_any_other(tmp_path: Path) -> None:
    """An adopted order must be reachable by the flatten/cancel path."""
    eng = make_engine(tmp_path)
    worker = eng.workers["BTC-USD"]
    worker.adopt("x1", "EX-1", {"market": "BTC-USD", "side": "BUY",
                                "size": "0.001", "price": "63000", "reduceOnly": False})
    assert any(q.client_id == "x1" for q in worker.open_quotes())
    # Go long, so the adopted BUY is a position-increasing order that the
    # flatten path is required to pull.
    eng.pnl.record_fill(Fill(trade_id="f1", market="BTC-USD", side="BUY",
                             price=Decimal("64000"), size=Decimal("0.002"),
                             liquidity="MAKER", fee=Decimal("0"), ts=time.time()))
    cancels = [i for i in worker.flatten_intents(urgent=True) if i.action == "cancel"]
    assert any(c.client_id == "x1" and c.order_id == "EX-1" for c in cancels)


# ----------------------------------------------------------- fill handling --


def test_partial_fills_accumulate_into_one_position() -> None:
    pnl = PnLTracker()
    for i, qty in enumerate(["0.001", "0.002", "0.003"]):
        pnl.record_fill(Fill(trade_id=f"t{i}", market="BTC-USD", side="BUY",
                             price=Decimal("64000"), size=Decimal(qty),
                             liquidity="MAKER", fee=Decimal("0"), ts=time.time()))
    assert pnl.book("BTC-USD").position == Decimal("0.006")


def test_duplicate_fill_events_are_idempotent() -> None:
    """Exchanges re-send fills after a reconnect; PnL must not double-count."""
    pnl = PnLTracker()
    fill = Fill(trade_id="dup-1", market="BTC-USD", side="BUY",
                price=Decimal("64000"), size=Decimal("0.001"),
                liquidity="MAKER", fee=Decimal("0.05"), ts=time.time())
    assert pnl.record_fill(fill) is True
    assert pnl.record_fill(fill) is False, "same tradeId must be ignored"
    # A re-sent copy after a reconnect (new object, same id) is also a no-op.
    assert pnl.record_fill(Fill(trade_id="dup-1", market="BTC-USD", side="BUY",
                                price=Decimal("64000"), size=Decimal("0.001"),
                                liquidity="MAKER", fee=Decimal("0.05"),
                                ts=time.time())) is False
    assert pnl.book("BTC-USD").position == Decimal("0.001")


def test_out_of_order_terminal_before_ack_is_survivable(tmp_path: Path) -> None:
    """A terminal event may arrive before the REST ack it belongs to."""
    eng = make_engine(tmp_path)
    worker = eng.workers["BTC-USD"]
    worker.register(intent("b0002abcdef"))
    worker.on_terminal("b0002abcdef")
    worker.on_ack("b0002abcdef", "EX-2")  # late ack for a dead order
    assert "b0002abcdef" not in worker.quotes


def test_terminal_for_unknown_order_is_ignored(tmp_path: Path) -> None:
    eng = make_engine(tmp_path)
    eng.workers["BTC-USD"].on_terminal("never-seen")  # must not raise


def _noop(*_args, **_kwargs):
    async def _inner():
        return None
    return _inner()
