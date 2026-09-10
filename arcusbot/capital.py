"""Capital allocation — "how much of my balance may this bot use?".

The bot has two sizing modes.

**fixed** (default)
    You state the clip size and position caps directly
    (``BOT_ORDER_NOTIONAL_USD``, ``BOT_MAX_POSITION_NOTIONAL_USD``, ...).
    Predictable and reproducible, but it ignores your actual balance: the same
    numbers on a $100 account and a $100,000 account mean very different risk.

**capital** (opt-in — set ``BOT_CAPITAL_USD`` or ``BOT_CAPITAL_PCT``)
    You state a budget — a dollar amount or a share of equity — and every
    sizing knob is derived from it. One number scales the whole bot, and it
    re-scales as the account grows or shrinks.

The derivation::

    deployable      = min(budget, equity − reserve)          # money at work
    exposure_budget = deployable × leverage × utilisation    # gross notional
    per_market      = exposure_budget ÷ market_count
    order_notional  = per_market ÷ clips                     # one clip
    max_position    = per_market                             # hard cap/market
    max_inventory   = per_market × inventory_fraction        # soft cap/market

``utilisation`` (default 0.5) is the deliberate gap between what the margin
engine would *allow* and what the bot actually deploys. Running at full
available leverage means a single adverse move liquidates you; half is a
working compromise between volume and survival.

Nothing here bypasses the risk manager — it produces the *inputs* the risk
manager then enforces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .config import Config
from .scaling import D, dec_str

log = logging.getLogger("arcusbot.capital")

VENUE_MIN_NOTIONAL = Decimal("5")   # Arcus minimum order notional


@dataclass(slots=True)
class Allocation:
    """The resolved sizing plan for one session."""

    mode: str                       # "fixed" | "capital" | "unfunded"
    equity: Decimal | None
    reserve_usd: Decimal
    budget_usd: Decimal             # what the operator asked to deploy
    deployable_usd: Decimal         # what is actually available after reserve
    exposure_budget_usd: Decimal    # gross notional the bot may run
    per_market_usd: Decimal
    order_notional_usd: Decimal
    max_position_notional_usd: Decimal
    max_inventory_notional_usd: Decimal
    max_drawdown_usd: Decimal
    min_free_collateral_usd: Decimal
    market_count: int
    sufficient: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def utilisation_pct(self) -> Decimal:
        if not self.equity:
            return Decimal(0)
        return (self.deployable_usd / self.equity) * 100

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "equity": dec_str(self.equity) if self.equity is not None else None,
            "reserveUsd": dec_str(self.reserve_usd),
            "budgetUsd": dec_str(self.budget_usd),
            "deployableUsd": dec_str(self.deployable_usd),
            "deployablePctOfEquity": dec_str(self.utilisation_pct.quantize(Decimal("0.01"))),
            "exposureBudgetUsd": dec_str(self.exposure_budget_usd),
            "perMarketUsd": dec_str(self.per_market_usd),
            "orderNotionalUsd": dec_str(self.order_notional_usd),
            "maxPositionNotionalUsd": dec_str(self.max_position_notional_usd),
            "maxInventoryNotionalUsd": dec_str(self.max_inventory_notional_usd),
            "maxDrawdownUsd": dec_str(self.max_drawdown_usd),
            "minFreeCollateralUsd": dec_str(self.min_free_collateral_usd),
            "marketCount": self.market_count,
            "sufficient": self.sufficient,
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        if self.mode == "fixed":
            return (
                f"sizing: fixed — clip ${dec_str(self.order_notional_usd)}, "
                f"max position ${dec_str(self.max_position_notional_usd)}/market"
            )
        eq = dec_str(self.equity) if self.equity is not None else "unknown"
        return (
            f"sizing: capital — equity ${eq}, deploying "
            f"${dec_str(self.deployable_usd)} ({dec_str(self.utilisation_pct.quantize(Decimal('0.1')))}%), "
            f"reserve ${dec_str(self.reserve_usd)} | exposure budget "
            f"${dec_str(self.exposure_budget_usd)} across {self.market_count} market(s) -> "
            f"clip ${dec_str(self.order_notional_usd)}, max position "
            f"${dec_str(self.max_position_notional_usd)}/market"
        )


def capital_mode_enabled(cfg: Config) -> bool:
    """Capital sizing activates only when the operator asks for it."""
    return cfg.capital_usd > 0 or cfg.capital_pct > 0


def _fixed_allocation(cfg: Config, equity: Decimal | None, market_count: int,
                      notes: list[str]) -> Allocation:
    return Allocation(
        mode="fixed",
        equity=equity,
        reserve_usd=cfg.reserve_usd,
        budget_usd=Decimal(0),
        deployable_usd=Decimal(0),
        exposure_budget_usd=cfg.max_position_notional_usd * market_count,
        per_market_usd=cfg.max_position_notional_usd,
        order_notional_usd=cfg.order_notional_usd,
        max_position_notional_usd=cfg.max_position_notional_usd,
        max_inventory_notional_usd=cfg.max_inventory_notional_usd,
        max_drawdown_usd=cfg.max_drawdown_usd,
        min_free_collateral_usd=cfg.min_free_collateral_usd,
        market_count=market_count,
        sufficient=True,
        notes=notes,
    )


def plan_capital(cfg: Config, equity: Decimal | None, market_count: int) -> Allocation:
    """Resolve the sizing plan for the current equity.

    Never raises: an unusable plan comes back with ``sufficient=False`` and an
    explanation in ``notes`` so the caller can refuse to trade loudly.
    """
    market_count = max(1, int(market_count))
    notes: list[str] = []

    if not capital_mode_enabled(cfg):
        return _fixed_allocation(cfg, equity, market_count, notes)

    if equity is None or equity <= 0:
        notes.append(
            "capital sizing requested but account equity is unknown — "
            "falling back to the fixed notionals. Fund the account, or run "
            "with --venue sim."
        )
        alloc = _fixed_allocation(cfg, equity, market_count, notes)
        alloc.mode = "unfunded"
        return alloc

    # ---- how much money is the bot allowed to work with? -------------------
    by_pct = equity * cfg.capital_pct / 100 if cfg.capital_pct > 0 else None
    by_abs = cfg.capital_usd if cfg.capital_usd > 0 else None
    if by_pct is not None and by_abs is not None:
        # Both given: the stricter wins. A percentage is a growth rule; an
        # absolute is a hard ceiling. Honour both.
        budget = min(by_pct, by_abs)
        notes.append(
            f"both BOT_CAPITAL_USD (${dec_str(by_abs)}) and BOT_CAPITAL_PCT "
            f"({dec_str(cfg.capital_pct)}% = ${dec_str(by_pct)}) set — using the lower"
        )
    else:
        budget = by_abs if by_abs is not None else (by_pct or Decimal(0))

    spendable = equity - cfg.reserve_usd
    deployable = min(budget, spendable)
    if deployable < budget:
        notes.append(
            f"budget ${dec_str(budget)} trimmed to ${dec_str(max(deployable, Decimal(0)))} "
            f"by the ${dec_str(cfg.reserve_usd)} reserve"
        )
    deployable = max(Decimal(0), deployable)

    # ---- turn money into position limits ------------------------------------
    leverage = Decimal(max(1, cfg.leverage))
    utilisation = cfg.capital_utilisation
    exposure_budget = deployable * leverage * utilisation
    per_market = exposure_budget / market_count
    clips = Decimal(max(1, cfg.capital_clips))

    order_notional = per_market / clips
    max_position = per_market
    max_inventory = per_market * cfg.capital_inventory_fraction

    sufficient = True
    if order_notional < VENUE_MIN_NOTIONAL:
        if per_market >= VENUE_MIN_NOTIONAL:
            notes.append(
                f"clip ${dec_str(order_notional.quantize(Decimal('0.01')))} below the "
                f"${dec_str(VENUE_MIN_NOTIONAL)} venue minimum — raised to "
                f"${dec_str(VENUE_MIN_NOTIONAL)} (fewer clips per market)"
            )
            order_notional = VENUE_MIN_NOTIONAL
        else:
            sufficient = False
            notes.append(
                f"${dec_str(deployable)} deployable across {market_count} market(s) at "
                f"{dec_str(leverage)}x cannot fund even one ${dec_str(VENUE_MIN_NOTIONAL)} "
                f"clip. Increase BOT_CAPITAL_USD/PCT, lower BOT_RESERVE_USD, "
                f"or trade fewer markets."
            )
            order_notional = VENUE_MIN_NOTIONAL

    if max_position < order_notional:
        max_position = order_notional
    if max_inventory < order_notional:
        max_inventory = order_notional

    # ---- scale the loss limits to the money at risk -------------------------
    if cfg.max_drawdown_pct > 0 and deployable > 0:
        max_drawdown = deployable * cfg.max_drawdown_pct / 100
        notes.append(
            f"drawdown limit ${dec_str(max_drawdown.quantize(Decimal('0.01')))} "
            f"= {dec_str(cfg.max_drawdown_pct)}% of deployed capital"
        )
    else:
        max_drawdown = cfg.max_drawdown_usd

    # The reserve is only real if the bot refuses to trade into it.
    min_free = max(cfg.min_free_collateral_usd, cfg.reserve_usd)

    return Allocation(
        mode="capital",
        equity=equity,
        reserve_usd=cfg.reserve_usd,
        budget_usd=budget,
        deployable_usd=deployable,
        exposure_budget_usd=exposure_budget,
        per_market_usd=per_market,
        order_notional_usd=_round_money(order_notional),
        max_position_notional_usd=_round_money(max_position),
        max_inventory_notional_usd=_round_money(max_inventory),
        max_drawdown_usd=_round_money(max_drawdown),
        min_free_collateral_usd=_round_money(min_free),
        market_count=market_count,
        sufficient=sufficient,
        notes=notes,
    )


def _round_money(value: Decimal) -> Decimal:
    return D(value).quantize(Decimal("0.01"))


def apply_allocation(cfg: Config, alloc: Allocation) -> None:
    """Write a capital plan back into the live config.

    ``fixed``/``unfunded`` plans are no-ops: they were built *from* the config.
    """
    if alloc.mode != "capital":
        return
    cfg.order_notional_usd = alloc.order_notional_usd
    cfg.max_position_notional_usd = alloc.max_position_notional_usd
    cfg.max_inventory_notional_usd = alloc.max_inventory_notional_usd
    cfg.max_drawdown_usd = alloc.max_drawdown_usd
    cfg.min_free_collateral_usd = alloc.min_free_collateral_usd


def needs_resize(alloc: Allocation, equity: Decimal | None, threshold_pct: Decimal) -> bool:
    """Has equity moved far enough to justify re-sizing mid-session?

    Re-sizing on every tick would make position caps jitter under the strategy;
    re-sizing never means a bot that doubled its balance keeps trading tiny, and
    one that halved keeps trading too large. A threshold gives both.
    """
    if alloc.mode != "capital" or equity is None or threshold_pct <= 0:
        return False
    if alloc.equity is None or alloc.equity <= 0:
        return True
    drift = abs(equity - alloc.equity) / alloc.equity * 100
    return drift >= threshold_pct
