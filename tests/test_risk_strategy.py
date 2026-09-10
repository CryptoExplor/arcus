"""Risk guards and strategy invariants — the things that protect the funds."""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.book import BookState, MarketState  # noqa: E402
from arcusbot.config import Config  # noqa: E402
from arcusbot.pnl import Fill, PnLTracker  # noqa: E402
from arcusbot.risk import FLATTEN, HALT, OK, RiskManager  # noqa: E402
from arcusbot.sim import SIM_MARKETS  # noqa: E402
from arcusbot.strategy import MarketWorker  # noqa: E402


def make(**overrides) -> tuple[Config, MarketState, PnLTracker, RiskManager, MarketWorker]:
    cfg = Config.from_env(venue="sim", mode="dry-run", markets=["BTC-USD"], **overrides)
    meta = dict(SIM_MARKETS["BTC-USD"])
    book = BookState(market="BTC-USD")
    book.apply_snapshot({"bids": [["63990", "1"]], "asks": [["64010", "1"]],
                         "lastSequenceId": 1, "globalSequenceId": 1})
    state = MarketState(market="BTC-USD", market_id=1, meta=meta, book=book)
    state.mark_price = Decimal("64000")
    state.mark_updated_at = time.time()
    pnl = PnLTracker()
    risk = RiskManager(cfg, pnl)
    worker = MarketWorker(cfg=cfg, market="BTC-USD", state=state, pnl=pnl)
    return cfg, state, pnl, risk, worker


# --------------------------------------------------------------------- risk --


def test_position_cap_blocks_an_opening_order() -> None:
    cfg, state, _, risk, _ = make(max_position_notional_usd=Decimal("100"))
    ok, why = risk.check_order(state, "BUY", Decimal("0.01"), Decimal("64000"),
                               reduce_only=False, open_orders=0, position=Decimal(0))
    assert not ok and "cap" in why


def test_reduce_only_bypasses_the_position_cap() -> None:
    cfg, state, _, risk, _ = make(max_position_notional_usd=Decimal("100"))
    ok, _ = risk.check_order(state, "SELL", Decimal("0.01"), Decimal("64000"),
                             reduce_only=True, open_orders=0, position=Decimal("0.01"))
    assert ok


def test_reduce_only_still_allowed_after_the_kill_switch() -> None:
    """Halting must never trap the bot in a position it cannot close."""
    cfg, state, _, risk, _ = make()
    risk.halt("test")
    blocked, _ = risk.check_order(state, "BUY", Decimal("0.0001"), Decimal("64000"),
                                  reduce_only=False, open_orders=0, position=Decimal(0))
    allowed, _ = risk.check_order(state, "SELL", Decimal("0.0001"), Decimal("64000"),
                                  reduce_only=True, open_orders=0, position=Decimal("0.0001"))
    assert not blocked and allowed


def test_minimum_notional_is_enforced() -> None:
    cfg, state, _, risk, _ = make()
    ok, why = risk.check_order(state, "BUY", Decimal("0.00001"), Decimal("64000"),
                               reduce_only=False, open_orders=0, position=Decimal(0))
    assert not ok and "min" in why


def test_stale_prices_block_quoting() -> None:
    cfg, state, _, risk, _ = make(stale_price_s=1.0)
    state.mark_updated_at = time.time() - 30
    state.book.updated_at = time.time() - 30
    ok, why = risk.check_order(state, "BUY", Decimal("0.001"), Decimal("64000"),
                               reduce_only=False, open_orders=0, position=Decimal(0))
    assert not ok and "stale" in why


def test_off_hours_trading_band_blocks_prices_outside_it() -> None:
    cfg, state, _, risk, _ = make()
    state.upper_trading_bound = Decimal("64005")
    ok, why = risk.check_order(state, "BUY", Decimal("0.001"), Decimal("64100"),
                               reduce_only=False, open_orders=0, position=Decimal(0))
    assert not ok and "band" in why


