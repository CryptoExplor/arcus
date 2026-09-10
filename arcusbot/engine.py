"""The trading engine: wires market data, strategy, execution, risk and PnL.

Execution model
---------------
* Market data arrives over WebSocket (`bbo`, `l2Orderbook`, `oraclePrices`,
  `markets`); account truth arrives over `account`, `positions`, `orders`,
  `userFills`, `funding`. REST is used only for bootstrap (markets, fee tiers,
  account) and for the order writes themselves — WebSocket subscriptions are
  free while REST polling burns the IP weight bucket.
* Orders are written over REST (`placeOrder`/`cancelOrder` cost 0 IP weight);
  the definitive lifecycle is taken from the `orders`/`userFills` streams, not
  from the HTTP 202 body.
* Positions from the exchange are inventory truth. The local PnL book is
  reconciled against them every loop so a missed fill event cannot make the
  bot think it is flat when it is not.

Shutdown is always graceful: cancel-all, optionally flatten, write the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .book import BookState, MarketState
from .capital import Allocation, apply_allocation, needs_resize, plan_capital
from .config import Config
from .pnl import FeeSchedule, Fill, PnLTracker, fills_from_ws
from .referral import all_referral_links, banner, vip_progress
from .rest import ArcusError, ArcusREST
from .session import SessionStore
from .risk import FLATTEN, HALT, OK, RiskManager
from .scaling import D, dec_str
from .signing import Signer
from .sim import SIM_FEE_TIERS, SimExchange
from .strategy import MarketWorker, OrderIntent, SpotVolumeStrategy
from .ws import ArcusWS, account_channels, market_channels

log = logging.getLogger("arcusbot.engine")


class Engine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.signer: Signer | None = Signer(cfg.api_secret) if cfg.api_secret else None
        self.rest = ArcusREST(cfg, self.signer)
        self.ws: ArcusWS | None = None
        self.sim: SimExchange | None = None
        self.pnl = PnLTracker(journal_path=cfg.state_dir / "fills.jsonl")
        self.session = SessionStore(cfg.state_dir / "session.json",
                                    enabled=cfg.persist_state).load()
        self.risk = RiskManager(cfg, self.pnl, self.session)
        self.workers: dict[str, MarketWorker] = {}
        self.states: dict[str, MarketState] = {}
        self.spot = SpotVolumeStrategy(cfg, self.pnl)
        self.spot_intents: list[dict[str, Any]] = []
        self.stopping = asyncio.Event()
        self.started_at = time.time()
        self.loop_count = 0
        self.orders_sent = 0
        self.cancels_sent = 0
        self.rejects = 0
        self.dry_run_log: list[str] = []
        self.last_report = 0.0
        self.exit_reason = "not-started"
        self.allocation: Allocation | None = None
        self.resizes = 0

    # ------------------------------------------------------------ bootstrap --
    def _load_markets(self) -> dict[str, dict[str, Any]]:
        if self.cfg.venue == "sim":
            assert self.sim is not None
            return self.sim.markets()
        return self.rest.markets(refresh=True)

    def _load_fees(self) -> FeeSchedule:
        if self.cfg.venue == "sim":
            # Offline, the only volume history that exists is our own, so the
            # simulator can still exercise tier progression across restarts.
            return FeeSchedule.from_fee_tiers(SIM_FEE_TIERS,
                                              self.session.lifetime.volume_usd)
        try:
            volume = None
            try:
                stats = self.rest.account_stats(sections="volume")
                for key in ("volume30d", "rolling30dVolume", "volume30dUsd"):
                    if isinstance(stats, dict) and stats.get(key) not in (None, ""):
                        volume = D(stats[key])
                        break
            except ArcusError:
                pass
            if volume is None and self.session.lifetime.volume_usd > 0:
                # The exchange is authoritative; fall back to our own tally only
                # when it will not answer.
                volume = self.session.lifetime.volume_usd
                log.info("using locally tracked volume $%s for the fee tier estimate",
                         dec_str(volume))
            return FeeSchedule.from_fee_tiers(self.rest.fee_tiers(), volume)
        except ArcusError as exc:
            log.warning("fee tier fetch failed (%s); using conservative defaults", exc)
            return FeeSchedule()

    async def setup(self) -> None:
        if self.cfg.venue == "sim":
            self.sim = SimExchange(self.cfg)

        markets = self._load_markets()
        self.pnl.fees = self._load_fees()
        log.info(
            "fee schedule: tier %s (%s) maker %s bps / taker %s bps [%s]",
            self.pnl.fees.level, self.pnl.fees.name,
            dec_str(self.pnl.fees.maker_bps), dec_str(self.pnl.fees.taker_bps), self.pnl.fees.source,
        )
        if self.pnl.fees.maker_is_rebate:
            log.info("maker fees are a REBATE at this tier — resting both legs earns money")
        progress = self.pnl.fees.next_tier_progress()
        if progress:
            log.info("next fee tier %s at $%s volume (%s%% there): saves %s bps per "
                     "round trip%s",
                     progress["name"], progress["volumeThresholdUsd"],
                     progress["pctComplete"], progress["savingBpsPerRoundTrip"],
                     " and unlocks maker rebates" if progress["unlocksMakerRebate"] else "")

        required = self.pnl.edge_required_bps(self.cfg.fee_buffer_bps, maker_legs=2)
        log.info("required round-trip edge: %s bps (maker/maker) — quoting at >= this", dec_str(required))

        if self.session.loaded_from_disk:
            log.info("restored state — %s", self.session.describe())
            if self.risk.carried_daily_loss > 0:
                log.warning("carrying $%s of loss already booked today toward the "
                            "$%s daily limit",
                            dec_str(self.risk.carried_daily_loss),
                            dec_str(self.cfg.max_daily_loss_usd))
            if self.session.risk.consecutive_crashes:
                log.warning("%d consecutive unclean exit(s); last: %s",
                            self.session.risk.consecutive_crashes,
                            self.session.risk.last_exit_reason or "unknown")

        wanted = [m for m in self.cfg.markets if m in markets]
        missing = [m for m in self.cfg.markets if m not in markets]
        if missing:
            log.warning("skipping unknown markets: %s", ", ".join(missing))
        if not wanted:
            raise RuntimeError(f"none of {self.cfg.markets} exist on the venue")

        for name in wanted:
            meta = markets[name]
            state = MarketState(
                market=name,
                market_id=int(meta["marketId"]),
                meta=meta,
                book=BookState(market=name),
            )
            state.apply_market_row(meta)
            self.states[name] = state
            self.workers[name] = MarketWorker(cfg=self.cfg, market=name, state=state, pnl=self.pnl)

        if self.cfg.venue == "arcus":
            await self._setup_live()
        elif self.sim is not None:
            # The simulator knows its own starting equity, so capital sizing
            # can be exercised offline exactly as it would be live.
            self.risk.note_account(self.sim.account())

        self._resize_capital(initial=True)

        self.session.start_session(self.pnl.session_id, {
            "venue": self.cfg.venue,
            "mode": self.cfg.mode,
            "strategy": self.cfg.strategy,
            "markets": ",".join(wanted),
        })
        self.session.save()

        log.info("engine ready: venue=%s mode=%s strategy=%s markets=%s",
                 self.cfg.venue, self.cfg.mode, self.cfg.strategy, ",".join(wanted))

    async def _setup_live(self) -> None:
        # Account bootstrap (404 simply means "no activity yet").
        if self.cfg.address:
            try:
                account = self.rest.account()
                self.risk.note_account(account)
                log.info("account equity=%s freeCollateral=%s",
                         account.get("equity"), account.get("freeCollateral"))
            except ArcusError as exc:
                if exc.status == 404:
                    log.warning("account has no activity yet — fund it before running live")
                else:
                    log.warning("account read failed: %s", exc)

        if self.cfg.live and self.cfg.leverage:
            for name in self.workers:
                try:
                    self.rest.set_leverage(name, self.cfg.leverage)
                    log.info("leverage %sx set on %s", self.cfg.leverage, name)
                except ArcusError as exc:
                    log.warning("setLeverage failed on %s: %s", name, exc)

        if self.cfg.live and self.cfg.dead_mans_switch_s:
            try:
                self.rest.arm_dead_mans_switch(self.cfg.dead_mans_switch_s)
                log.info("dead man's switch armed at %ss", self.cfg.dead_mans_switch_s)
            except ArcusError as exc:
                log.warning("dead man's switch unavailable: %s", exc)

        self.ws = ArcusWS(self.cfg, self.signer)
        self.ws.on_message(self._on_ws)
        for channel, sub_id, params in market_channels(self.workers.keys()):
            self.ws.subscribe(channel, sub_id, **params)
        if self.cfg.address:
            for channel, sub_id, params in account_channels(self.cfg.address):
                self.ws.subscribe(channel, sub_id, **params)
        await self.ws.start()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.ws.connected.wait(), timeout=15)

    # --------------------------------------------------- capital allocation --
    def _resize_capital(self, initial: bool = False) -> None:
        """Derive sizing from the account balance (capital mode only).

        Called once at startup and again whenever equity has drifted past
        ``BOT_CAPITAL_RESIZE_PCT``, so the bot scales with the account instead
        of trading a stale notional. In fixed mode this is a cheap no-op.
        """
        equity = self.risk.equity
        alloc = plan_capital(self.cfg, equity, len(self.workers) or len(self.cfg.markets))

        if initial:
            self.allocation = alloc
            apply_allocation(self.cfg, alloc)
            log.info("%s", alloc.describe())
            for note in alloc.notes:
                # Only the note that blocks trading deserves a warning; the
                # rest are explanations of a working plan.
                (log.warning if not alloc.sufficient else log.info)("capital: %s", note)
            if not alloc.sufficient:
                # Refuse loudly rather than quietly trading a size the venue
                # will reject on every single order.
                self.risk.halt(
                    "insufficient capital: " + (alloc.notes[-1] if alloc.notes else "budget too small")
                )
            return

        if not needs_resize(self.allocation, equity, self.cfg.capital_resize_pct):
            return

        self.allocation = alloc
        apply_allocation(self.cfg, alloc)
        self.resizes += 1
        log.info("capital re-sized (equity moved past %s%%): %s",
                 dec_str(self.cfg.capital_resize_pct), alloc.describe())

    # -------------------------------------------------------- ws ingestion --
    def _on_ws(self, channel: str, sub_id: str, contents: dict[str, Any]) -> None:
        try:
            if channel in {"l2Orderbook", "l2OrderbookUpdates"}:
                state = self.states.get(sub_id)
                if state:
                    state.book.apply_snapshot(contents)
            elif channel == "bbo":
                state = self.states.get(sub_id)
                if state:
                    state.book.apply_bbo(contents)
            elif channel == "oraclePrices":
                for entry in contents.get("prices", []) or []:
                    state = self.states.get(str(entry.get("marketDisplayName")))
                    if state:
                        state.apply_oracle(entry)
            elif channel == "markets":
                for row in (contents.get("markets") or {}).values():
                    state = self.states.get(str(row.get("marketDisplayName")))
                    if state:
                        state.apply_market_row(row)
                        state.meta.update(row)
            elif channel == "marketAttributes":
                for entry in contents.get("entries", []) or []:
                    state = self.states.get(str(entry.get("marketDisplayName")))
                    if state:
                        state.apply_market_row(entry)
            elif channel == "account":
                self.risk.note_account(contents)
                # The account frame's `positions` map is a full snapshot
                # ({} when flat); streaming account frames omit it entirely.
                if contents.get("positions") is not None:
                    self._sync_positions(contents["positions"], authoritative=True)
            elif channel == "positions":
                # Snapshot => keyed object under "positions"; streaming deltas
                # are a single bare position row (isSnapshot False).
                if contents.get("positions") is not None:
                    self._sync_positions(contents["positions"], authoritative=True)
                else:
                    self._sync_positions(contents, authoritative=False)
            elif channel == "userFills":
                for fill in fills_from_ws(contents, self.pnl.fees):
                    if fill.market and self.pnl.record_fill(fill):
                        log.info("FILL %s %s %s @ %s (%s, fee %s)",
                                 fill.market, fill.side, dec_str(fill.size),
                                 dec_str(fill.price), fill.liquidity, dec_str(fill.fee))
                        worker = self.workers.get(fill.market)
                        if worker and fill.client_id:
                            worker.on_terminal(fill.client_id)
            elif channel == "orders":
                self._on_orders(contents)
            elif channel == "funding":
                self._on_funding(contents)
        except Exception:
            log.exception("failed handling %s frame", channel)

    def _sync_positions(self, payload: Any, authoritative: bool = False) -> None:
        """Exchange positions are inventory truth; reconcile the local book.

        `authoritative` marks a full snapshot (the keyed `positions` object on
        the `account` / `positions` subscribe frames), where a market's absence
        means flat. Streaming deltas are per-position and must NOT zero others.
        """
        if payload is None:
            return
        rows: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            if "positions" in payload and isinstance(payload["positions"], dict):
                rows = list(payload["positions"].values())
            elif "marketId" in payload or "marketDisplayName" in payload:
                rows = [payload]
            else:
                rows = [v for v in payload.values() if isinstance(v, dict)]
        elif isinstance(payload, list):
            rows = payload

        seen: set[str] = set()
        for row in rows:
            name = str(row.get("marketDisplayName") or "")
            if name not in self.workers:
                continue
            seen.add(name)
            size = D(row.get("size", 0))
            side = str(row.get("side", "")).upper()
            signed = -size if side in {"SHORT", "SELL"} else size
            if side == "FLAT":
                signed = Decimal(0)
            book = self.pnl.book(name)
            if book.position != signed:
                log.debug("position resync %s: local %s -> venue %s",
                          name, dec_str(book.position), dec_str(signed))
                book.position = signed
                entry = row.get("averageEntryPrice")
                if entry not in (None, "", "0"):
                    book.avg_entry = D(entry)
                elif signed == 0:
                    book.avg_entry = Decimal(0)

        if authoritative:
            for name in self.workers:
                if name not in seen and self.pnl.book(name).position != 0:
                    log.debug("position resync %s: venue reports flat", name)
                    self.pnl.book(name).position = Decimal(0)
                    self.pnl.book(name).avg_entry = Decimal(0)

    def _on_orders(self, contents: dict[str, Any]) -> None:
        rows: list[dict[str, Any]] = []
        for key in ("orders", "openOrders", "data"):
            if isinstance(contents.get(key), list):
                rows = contents[key]
                break
        if not rows and ("orderId" in contents or "clientId" in contents):
            rows = [contents]

        for row in rows:
            market = str(row.get("market") or row.get("marketDisplayName") or "")
            worker = self.workers.get(market)
            if not worker:
                continue
            client_id = str(row.get("clientId") or "")
            order_id = str(row.get("orderId") or "")
            status = str(row.get("status") or row.get("type") or "").upper()
            if status in {"OPEN", "ACK", "PLACED", "NEW"} and client_id:
                worker.on_ack(client_id, order_id)
            elif status in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                if client_id:
                    worker.on_terminal(client_id)
                reason = str(row.get("rejectionReason") or "")
                if status == "REJECTED":
                    self.rejects += 1
                    self.risk.note_rejection(reason)
                    if reason not in {"POST_ONLY_WOULD_CROSS", "SELF_TRADE"}:
                        log.warning("order rejected on %s: %s", market, reason or row)

    def _on_funding(self, contents: dict[str, Any]) -> None:
        rows = contents.get("payments") or contents.get("funding") or []
        if isinstance(contents, dict) and ("payment" in contents or "amount" in contents):
            rows = [contents]
        for row in rows if isinstance(rows, list) else []:
            market = str(row.get("market") or row.get("marketDisplayName") or "")
            amount = row.get("payment", row.get("amount"))
            if market and amount not in (None, ""):
                # Positive payment = received. Sign convention follows the API.
                self.pnl.record_funding(market, D(amount))

    # ---------------------------------------------------------- execution ---
    async def _execute(self, intents: list[OrderIntent]) -> None:
        for intent in intents:
            # During shutdown only reduce-only / cancel work is still allowed:
            # the bot must always be able to close what it opened.
            if self.stopping.is_set() and intent.action == "place" and not intent.reduce_only:
                continue
            worker = self.workers[intent.market]
            if intent.action == "cancel":
                await self._do_cancel(worker, intent)
            else:
                await self._do_place(worker, intent)

    async def _do_place(self, worker: MarketWorker, intent: OrderIntent) -> None:
        state = self.states[intent.market]
        allowed, reason = self.risk.check_order(
            state, intent.side, intent.size, intent.price,
            reduce_only=intent.reduce_only,
            open_orders=len(worker.quotes),
            position=worker.position,
        )
        if not allowed:
            log.debug("skip %s: %s", intent.describe(), reason)
            return

        if not self.cfg.live and self.cfg.venue == "arcus":
            self.dry_run_log.append(intent.describe())
            log.info("[dry-run] %s", intent.describe())
            worker.register(intent)
            return

        try:
            if self.cfg.venue == "sim":
                assert self.sim is not None
                resp = self.sim.place(
                    intent.market, intent.side, intent.size, intent.price,
                    intent.tif, intent.reduce_only, intent.client_id,
                )
            else:
                resp = await asyncio.to_thread(
                    self.rest.place_order,
                    intent.market, intent.side, intent.size, intent.price,
                    tif=intent.tif, reduce_only=intent.reduce_only, client_id=intent.client_id,
                    order_type="LIMIT",
                )
            self.orders_sent += 1
            self.risk.note_order()
            self.risk.note_success()
            status = str(resp.get("status", "")).upper()
            if status == "REJECTED":
                self.rejects += 1
                self.risk.note_rejection(str(resp.get("rejectionReason", "")))
            elif status not in {"CANCELED", "CANCELLED"}:
                worker.register(intent)
                if resp.get("orderId"):
                    worker.on_ack(intent.client_id, str(resp["orderId"]))
        except ArcusError as exc:
            self._handle_api_error(exc, intent)
        except Exception as exc:
            self.risk.note_error(exc)
            log.warning("place failed (%s): %s", type(exc).__name__, exc)

    async def _do_cancel(self, worker: MarketWorker, intent: OrderIntent) -> None:
        worker.quotes.pop(intent.client_id, None)
        if not self.cfg.live and self.cfg.venue == "arcus":
            return
        try:
            if self.cfg.venue == "sim":
                assert self.sim is not None
                self.sim.cancel(client_id=intent.client_id, order_id=intent.order_id)
            else:
                if intent.order_id:
                    await asyncio.to_thread(self.rest.cancel_order, intent.market, order_id=intent.order_id)
                else:
                    await asyncio.to_thread(self.rest.cancel_order, intent.market, client_id=intent.client_id)
            self.cancels_sent += 1
            self.risk.note_success()
        except ArcusError as exc:
            # Cancelling an already-terminal order is normal noise.
            if exc.status not in {400, 404} and "ORDER_NOT_FOUND" not in exc.raw:
                self._handle_api_error(exc, intent)
        except Exception as exc:
            self.risk.note_error(exc)

    def _handle_api_error(self, exc: ArcusError, intent: OrderIntent) -> None:
        if exc.rate_limited:
            wait = max(exc.retry_after_s, 0.5)
            self.risk.throttle(wait, f"429 ({exc.limit_reason})")
            log.warning("rate limited (%s) — backing off %.2fs", exc.limit_reason, wait)
            return
        if exc.error_type in {"Tick", "InvalidRequest", "OracleDeviation"}:
            log.warning("order rejected (%s) on %s: %s", exc.error_type, intent.market, exc.raw[:200])
            self.risk.note_rejection(exc.error_type)
            return
        if exc.status in {401, 403}:
            self.risk.halt(f"auth failure {exc.status}: {exc.raw[:160]}")
            return
        self.risk.note_error(exc)
        log.warning("api error on %s: %s", intent.market, exc)

    # ---------------------------------------------------------- main loop ---
    async def run(self) -> int:
        await self.setup()
        self._install_signals()
        self.last_report = time.time()

        try:
            while not self.stopping.is_set():
                self.loop_count += 1
                await self._loop_once()
                await asyncio.sleep(self.cfg.loop_interval_s / max(self.cfg.sim_speed, 0.01)
                                    if self.cfg.venue == "sim" else self.cfg.loop_interval_s)
        except asyncio.CancelledError:
            self.exit_reason = "cancelled"
        finally:
            await self.shutdown()
        return 0 if self.pnl.net_pnl() >= 0 else 1

    async def _loop_once(self) -> None:
        if self.cfg.venue == "sim":
            self._pump_sim()

        for st in self.states.values():
            st.observe_vol()
        self.pnl.set_marks({name: st.reference_price for name, st in self.states.items()})
        self._resize_capital()
        verdict = self.risk.evaluate()

        if verdict.halted:
            self.exit_reason = verdict.reasons[0] if verdict.reasons else "halted"
            self.stopping.set()
            return

        can_open = verdict.state == OK and self.cfg.aggressive is not None
        if verdict.state in {FLATTEN, HALT}:
            can_open = False

        intents: list[OrderIntent] = []
        for worker in self.workers.values():
            intents.extend(worker.tick(can_open=can_open))
        await self._execute(intents)

        if self.cfg.enable_spot:
            for spot in self.spot.tick(self.cfg.spot_markets, can_open):
                self.spot_intents.append({"ts": time.time(), "intent": spot.describe()})
                log.info("[spot] %s", spot.describe())

        if time.time() - self.last_report >= self.cfg.report_interval_s:
            self.last_report = time.time()
            self._report()

    def _pump_sim(self) -> None:
        assert self.sim is not None
        self.sim.step_prices()
        for name, state in self.states.items():
            state.book.apply_snapshot(self.sim.book(name))
            state.apply_oracle({
                "marketDisplayName": name,
                "price": dec_str(self.sim.mids[name]),
                "markPrice": dec_str(self.sim.mids[name]),
                "markEpochNanos": time.time_ns(),
            })
        self.sim.match()
        # Every sim fill — maker (from match) AND taker (from place) — lands on
        # the event queue, so draining it is the single ingestion path. Missing
        # the taker leg here is exactly how a bot ends up believing it is flat
        # while the venue says otherwise.
        for event in self.sim.drain_events():
            if event.get("type") == "ACK":
                worker = self.workers.get(event.get("market", ""))
                if worker:
                    worker.on_ack(event.get("clientId", ""), event.get("orderId", ""))
                continue
            if event.get("type") == "CANCELED":
                worker = self.workers.get(event.get("market", ""))
                if worker:
                    worker.on_terminal(event.get("clientId", ""))
                continue
            fill = event.get("fill")
            if fill is None or not self.pnl.record_fill(fill):
                continue
            log.info("FILL %s %s %s @ %s (%s, fee %s)",
                     fill.market, fill.side, dec_str(fill.size), dec_str(fill.price),
                     fill.liquidity, dec_str(fill.fee))
            worker = self.workers.get(fill.market)
            if worker and fill.client_id:
                worker.on_terminal(fill.client_id)

        account = self.sim.account()
        self.risk.note_account(account)
        self._sync_positions(account.get("positions"), authoritative=True)

    def _report(self) -> None:
        log.info("--- %s", self.pnl.text_report().replace("\n", " | "))
        self.write_state()

    # ------------------------------------------------------------ shutdown --
    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._on_signal, sig)

    def _on_signal(self, sig: Any) -> None:
        if not self.stopping.is_set():
            self.exit_reason = f"signal {getattr(sig, 'name', sig)}"
            log.warning("shutdown requested (%s)", self.exit_reason)
            self.stopping.set()

    async def shutdown(self) -> None:
        log.info("shutting down: %s", self.exit_reason)
        if self.cfg.cancel_all_on_exit:
            await self._cancel_everything()
            await self._flatten_everything()
        if self.ws:
            await self.ws.stop()

        # Fold this run into the durable totals BEFORE writing the report, so a
        # crash between the two loses the report rather than the risk state.
        snapshot = self.pnl.snapshot()
        self.session.risk.peak_net_pnl_usd = max(
            self.session.risk.peak_net_pnl_usd, self.pnl.peak_net)
        self.session.note_equity(self.risk.equity)
        self.session.finish_session(self.pnl.session_id, snapshot, self.exit_reason)

        self.write_state()
        report = self.pnl.write_report(self.cfg.state_dir / f"report-{self.pnl.session_id}.json")
        latest = self.cfg.state_dir / "report-latest.json"
        latest.write_text(report.read_text(encoding="utf-8"), encoding="utf-8")
        if self.cfg.print_summary:
            print("\n" + "=" * 72)
            print(self.pnl.text_report())
            if self.allocation is not None and self.allocation.mode == "capital":
                print(self.allocation.describe())
            print(vip_progress(self.pnl.snapshot(),
                               lifetime_volume_usd=self.session.lifetime.volume_usd).describe())
            if self.session.enabled and self.session.lifetime.sessions > 1:
                print(self.session.describe())
            print(f"exit reason : {self.exit_reason}")
            print(f"report      : {report}")
            print("=" * 72)
            banner_text = banner(self.cfg)
            if banner_text:
                print(banner_text)

    async def _cancel_everything(self) -> None:
        if self.cfg.venue == "sim":
            assert self.sim is not None
            self.sim.cancel_all()
            return
        if not self.cfg.live:
            return
        try:
            await asyncio.to_thread(self.rest.cancel_all)
            log.info("cancelAllOrders sent")
        except Exception as exc:
            log.warning("cancel-all failed: %s", exc)
            for worker in self.workers.values():
                for quote in list(worker.quotes.values()):
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            self.rest.cancel_order, worker.market, order_id=quote.order_id or None,
                            client_id=None if quote.order_id else quote.client_id,
                        )

    async def _flatten_everything(self, attempts: int = 5) -> None:
        """Best-effort reduce-only IOC on every open position.

        Retried because an IOC can legitimately come back empty
        (``IOC_CANCELED``) when the book moves away between quote and send.
        """
        for name, worker in self.workers.items():
            if worker.position == 0:
                continue
            log.info("flattening %s position %s", name, dec_str(worker.position))
            for attempt in range(attempts):
                await self._execute(worker.flatten_intents(urgent=True))
                await asyncio.sleep(0.4)
                if self.cfg.venue == "sim":
                    self._pump_sim()
                else:
                    await self._refresh_positions()
                if worker.position == 0:
                    log.info("%s flat after %d attempt(s)", name, attempt + 1)
                    break
                log.debug("flatten attempt %d left %s on %s", attempt + 1, dec_str(worker.position), name)
            if worker.position != 0:
                log.error("COULD NOT FLATTEN %s: %s still open — close it manually",
                          name, dec_str(worker.position))

    async def _refresh_positions(self) -> None:
        if self.cfg.venue != "arcus" or not self.cfg.address:
            return
        try:
            payload = await asyncio.to_thread(self.rest.positions)
            rows = payload.get("positions", payload) if isinstance(payload, dict) else payload
            self._sync_positions(rows, authoritative=True)
        except Exception as exc:
            log.debug("position refresh failed: %s", exc)

    # -------------------------------------------------------------- status --
    def status(self) -> dict[str, Any]:
        pnl_snapshot = self.pnl.snapshot()
        return {
            "version": "1.0.0",
            "venue": self.cfg.venue,
            "network": self.cfg.network,
            "mode": self.cfg.mode,
            "strategy": self.cfg.strategy,
            "uptimeSeconds": round(time.time() - self.started_at, 1),
            "loops": self.loop_count,
            "ordersSent": self.orders_sent,
            "cancelsSent": self.cancels_sent,
            "rejects": self.rejects,
            "exitReason": self.exit_reason if self.stopping.is_set() else None,
            "risk": self.risk.snapshot(),
            "pnl": pnl_snapshot,
            "capital": self.allocation.as_dict() if self.allocation else None,
            "capitalResizes": self.resizes,
            "vip": vip_progress(pnl_snapshot,
                                lifetime_volume_usd=self.session.lifetime.volume_usd).as_dict(),
            "session": self.session.snapshot(),
            "referral": all_referral_links(self.cfg) if self.cfg.show_referral else {},
            "markets": [w.snapshot() for w in self.workers.values()],
            "ws": self.ws.health() if self.ws else None,
            "ipBudget": self.rest.budget.snapshot(),
            "spotIntents": self.spot_intents[-10:],
            "dryRunSample": self.dry_run_log[-10:],
        }

    def write_state(self) -> Path:
        path = self.cfg.state_dir / "status.json"
        path.write_text(json.dumps(self.status(), indent=2), encoding="utf-8")
        return path
