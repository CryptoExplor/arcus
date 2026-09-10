"""PnL / fee accounting — the numbers the whole objective is judged on."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcusbot.pnl import FeeSchedule, Fill, MarketPnL, PnLTracker, fills_from_ws  # noqa: E402

TIERS = {
    "tiers": [
        {"level": 0, "name": "Base", "volumeThreshold": 0, "makerFeePpm": 200, "takerFeePpm": 500},
        {"level": 1, "name": "VIP1", "volumeThreshold": 1_000_000, "makerFeePpm": -50, "takerFeePpm": 300},
    ]
}


def fill(side: str, price: str, size: str, liquidity: str = "MAKER", fee: str = "0", tid: str = "") -> Fill:
    return Fill(trade_id=tid or f"{side}{price}{size}", market="BTC-USD", side=side,
                price=Decimal(price), size=Decimal(size), liquidity=liquidity,
                fee=Decimal(fee), ts=0.0)


def test_fee_tier_selection_by_volume() -> None:
    assert FeeSchedule.from_fee_tiers(TIERS).level == 0
    vip = FeeSchedule.from_fee_tiers(TIERS, Decimal("2000000"))
    assert vip.level == 1 and vip.maker_ppm == Decimal(-50)


def test_ppm_to_bps_and_fee_amount() -> None:
    fees = FeeSchedule.from_fee_tiers(TIERS)
    assert fees.maker_bps == Decimal(2) and fees.taker_bps == Decimal(5)
    assert fees.fee_for(Decimal("10000"), "TAKER") == Decimal(5)   # 5 bps of 10k
    assert fees.fee_for(Decimal("10000"), "MAKER") == Decimal(2)


def test_maker_rebate_is_negative_fee() -> None:
    vip = FeeSchedule.from_fee_tiers(TIERS, Decimal("2000000"))
    assert vip.fee_for(Decimal("10000"), "MAKER") < 0


def test_round_trip_cost_by_leg_mix() -> None:
    fees = FeeSchedule.from_fee_tiers(TIERS)
    assert fees.round_trip_bps(maker_legs=2) == Decimal(4)    # 2+2
    assert fees.round_trip_bps(maker_legs=1) == Decimal(7)    # 2+5
    assert fees.round_trip_bps(maker_legs=0) == Decimal(10)   # 5+5


def test_realized_pnl_on_a_clean_round_trip() -> None:
    book = MarketPnL(market="BTC-USD")
    book.apply(fill("BUY", "100", "1"))
    book.apply(fill("SELL", "110", "1"))
    assert book.realized == Decimal(10) and book.position == 0


def test_short_round_trip_profits_when_price_falls() -> None:
    book = MarketPnL(market="BTC-USD")
    book.apply(fill("SELL", "100", "1"))
    book.apply(fill("BUY", "90", "1"))
    assert book.realized == Decimal(10) and book.position == 0


def test_average_entry_on_scale_in() -> None:
    book = MarketPnL(market="BTC-USD")
    book.apply(fill("BUY", "100", "1"))
    book.apply(fill("BUY", "200", "1"))
    assert book.avg_entry == Decimal(150)
    book.apply(fill("SELL", "150", "2"))
    assert book.realized == Decimal(0) and book.position == 0


def test_partial_close_leaves_entry_untouched() -> None:
    book = MarketPnL(market="BTC-USD")
    book.apply(fill("BUY", "100", "2"))
    book.apply(fill("SELL", "110", "1"))
    assert book.realized == Decimal(10)
    assert book.position == Decimal(1) and book.avg_entry == Decimal(100)


def test_flip_through_zero_resets_entry() -> None:
    book = MarketPnL(market="BTC-USD")
    book.apply(fill("BUY", "100", "1"))
    book.apply(fill("SELL", "110", "3"))       # close 1, open 2 short
    assert book.realized == Decimal(10)
    assert book.position == Decimal(-2) and book.avg_entry == Decimal(110)


def test_unrealized_tracks_the_mark() -> None:
    book = MarketPnL(market="BTC-USD")
    book.apply(fill("BUY", "100", "2"))
    assert book.unrealized(Decimal("105")) == Decimal(10)
    assert book.unrealized(None) == Decimal(0)


def test_net_pnl_identity_includes_fees_and_rebates() -> None:
    tracker = PnLTracker(FeeSchedule.from_fee_tiers(TIERS))
    tracker.record_fill(fill("BUY", "100", "1", "MAKER", "0.02", "t1"))
    tracker.record_fill(fill("SELL", "110", "1", "TAKER", "0.055", "t2"))
    expected = (tracker.realized() + tracker.unrealized() + tracker.funding()
                + tracker.total_rebates() - tracker.total_fees())
    assert tracker.net_pnl() == expected
    assert tracker.total_fees() == Decimal("0.075")


def test_rebates_credited_not_charged() -> None:
    tracker = PnLTracker()
    tracker.record_fill(fill("BUY", "100", "1", "MAKER", "-0.01", "r1"))
    assert tracker.total_rebates() == Decimal("0.01")
    assert tracker.total_fees() == Decimal(0)


def test_fills_are_idempotent_by_trade_id() -> None:
    """Snapshot + stream overlap must not double-count volume."""
    tracker = PnLTracker()
    assert tracker.record_fill(fill("BUY", "100", "1", tid="dup")) is True
    assert tracker.record_fill(fill("BUY", "100", "1", tid="dup")) is False
    assert tracker.total_volume() == Decimal(100)


def test_volume_and_maker_share() -> None:
    tracker = PnLTracker()
    tracker.record_fill(fill("BUY", "100", "1", "MAKER", tid="a"))
    tracker.record_fill(fill("SELL", "100", "1", "TAKER", tid="b"))
    assert tracker.total_volume() == Decimal(200)
    assert tracker.maker_volume() == Decimal(100)


def test_edge_required_covers_fees_plus_buffer() -> None:
    tracker = PnLTracker(FeeSchedule.from_fee_tiers(TIERS))
    assert tracker.edge_required_bps(Decimal(1), maker_legs=2) == Decimal(5)   # 4 + 1


def test_fee_coverage_ratio_flags_a_losing_session() -> None:
    tracker = PnLTracker()
    tracker.record_fill(fill("BUY", "100", "1", "MAKER", "0.5", "x"))
    tracker.record_fill(fill("SELL", "100.1", "1", "MAKER", "0.5", "y"))
    assert tracker.total_fees() == Decimal(1)
    assert tracker.fee_coverage() < 1          # edge did not cover the fees
    assert tracker.net_pnl() < 0


def test_drawdown_tracks_peak_to_trough() -> None:
    tracker = PnLTracker()
    tracker.record_fill(fill("BUY", "100", "1", "MAKER", "0", "1"))
    tracker.record_fill(fill("SELL", "120", "1", "MAKER", "0", "2"))   # +20
    tracker.record_fill(fill("BUY", "100", "1", "MAKER", "0", "3"))
    tracker.record_fill(fill("SELL", "90", "1", "MAKER", "0", "4"))    # -10
    assert tracker.drawdown() == Decimal(10)


def test_ws_fill_parsing_infers_fee_when_absent() -> None:
    fees = FeeSchedule.from_fee_tiers(TIERS)
    rows = list(fills_from_ws(
        {"tradeId": "t9", "market": "BTC-USD", "side": "BUY",
         "fillPrice": "100", "fillSize": "2", "liquidity": "MAKER"}, fees))
    assert len(rows) == 1
    assert rows[0].fee == fees.fee_for(Decimal(200), "MAKER")


def test_ws_fill_parsing_prefers_the_venue_fee() -> None:
    rows = list(fills_from_ws(
        {"tradeId": "t10", "market": "BTC-USD", "side": "SELL",
         "price": "100", "size": "1", "liquidity": "TAKER", "fee": "0.42"},
        FeeSchedule.from_fee_tiers(TIERS)))
    assert rows[0].fee == Decimal("0.42")