def test_open_order_cap() -> None:
    cfg, state, _, risk, _ = make(max_open_orders=2)
    ok, why = risk.check_order(state, "BUY", Decimal("0.001"), Decimal("64000"),
                               reduce_only=False, open_orders=2, position=Decimal(0))
    assert not ok and "open orders" in why


def test_drawdown_triggers_the_kill_switch() -> None:
    cfg, _, pnl, risk, _ = make(max_drawdown_usd=Decimal("5"))
    pnl.record_fill(Fill("a", "BTC-USD", "BUY", Decimal("100"), Decimal("1"), "MAKER", Decimal(0), 0))
    pnl.record_fill(Fill("b", "BTC-USD", "SELL", Decimal("110"), Decimal("1"), "MAKER", Decimal(0), 0))
    pnl.record_fill(Fill("c", "BTC-USD", "BUY", Decimal("100"), Decimal("1"), "MAKER", Decimal(0), 0))
    pnl.record_fill(Fill("d", "BTC-USD", "SELL", Decimal("94"), Decimal("1"), "MAKER", Decimal(0), 0))
    assert risk.evaluate().state == HALT


def test_daily_loss_limit_triggers_the_kill_switch() -> None:
    cfg, _, pnl, risk, _ = make(max_daily_loss_usd=Decimal("3"))
    pnl.record_fill(Fill("a", "BTC-USD", "BUY", Decimal("100"), Decimal("1"), "TAKER", Decimal(0), 0))
    pnl.record_fill(Fill("b", "BTC-USD", "SELL", Decimal("95"), Decimal("1"), "TAKER", Decimal(0), 0))
    assert risk.evaluate().halted


def test_low_free_collateral_forces_flatten_not_halt() -> None:
    cfg, _, _, risk, _ = make(min_free_collateral_usd=Decimal("100"))
    risk.note_account({"freeCollateral": "10", "equity": "50"})
    verdict = risk.evaluate()
    assert verdict.state == FLATTEN and not verdict.can_open and verdict.can_trade


def test_volume_target_stops_the_run() -> None:
    cfg, _, pnl, risk, _ = make(volume_target_usd=Decimal("100"))
    pnl.record_fill(Fill("v", "BTC-USD", "BUY", Decimal("200"), Decimal("1"), "MAKER", Decimal(0), 0))
    assert risk.evaluate().halted


def test_consecutive_errors_halt() -> None:
    cfg, _, _, risk, _ = make(max_consecutive_errors=3)
    for _ in range(3):
        risk.note_error("boom")
    assert risk.evaluate().halted
    risk.halt_reason = None
    risk.note_success()
    assert risk.consecutive_errors == 0


def test_undercollateralized_rejection_throttles() -> None:
    cfg, _, _, risk, _ = make()
    risk.note_rejection("UNDERCOLLATERALIZED")
    assert risk.throttle_until > time.time()


# ----------------------------------------------------------------- strategy --


def test_quotes_never_cross_the_book() -> None:
    _, state, _, _, worker = make()
    for side, price, _ in worker.desired_quotes():
        if side == "BUY":
            assert price < state.book.best_ask
        else:
            assert price > state.book.best_bid


def test_quoted_spread_always_clears_the_fee_floor() -> None:
    """The core economic invariant: never quote a round trip that loses money."""
    _, state, pnl, _, worker = make(spread_bps=Decimal("1"))   # deliberately too tight
    quotes = {side: price for side, price, _ in worker.desired_quotes()}
    mid = state.reference_price
    realised_bps = (quotes["SELL"] - quotes["BUY"]) / mid * Decimal(10_000)
    assert realised_bps >= worker.required_edge_bps(maker_legs=2)


def test_spread_widens_with_volatility() -> None:
    _, state, _, _, worker = make()
    calm = worker.edge_bps()
    state.vol_bps = Decimal("25")
    state.vol_samples = 50
    assert worker.edge_bps() > calm


