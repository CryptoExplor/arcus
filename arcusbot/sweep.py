"""Parameter sweep / edge measurement against the offline simulator.

A single short run tells you almost nothing: maker PnL is dominated by which
side of a random walk you happened to be filled on. This runs the same config
over several seeds and reports the mean and spread, which is the only honest
way to decide whether a spread setting actually clears fees.

    python -m arcusbot sweep --sweep-spreads 4,10,25 --sweep-seeds 4

Read the output as: "at this spread, net bps of volume was X +/- Y". If the
mean is negative, the configuration is paying to trade — widen the spread,
reduce the taker escalation, or accept that the venue's flow is too toxic.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from .config import Config
from .engine import Engine


@dataclass(slots=True)
class SweepResult:
    spread_bps: float
    seeds: int
    volume: float
    net: float
    fees: float
    net_bps_mean: float
    net_bps_stdev: float
    maker_share: float
    fills: int
    profitable_runs: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "spreadBps": self.spread_bps,
            "seeds": self.seeds,
            "volumeUsd": round(self.volume, 2),
            "netPnl": round(self.net, 4),
            "feesPaid": round(self.fees, 4),
            "netBpsOfVolumeMean": round(self.net_bps_mean, 3),
            "netBpsOfVolumeStdev": round(self.net_bps_stdev, 3),
            "makerSharePct": round(self.maker_share, 1),
            "fills": self.fills,
            "profitableRuns": self.profitable_runs,
        }


async def _one_run(base: Config, spread: float, seed: int, duration: int) -> dict[str, float]:
    cfg = Config.from_env(
        venue="sim",
        mode="live",
        markets=base.markets,
        strategy=base.strategy,
        order_notional_usd=base.order_notional_usd,
        spread_bps=Decimal(str(spread)),
        max_runtime_s=duration,
        sim_seed=seed,
        sim_uninformed_rate=base.sim_uninformed_rate,
        sim_vol_bps=base.sim_vol_bps,
        sim_speed=max(base.sim_speed, 5.0),   # compress wall-clock
        loop_interval_s=0.2,
        report_interval_s=10**9,
        log_level="CRITICAL",
        cancel_all_on_exit=True,
        print_summary=False,
        persist_state=False,   # sweeps must never touch real risk state
    )
    engine = Engine(cfg)
    await engine.run()
    pnl = engine.pnl
    volume = float(pnl.total_volume())
    return {
        "volume": volume,
        "net": float(pnl.net_pnl()),
        "fees": float(pnl.total_fees()),
        "net_bps": float(pnl.bps_per_volume()),
        "maker_share": float(pnl.maker_volume() / pnl.total_volume() * 100) if volume else 0.0,
        "fills": sum(b.fill_count for b in pnl.books.values()),
    }


async def run_sweep(
    base: Config,
    spreads: Sequence[float],
    seeds: int = 4,
    duration: int = 20,
) -> list[SweepResult]:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        results: list[SweepResult] = []
        for spread in spreads:
            runs = [await _one_run(base, spread, seed, duration) for seed in range(1, seeds + 1)]
            bps = [r["net_bps"] for r in runs]
            results.append(
                SweepResult(
                    spread_bps=spread,
                    seeds=seeds,
                    volume=sum(r["volume"] for r in runs),
                    net=sum(r["net"] for r in runs),
                    fees=sum(r["fees"] for r in runs),
                    net_bps_mean=statistics.fmean(bps),
                    net_bps_stdev=statistics.pstdev(bps) if len(bps) > 1 else 0.0,
                    maker_share=statistics.fmean([r["maker_share"] for r in runs]),
                    fills=sum(r["fills"] for r in runs),
                    profitable_runs=sum(1 for r in runs if r["net"] > 0),
                )
            )
        return results
    finally:
        logging.disable(previous)


def format_table(results: list[SweepResult]) -> str:
    lines = [
        f"{'spread':>7} {'volume':>11} {'net':>10} {'fees':>9} "
        f"{'net bps':>10} {'stdev':>8} {'maker%':>7} {'fills':>6} {'win':>5}",
        "-" * 82,
    ]
    for r in results:
        lines.append(
            f"{r.spread_bps:>7.1f} {r.volume:>11.2f} {r.net:>10.4f} {r.fees:>9.4f} "
            f"{r.net_bps_mean:>10.2f} {r.net_bps_stdev:>8.2f} {r.maker_share:>7.1f} "
            f"{r.fills:>6} {r.profitable_runs}/{r.seeds:<3}"
        )
    best = max(results, key=lambda r: r.net_bps_mean) if results else None
    if best:
        verdict = "clears fees" if best.net_bps_mean > 0 else "DOES NOT clear fees"
        lines += [
            "-" * 82,
            f"best: {best.spread_bps:.1f} bps spread -> {best.net_bps_mean:+.2f} bps of volume "
            f"(+/-{best.net_bps_stdev:.2f}) — {verdict}",
            "note: simulator results are indicative only; confirm on testnet before scaling.",
        ]
    return "\n".join(lines)


def sweep_command(base: Config, spreads: Sequence[float], seeds: int, duration: int) -> list[SweepResult]:
    return asyncio.run(run_sweep(base, spreads, seeds, duration))
