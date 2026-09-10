"""Command line interface: `python -m arcusbot <command>`.

Commands
--------
run        start the trading loop (venue/mode from env or flags)
sweep      measure net bps of volume across spreads / seeds (offline)
preflight  read-only checks: connectivity, key, markets, fees, balance, sizing
markets    list tradable markets with tick/step/limits
quote      show the quotes the strategy *would* place right now (no orders)
report     print/refresh the latest PnL report
selftest   run the offline simulator for N seconds and assert the invariants
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from decimal import Decimal
from typing import Any

from .config import Config
from .dashboard import start_dashboard
from .engine import Engine
from .pnl import FeeSchedule
from .rest import ArcusError, ArcusREST
from .scaling import dec_str
from .signing import Signer
from .sim import SIM_FEE_TIERS


def setup_logging(cfg: Config) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = cfg.log_dir / f"bot-{time.strftime('%Y%m%d')}.log"
    handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m arcusbot", description="Arcus testnet trading bot")
    p.add_argument("command",
                   choices=["run", "preflight", "markets", "quote", "report", "selftest", "sweep"])
    p.add_argument("--venue", choices=["arcus", "sim"], help="arcus (real API) or sim (offline)")
    p.add_argument("--mode", choices=["dry-run", "live"], help="dry-run signs nothing, live sends orders")
    p.add_argument("--strategy", choices=["volume-maker", "ping-pong", "spot-rfq"])
    p.add_argument("--markets", help="comma-separated market list, e.g. BTC-USD,ETH-USD")
    p.add_argument("--notional", type=str, help="per-order notional in USD")
    p.add_argument("--spread-bps", type=str, help="target maker spread in bps")
    p.add_argument("--duration", type=int, help="max runtime in seconds (0 = unlimited)")
    p.add_argument("--volume-target", type=str, help="stop after this much USD volume")
    p.add_argument("--port", type=int, help="dashboard port (0 disables)")
    p.add_argument("--spot", action="store_true", help="enable the spot RFQ leg")
    p.add_argument("--log-level", help="DEBUG/INFO/WARNING")
    p.add_argument("--json", action="store_true", help="machine-readable output where applicable")
    p.add_argument("--sweep-spreads", default="4,8,12,20", help="spreads in bps to test (sweep)")
    p.add_argument("--sweep-seeds", type=int, default=4, help="runs per spread (sweep)")
    p.add_argument("--sweep-duration", type=int, default=20, help="seconds per run (sweep)")
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}
    if args.venue:
        overrides["venue"] = args.venue
    if args.mode:
        overrides["mode"] = args.mode
    if args.strategy:
        overrides["strategy"] = args.strategy
    if args.markets:
        overrides["markets"] = [m.strip() for m in args.markets.split(",") if m.strip()]
    if args.notional:
        overrides["order_notional_usd"] = Decimal(args.notional)
    if args.spread_bps:
        overrides["spread_bps"] = Decimal(args.spread_bps)
    if args.duration is not None:
        overrides["max_runtime_s"] = args.duration
    if args.volume_target:
        overrides["volume_target_usd"] = Decimal(args.volume_target)
    if args.port is not None:
        overrides["metrics_port"] = args.port
    if args.spot:
        overrides["enable_spot"] = True
    if args.log_level:
        overrides["log_level"] = args.log_level.upper()
    return Config.from_env(**overrides)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_preflight(cfg: Config, as_json: bool) -> int:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, fatal: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "fatal": fatal, "detail": detail})

    problems = cfg.validate()
    check("config", not problems, "; ".join(problems) or "valid")

    if cfg.venue == "sim":
        check("venue", True, "offline simulator — no network required", fatal=False)
        fees = FeeSchedule.from_fee_tiers(SIM_FEE_TIERS)
        check("fees", True, f"sim tier {fees.level}: {fees.maker_bps}/{fees.taker_bps} bps", fatal=False)
    else:
        signer = Signer(cfg.api_secret) if cfg.api_secret else None
        rest = ArcusREST(cfg, signer)
        try:
            rest.health()
            check("gateway", True, f"{cfg.rest_url} reachable")
        except ArcusError as exc:
            check("gateway", False, str(exc))

        try:
            markets = rest.markets(refresh=True)
            wanted = [m for m in cfg.markets if m in markets]
            check("markets", bool(wanted),
                  f"{len(markets)} markets; trading {wanted or 'NONE — check BOT_MARKETS'}")
            for name in wanted:
                m = markets[name]
                check(f"grid:{name}", True,
                      f"tick {m['tickSize']} step {m['stepSize']} "
                      f"minNotional {m.get('minOrderNotional')} status {m.get('status')}",
                      fatal=False)
        except ArcusError as exc:
            check("markets", False, str(exc))

        try:
            fees = FeeSchedule.from_fee_tiers(rest.fee_tiers())
            check("fees", True,
                  f"tier {fees.level} {fees.name}: maker {dec_str(fees.maker_bps)} bps / "
                  f"taker {dec_str(fees.taker_bps)} bps", fatal=False)
            required = fees.round_trip_bps(maker_legs=2) + cfg.fee_buffer_bps
            check("edge", cfg.spread_bps >= required,
                  f"target spread {dec_str(cfg.spread_bps)} bps vs required "
                  f"{dec_str(required)} bps (maker/maker + buffer)", fatal=False)
        except ArcusError as exc:
            check("fees", False, f"{exc} (will fall back to conservative defaults)", fatal=False)

        if signer:
            check("apiKey", True, f"public key {signer.api_key[:12]}…")
            if cfg.address:
                try:
                    account = rest.account()
                    equity = Decimal(str(account.get("equity", account.get("accountEquity", 0))))
                    free = Decimal(str(account.get("freeCollateral", 0)))
                    enough = free >= cfg.min_free_collateral_usd
                    check("balance", enough,
                          f"equity ${dec_str(equity)} free ${dec_str(free)} "
                          f"(floor ${dec_str(cfg.min_free_collateral_usd)})")
                except ArcusError as exc:
                    if exc.status == 404:
                        check("balance", False,
                              "account has no activity yet — use the Testnet Deposit button "
                              "or tools/fund_testnet.py")
                    else:
                        check("balance", False, str(exc))
                try:
                    keys = rest.api_keys()
                    rows = keys.get("apiKeys", []) if isinstance(keys, dict) else []
                    live = any(k.get("apiKey") == signer.api_key for k in rows)
                    check("keyRegistered", live,
                          "key is registered to this address" if live
                          else "key NOT found for this address — register it first")
                except ArcusError as exc:
                    check("keyRegistered", False, str(exc), fatal=False)
        else:
            check("apiKey", cfg.mode != "live",
                  "no ARCUS_API_SECRET set (fine for dry-run, required for live)")

    fatal = [c for c in checks if not c["ok"] and c["fatal"]]
    if as_json:
        print(json.dumps({"ok": not fatal, "checks": checks}, indent=2))
    else:
        print(f"\nPreflight — venue={cfg.venue} network={cfg.network} mode={cfg.mode}\n" + "-" * 72)
        for c in checks:
            mark = "PASS" if c["ok"] else ("FAIL" if c["fatal"] else "WARN")
            print(f"  [{mark}] {c['check']:<16} {c['detail']}")
        print("-" * 72)
        print("READY" if not fatal else f"NOT READY — {len(fatal)} blocking issue(s)")
    return 0 if not fatal else 1


def cmd_markets(cfg: Config, as_json: bool) -> int:
    if cfg.venue == "sim":
        from .sim import SIM_MARKETS
        markets = SIM_MARKETS
    else:
        markets = ArcusREST(cfg, Signer(cfg.api_secret) if cfg.api_secret else None).markets(refresh=True)
    rows = sorted(markets.values(), key=lambda m: int(m["marketId"]))
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'id':>4}  {'market':<14} {'status':<8} {'tick':>10} {'step':>12} "
          f"{'minNotional':>12} {'mark':>14}")
    for m in rows:
        print(f"{m['marketId']:>4}  {m['marketDisplayName']:<14} {str(m.get('status','')):<8} "
              f"{str(m.get('tickSize','')):>10} {str(m.get('stepSize','')):>12} "
              f"{str(m.get('minOrderNotional','')):>12} {str(m.get('markPrice',''))[:14]:>14}")
    print(f"\n{len(rows)} markets")
    return 0


async def cmd_quote(cfg: Config, as_json: bool) -> int:
    """Show the intents the strategy would emit right now — places nothing."""
    cfg.mode = "dry-run"
    engine = Engine(cfg)
    await engine.setup()
    if cfg.venue == "sim":
        engine._pump_sim()
    else:
        await asyncio.sleep(3)  # let the ws snapshots land
    out = []
    for worker in engine.workers.values():
        for intent in worker.tick(can_open=True):
            out.append(intent.describe())
    if engine.ws:
        await engine.ws.stop()
    if as_json:
        print(json.dumps({"intents": out, "markets": [w.snapshot() for w in engine.workers.values()]},
                         indent=2))
    else:
        for w in engine.workers.values():
            s = w.snapshot()
            print(f"{s['market']:<12} bid {s['bestBid']} ask {s['bestAsk']} "
                  f"spread {s['spreadBps']} bps | required edge {s['requiredEdgeBps']} bps")
        print("\nintents:")
        for line in out or ["  (none — market data not ready or edge insufficient)"]:
            print("  " + line)
    return 0


def cmd_report(cfg: Config, as_json: bool) -> int:
    path = cfg.state_dir / "report-latest.json"
    if not path.is_file():
        print(f"no report yet at {path}")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"session {data['sessionId']} · runtime {data['runtimeSeconds']}s")
    print(f"volume    ${data['volumeUsd']}  ({data['makerShare']}% maker, "
          f"${data['volumePerHourUsd']}/h, {data['fillCount']} fills)")
    print(f"fees      ${data['feesPaid']} paid, ${data['rebatesEarned']} rebates")
    print(f"pnl       gross ${data['grossPnl']} -> net ${data['netPnl']} "
          f"({data['netBpsOfVolume']} bps, coverage {data['feeCoverageRatio']}x)")
    for m in data["markets"]:
        print(f"  {m['market']:<12} net ${m['net']:<12} vol ${m['volume']:<14} "
              f"fills {m['fills']:<5} pos {m['position']}")
    return 0


async def cmd_selftest(cfg: Config) -> int:
    """Offline end-to-end check with hard assertions on the accounting."""
    cfg.venue = "sim"
    cfg.mode = "live"          # 'live' against the simulator = orders really match
    cfg.max_runtime_s = cfg.max_runtime_s or 45
    cfg.report_interval_s = 10
    engine = Engine(cfg)
    await engine.run()

    s = engine.pnl.snapshot()
    failures: list[str] = []
    if float(s["volumeUsd"]) <= 0:
        failures.append("no volume generated")
    if s["fillCount"] == 0:
        failures.append("no fills booked")
    if engine.pnl.open_positions():
        failures.append(f"positions left open: {engine.pnl.open_positions()}")
    if float(s["feesPaid"]) < 0:
        failures.append("negative fees paid")
    recomputed = (
        engine.pnl.realized() + engine.pnl.unrealized() + engine.pnl.funding()
        + engine.pnl.total_rebates() - engine.pnl.total_fees()
    )
    if abs(recomputed - engine.pnl.net_pnl()) > Decimal("0.000001"):
        failures.append("net PnL identity broken")

    print("\nselftest assertions:")
    for name, ok in [
        ("volume generated", float(s["volumeUsd"]) > 0),
        ("fills booked", s["fillCount"] > 0),
        ("flat at exit", not engine.pnl.open_positions()),
        ("pnl identity", abs(recomputed - engine.pnl.net_pnl()) <= Decimal("0.000001")),
        ("risk guards armed", engine.risk.last_verdict.state in {"OK", "FLATTEN", "THROTTLE", "HALT"}),
    ]:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if failures:
        print("\nFAILED: " + "; ".join(failures))
        return 1
    print("\nSELFTEST OK")
    return 0


async def cmd_run(cfg: Config) -> int:
    engine = Engine(cfg)
    server = None
    if cfg.metrics_port:
        server = start_dashboard(cfg.metrics_host, cfg.metrics_port, engine.status)
    try:
        return await engine.run()
    finally:
        if server:
            server.shutdown()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    setup_logging(cfg)

    problems = cfg.validate()
    if problems and args.command in {"run", "selftest"}:
        for p in problems:
            print(f"config error: {p}", file=sys.stderr)
        return 2

    if args.command == "preflight":
        return cmd_preflight(cfg, args.json)
    if args.command == "markets":
        return cmd_markets(cfg, args.json)
    if args.command == "report":
        return cmd_report(cfg, args.json)
    if args.command == "quote":
        return asyncio.run(cmd_quote(cfg, args.json))
    if args.command == "selftest":
        return asyncio.run(cmd_selftest(cfg))
    if args.command == "sweep":
        from .sweep import format_table, sweep_command
        spreads = [float(x) for x in args.sweep_spreads.split(",") if x.strip()]
        results = sweep_command(cfg, spreads, args.sweep_seeds, args.sweep_duration)
        if args.json:
            print(json.dumps([r.as_dict() for r in results], indent=2))
        else:
            print(format_table(results))
        return 0 if any(r.net_bps_mean > 0 for r in results) else 1
    if args.command == "run":
        if cfg.live:
            print(f"\n*** LIVE MODE on {cfg.network} — real signed orders will be sent ***")
            print(f"    markets={','.join(cfg.markets)} notional=${dec_str(cfg.order_notional_usd)} "
                  f"maxPos=${dec_str(cfg.max_position_notional_usd)}\n")
        return asyncio.run(cmd_run(cfg))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