def test_inventory_skews_quotes_against_the_position() -> None:
    _, state, pnl, _, worker = make()
    flat = {s: p for s, p, _ in worker.desired_quotes()}
    pnl.book("BTC-USD").position = Decimal("0.001")      # long
    pnl.book("BTC-USD").avg_entry = Decimal("64000")
    longed = {s: p for s, p, _ in worker.desired_quotes()}
    assert longed["BUY"] < flat["BUY"]                    # less eager to buy more


def test_closing_side_is_reduce_only_and_covers_the_position() -> None:
    _, state, pnl, _, worker = make()
    pnl.book("BTC-USD").position = Decimal("0.001")
    pnl.book("BTC-USD").avg_entry = Decimal("64000")
    places = [i for i in worker.tick(can_open=True) if i.action == "place"]
    sells = [i for i in places if i.side == "SELL"]
    assert sells and all(i.reduce_only for i in sells)
    assert all(i.size >= Decimal("0.001") for i in sells)


def test_close_price_is_profitable_versus_entry() -> None:
    _, state, pnl, _, worker = make()
    pnl.book("BTC-USD").position = Decimal("0.001")
    pnl.book("BTC-USD").avg_entry = Decimal("64000")
    price = worker.close_price("SELL")
    edge = worker.required_edge_bps(maker_legs=2) / Decimal(10_000)
    assert price >= Decimal("64000") * (Decimal(1) + edge)


def test_short_close_price_is_below_entry() -> None:
    _, state, pnl, _, worker = make()
    pnl.book("BTC-USD").position = Decimal("-0.001")
    pnl.book("BTC-USD").avg_entry = Decimal("64000")
    assert worker.close_price("BUY") < Decimal("64000")


def test_stale_inventory_escalates_to_a_taker_close() -> None:
    _, state, pnl, _, worker = make(inventory_max_age_s=0.0)
    pnl.book("BTC-USD").position = Decimal("0.001")
    pnl.book("BTC-USD").avg_entry = Decimal("64000")
    worker.inventory_since = time.time() - 10
    places = [i for i in worker.flatten_intents() if i.action == "place"]
    assert places and places[0].tif == "IOC" and places[0].reduce_only


def test_oversized_inventory_escalates_immediately() -> None:
    _, state, pnl, _, worker = make(max_inventory_notional_usd=Decimal("1"))
    pnl.book("BTC-USD").position = Decimal("0.01")     # ~$640
    pnl.book("BTC-USD").avg_entry = Decimal("64000")
    worker.inventory_since = time.time()
    places = [i for i in worker.tick(can_open=True) if i.action == "place"]
    assert places and all(i.reduce_only for i in places)
    assert any(i.tif == "IOC" for i in places)


def test_flatten_cancels_quotes_that_would_add_to_the_position() -> None:
    _, state, pnl, _, worker = make()
    intents = worker.tick(can_open=True)
    for i in intents:
        if i.action == "place":
            worker.register(i)
    pnl.book("BTC-USD").position = Decimal("0.001")
    pnl.book("BTC-USD").avg_entry = Decimal("64000")
    worker.inventory_since = time.time()
    cancels = [i for i in worker.flatten_intents() if i.action == "cancel"]
    assert cancels


def test_no_quotes_when_not_allowed_to_open() -> None:
    _, _, _, _, worker = make()
    assert not [i for i in worker.tick(can_open=False) if i.action == "place"]


def test_crossed_book_suppresses_quoting() -> None:
    _, state, _, _, worker = make()
    state.book.apply_snapshot({"bids": [["64020", "1"]], "asks": [["64010", "1"]]})
    assert not [i for i in worker.tick(can_open=True) if i.action == "place"]


def test_order_size_meets_the_notional_floor() -> None:
    _, state, _, _, worker = make(order_notional_usd=Decimal("5"))
    for _, price, size in worker.desired_quotes():
        assert price * size >= Decimal("5")
