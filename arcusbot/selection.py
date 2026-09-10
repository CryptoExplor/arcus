"""Market selection — deciding *where* to quote before deciding *how*.

Enabling every market a venue lists is a common way to lose money quietly: a
thin market with a wide tick and a $5 minimum forces oversized clips, and an
account too small to hold one clip per market ends up with none of them
properly hedged. This module scores markets on the properties that actually
determine whether a maker strategy can work there, and refuses the ones that
cannot.

Scoring is deliberately transparent — every candidate carries the reasons it
was scored the way it was, so `arcus markets --rank` explains itself rather
than emitting an opaque number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .scaling import D, dec_str

# Weights sum to 1.0. Spread and liquidity dominate: they decide whether the
# maker edge exists at all. Volatility is a two-sided term — some movement is
# needed for the spread to be re-crossed, too much is adverse selection.
WEIGHTS = {
    "spread": Decimal("0.34"),
    "liquidity": Decimal("0.26"),
    "volatility": Decimal("0.18"),
    "affordability": Decimal("0.14"),
    "tick": Decimal("0.08"),
}

# A market whose minimum clip eats more than this share of the per-market
# budget cannot be traded with any position granularity: one fill is the whole
# allocation, leaving nothing to average or scale out with.
MAX_MIN_CLIP_SHARE = Decimal("0.5")

# Beyond a handful of markets the operational cost (subscriptions, inventory,
# rate-limit weight) grows faster than the edge of the next-best market.
MAX_CONCURRENT_MARKETS = 8


@dataclass
class Candidate:
    """A market with its score and the reasoning behind it."""

    market: str
    market_id: int
    score: Decimal = Decimal("0")
    tradable: bool = True
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def reject(self, reason: str) -> "Candidate":
        self.tradable = False
        self.score = Decimal("0")
        self.reasons.append(reason)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "marketId": self.market_id,
            "score": dec_str(self.score.quantize(Decimal("0.0001"))),
            "tradable": self.tradable,
            "reasons": self.reasons,
            "metrics": self.metrics,
        }


def _clamp(value: Decimal, low: Decimal = Decimal("0"), high: Decimal = Decimal("1")) -> Decimal:
    return max(low, min(high, value))


def score_market(
    meta: dict[str, Any],
    *,
    per_market_budget: Decimal,
    spread_bps: Decimal | None = None,
    depth_usd: Decimal | None = None,
    vol_bps: Decimal | None = None,
    required_edge_bps: Decimal = Decimal("3"),
) -> Candidate:
    """Score one market. Higher is better; 0 means "do not trade".

    ``spread_bps``/``depth_usd``/``vol_bps`` come from live book observation
    when available. When they are missing the market is scored on static
    metadata alone and flagged, rather than being assumed good.
    """
    name = str(meta.get("marketDisplayName") or meta.get("market") or "?")
    cand = Candidate(market=name, market_id=int(meta.get("marketId") or 0))

    status = str(meta.get("status") or "").upper()
    if status and status != "ONLINE":
        return cand.reject(f"status is {status}, not ONLINE")

    mark = D(meta.get("markPrice") or "0")
    if mark <= 0:
        # "0" documented as "mark unavailable" — never fall back to oracle.
        return cand.reject("no mark price available")

    min_notional = D(meta.get("minOrderNotional") or "5")
    min_size = D(meta.get("minOrderSize") or "0")
    min_clip = max(min_notional, min_size * mark)
    cand.metrics["minClipUsd"] = dec_str(min_clip)
    cand.metrics["perMarketBudgetUsd"] = dec_str(per_market_budget)

    if per_market_budget > 0 and min_clip > per_market_budget:
        return cand.reject(
            f"minimum clip ${dec_str(min_clip)} exceeds the per-market budget "
            f"${dec_str(per_market_budget)}"
        )

    share = (min_clip / per_market_budget) if per_market_budget > 0 else Decimal("1")
    if share > MAX_MIN_CLIP_SHARE:
        return cand.reject(
            f"minimum clip is {dec_str((share * 100).quantize(Decimal('0.1')))}% of the "
            f"per-market budget — no room to scale or average"
        )

    # --- spread: the raw material of a maker strategy -----------------------
    if spread_bps is None:
        cand.reasons.append("no live spread observed; scored on metadata only")
        spread_score = Decimal("0.3")
    elif spread_bps <= required_edge_bps:
        return cand.reject(
            f"spread {dec_str(spread_bps)}bps does not cover the required edge "
            f"{dec_str(required_edge_bps)}bps"
        )
    else:
        # Saturates: 3x the required edge is already excellent.
        headroom = (spread_bps - required_edge_bps) / max(required_edge_bps, Decimal("0.1"))
        spread_score = _clamp(headroom / Decimal("2"))
        cand.metrics["spreadBps"] = dec_str(spread_bps)
        cand.metrics["edgeHeadroomBps"] = dec_str(spread_bps - required_edge_bps)

    # --- liquidity: can our clip rest without being the whole book? ---------
    if depth_usd is None:
        liquidity_score = Decimal("0.3")
    else:
        clip = per_market_budget if per_market_budget > 0 else min_clip
        ratio = depth_usd / max(clip, Decimal("1"))
        liquidity_score = _clamp(ratio / Decimal("50"))
        cand.metrics["depthUsd"] = dec_str(depth_usd)
        if ratio < 3:
            cand.reasons.append(
                f"thin: top-of-book depth is only {dec_str(ratio.quantize(Decimal('0.1')))}x our clip"
            )

    # --- volatility: enough to trade, not enough to run us over -------------
    if vol_bps is None:
        vol_score = Decimal("0.3")
    else:
        cand.metrics["volBps"] = dec_str(vol_bps)
        if vol_bps <= 0:
            vol_score = Decimal("0")
            cand.reasons.append("no observed movement; spread may never be re-crossed")
        elif vol_bps > spread_bps * 3 if spread_bps else False:
            vol_score = Decimal("0.1")
            cand.reasons.append(
                f"volatility {dec_str(vol_bps)}bps dwarfs the spread — adverse selection risk"
            )
        else:
            # Peak usefulness around 1x the required edge per interval.
            vol_score = _clamp(vol_bps / max(required_edge_bps * 2, Decimal("0.1")))

    # --- affordability: how many clips fit in the budget --------------------
    clips = (per_market_budget / min_clip) if min_clip > 0 else Decimal("0")
    affordability = _clamp(clips / Decimal("6"))
    cand.metrics["clipsAffordable"] = dec_str(clips.quantize(Decimal("0.1")))

    # --- tick granularity: a coarse tick quantises the edge away ------------
    tick = D(meta.get("tickSize") or "0")
    tick_bps = (tick / mark * 10_000) if mark > 0 else Decimal("0")
    cand.metrics["tickBps"] = dec_str(tick_bps.quantize(Decimal("0.01")))
    if tick_bps >= required_edge_bps:
        return cand.reject(
            f"tick {dec_str(tick_bps.quantize(Decimal('0.01')))}bps is coarser than the "
            f"required edge — cannot price inside it"
        )
    tick_score = _clamp(Decimal("1") - (tick_bps / max(required_edge_bps, Decimal("0.1"))))

    cand.score = (
        WEIGHTS["spread"] * spread_score
        + WEIGHTS["liquidity"] * liquidity_score
        + WEIGHTS["volatility"] * vol_score
        + WEIGHTS["affordability"] * affordability
        + WEIGHTS["tick"] * tick_score
    )
    return cand


def rank_markets(
    metas: Iterable[dict[str, Any]],
    *,
    per_market_budget: Decimal,
    observations: dict[str, dict[str, Decimal]] | None = None,
    required_edge_bps: Decimal = Decimal("3"),
) -> list[Candidate]:
    """Score every market and return them best-first."""
    obs = observations or {}
    out: list[Candidate] = []
    for meta in metas:
        name = str(meta.get("marketDisplayName") or meta.get("market") or "?")
        o = obs.get(name, {})
        out.append(score_market(
            meta,
            per_market_budget=per_market_budget,
            spread_bps=o.get("spreadBps"),
            depth_usd=o.get("depthUsd"),
            vol_bps=o.get("volBps"),
            required_edge_bps=required_edge_bps,
        ))
    out.sort(key=lambda c: (c.tradable, c.score), reverse=True)
    return out


def max_markets_for_capital(deployable: Decimal, min_clip_usd: Decimal = Decimal("5")) -> int:
    """How many markets an account this size can actually support.

    Spreading a small account across many markets is the failure mode this
    guards against: each market needs enough budget for a few clips, otherwise
    every fill is all-or-nothing. A $20 account gets exactly one market.
    """
    if deployable <= 0:
        return 0
    # Require room for ~3 clips per market before adding another one.
    per_market_floor = min_clip_usd * 3
    if deployable < per_market_floor:
        return 1 if deployable >= min_clip_usd else 0
    # Capital allows more, but attention does not: every extra market adds
    # book subscriptions, inventory to manage and rate-limit weight, while the
    # marginal edge of the Nth-best market keeps falling. Concentrating on the
    # best few beats spreading thin, so growth is logarithmic and capped.
    affordable = int(deployable / per_market_floor)
    return max(1, min(affordable, MAX_CONCURRENT_MARKETS))


def select_markets(
    metas: Iterable[dict[str, Any]],
    *,
    deployable: Decimal,
    requested: list[str] | None = None,
    observations: dict[str, dict[str, Decimal]] | None = None,
    required_edge_bps: Decimal = Decimal("3"),
    limit: int | None = None,
    min_clip_usd: Decimal = Decimal("5"),
) -> tuple[list[str], list[Candidate]]:
    """Pick the markets to trade. Returns ``(chosen, all_candidates)``.

    ``requested`` restricts the universe to an operator-chosen list — the
    ranking then acts as a veto, not an expansion: a market the operator asked
    for is still dropped if it is untradable for this account size.
    """
    metas = list(metas)
    if requested:
        wanted = {m.upper() for m in requested}
        metas = [m for m in metas
                 if str(m.get("marketDisplayName") or "").upper() in wanted]

    cap = limit if limit is not None else max_markets_for_capital(deployable, min_clip_usd)
    cap = max(cap, 0)
    # Budget per market depends on how many we end up with, so size against the
    # cap rather than the full universe.
    per_market = (deployable / cap) if cap > 0 else Decimal("0")

    ranked = rank_markets(metas, per_market_budget=per_market,
                          observations=observations,
                          required_edge_bps=required_edge_bps)
    chosen = [c.market for c in ranked if c.tradable][:cap]
    return chosen, ranked
