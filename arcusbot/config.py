"""Typed configuration for the Arcus testnet bot.

Everything is read from environment variables (a local ``.env`` is loaded
first, if present). Defaults are deliberately conservative: the bot starts in
``dry-run`` and must be explicitly switched to ``live``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields as dataclass_fields
from decimal import Decimal
from pathlib import Path
from typing import Any

# referral.py deliberately does not import config (it takes the config object as
# a parameter), so this direction is safe.
from .referral import DEFAULT_REFERRAL_MAINNET, DEFAULT_REFERRAL_TESTNET

REPO_ROOT = Path(__file__).resolve().parent.parent

TESTNET_REST = "https://api.testnet.arcus.xyz"
TESTNET_WS = "wss://api.testnet.arcus.xyz/v1/ws"
MAINNET_REST = "https://api.arcus.xyz"
MAINNET_WS = "wss://api.arcus.xyz/v1/ws"

# Robinhood Chain testnet — deposit path. Addresses change on every redeploy;
# verify before relying on them (docs/guides/fund-testnet-account).
RH_TESTNET_RPC = "https://rpc.testnet.chain.robinhood.com"
RH_TESTNET_CHAIN_ID = 46630
USDG_TESTNET = "0x293b337712d4312776a3a2d292f44410e7873bad"
DEPOSIT_PROXY_TESTNET = "0xb872366eef371d4afb7c6d4d2abd53c17292a34d"


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing ``# comment`` from an unquoted .env value.

    Only whitespace-preceded ``#`` starts a comment, so values that legitimately
    contain a hash (``pass#word``) survive. Quoted values are returned verbatim
    by the caller and never reach this function.
    """
    out: list[str] = []
    for i, ch in enumerate(value):
        if ch == "#" and (i == 0 or value[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).strip()


def load_dotenv(path: str | Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader (no dependency on python-dotenv).

    Supports ``KEY=value``, ``export KEY=value``, quoted values, blank lines,
    full-line comments and trailing inline comments. Existing environment
    variables always win, so ``FOO=1 python -m arcusbot`` overrides the file.
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]          # quoted: take it literally
        else:
            value = _strip_inline_comment(value)
        os.environ.setdefault(key, value)


def _env(name: str, default: Any = None) -> Any:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(str(_env(name, default)))


def _float(name: str, default: float) -> float:
    return float(str(_env(name, default)))


def _dec(name: str, default: str) -> Decimal:
    return Decimal(str(_env(name, default)))


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in str(_env(name, default)).split(",") if x.strip()]


@dataclass(slots=True)
class Config:
    # ---------------------------------------------------------------- venue --
    network: str = "testnet"          # testnet | mainnet
    venue: str = "arcus"              # arcus | sim   (sim = offline paper engine)
    rest_url: str = TESTNET_REST
    ws_url: str = TESTNET_WS

    # ------------------------------------------------------------ identity --
    address: str = ""                 # master Ethereum address (0x...)
    account_index: int = 0            # subaccount
    api_secret: str = ""              # Ed25519 signing key, 64 hex chars

    # ---------------------------------------------------------------- mode --
    mode: str = "dry-run"             # dry-run | live
    strategy: str = "volume-maker"    # volume-maker | ping-pong | spot-rfq
    markets: list[str] = field(default_factory=lambda: ["BTC-USD", "ETH-USD"])
    spot_markets: list[str] = field(default_factory=lambda: ["AAPL", "TSLA"])
    enable_spot: bool = False
    aggressive: bool = True           # testnet default: chase volume

    # ------------------------------------------------------------- sizing ---
    order_notional_usd: Decimal = Decimal("25")     # per-clip notional
    max_position_notional_usd: Decimal = Decimal("150")
    max_inventory_notional_usd: Decimal = Decimal("75")
    leverage: int = 3
    quote_levels: int = 1             # ladder depth per side
    level_step_bps: Decimal = Decimal("4")

    # -------------------------------------------------- capital management --
    # Set capital_usd or capital_pct to derive every sizing knob from your
    # balance instead of hard-coding notionals. See arcusbot/capital.py.
    capital_usd: Decimal = Decimal("0")          # absolute budget (0 = off)
    capital_pct: Decimal = Decimal("0")          # % of equity to deploy (0 = off)
    reserve_usd: Decimal = Decimal("0")          # never touch this much equity
    capital_utilisation: Decimal = Decimal("0.5")   # share of available leverage used
    capital_clips: int = 3                       # clips per market inside the cap
    capital_inventory_fraction: Decimal = Decimal("0.5")  # soft cap / hard cap
    capital_resize_pct: Decimal = Decimal("20")  # equity drift before re-sizing
    max_drawdown_pct: Decimal = Decimal("0")     # drawdown limit as % of capital

    # ------------------------------------------------------------ referral --
    referral_testnet: str = DEFAULT_REFERRAL_TESTNET
    referral_mainnet: str = DEFAULT_REFERRAL_MAINNET
    show_referral: bool = True

    # ------------------------------------------------------------- evidence --
    # Free-text tags recorded with each session so results can be grouped by
    # market regime when the evidence is analysed.
    session_regime: str = ""
    session_label: str = ""

    # -------------------------------------------------------------- mainnet --
    # Live mainnet execution requires ALL of these; see arcusbot/mainnet.py.
    # NOTE: BOT_MAINNET_ENABLED / _ACK / _CAPITAL_USD are deliberately NOT
    # parsed into this config. arcusbot/mainnet.py reads them straight from the
    # environment with strict parsing, so a malformed value is an error rather
    # than a silent default. Mirroring them here would create a second, laxer
    # source of truth for the one decision that risks real money.

    # ----------------------------------------------------------- persistence --
    # Carry drawdown/daily-loss/lifetime-volume across restarts so a crash loop
    # cannot reset the kill switch. Disable for sweeps, backtests and CI.
    persist_state: bool = True
    carry_drawdown: bool = True      # restored peak PnL counts toward the limit
    max_restart_crashes: int = 0     # >0: refuse to start after N dirty exits

    # ------------------------------------------------------------- quoting --
    spread_bps: Decimal = Decimal("10")      # target round-trip edge (maker); see docs/STRATEGY.md
    min_edge_bps: Decimal = Decimal("1")     # absolute spread floor, survives rebate tiers
    join_bbo: bool = True                    # ALO at/inside BBO
    requote_bps: Decimal = Decimal("2")      # re-quote when quote drifts this far
    requote_interval_s: float = 1.5
    max_quote_age_s: float = 20.0            # re-quote a resting order after this
    inventory_max_age_s: float = 90.0        # only then cross the spread to flatten
    flatten_tif: str = "IOC"                 # how inventory is closed
    taker_slippage_bps: int = 25
    vol_edge_multiplier: Decimal = Decimal("1.5")   # spread widening per bps of realised vol
    inventory_skew_bps: Decimal = Decimal("8")      # max quote shift at full inventory

    # --------------------------------------------------------------- risk ---
    max_drawdown_usd: Decimal = Decimal("25")        # kill switch on net PnL
    max_daily_loss_usd: Decimal = Decimal("40")
    min_free_collateral_usd: Decimal = Decimal("50")
    max_open_orders: int = 12
    max_orders_per_min: int = 90
    max_consecutive_errors: int = 12
    stale_price_s: float = 20.0
    cancel_all_on_exit: bool = True
    dead_mans_switch_s: int = 0        # 0 = disabled; else arm DMS with this TTL

    # -------------------------------------------------------------- volume --
    volume_target_usd: Decimal = Decimal("0")   # 0 = unlimited
    max_runtime_s: int = 0                      # 0 = unlimited
    fee_buffer_bps: Decimal = Decimal("1")      # extra edge required over fees

    # ------------------------------------------------------------ plumbing --
    loop_interval_s: float = 0.75
    reconnect_max_s: float = 30.0
    state_dir: Path = REPO_ROOT / "state"
    log_dir: Path = REPO_ROOT / "logs"
    log_level: str = "INFO"
    metrics_port: int = 0             # 0 = dashboard off
    metrics_host: str = "0.0.0.0"
    report_interval_s: float = 15.0
    print_summary: bool = True        # print the end-of-run block to stdout

    # ----------------------------------------------------------------- sim --
    sim_seed: int = 7
    sim_maker_fill_prob: float = 0.55
    sim_vol_bps: float = 6.0
    sim_speed: float = 1.0
    sim_uninformed_rate: float = 0.25   # chance/step a random taker lifts our touch

    # ------------------------------------------------------------ derived ---
    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.log_dir = Path(self.log_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.network == "mainnet":
            if self.rest_url == TESTNET_REST:
                self.rest_url = MAINNET_REST
            if self.ws_url == TESTNET_WS:
                self.ws_url = MAINNET_WS

    @property
    def live(self) -> bool:
        return self.mode == "live" and self.venue == "arcus"

    @property
    def signing_enabled(self) -> bool:
        return bool(self.api_secret and self.address)

    def summary(self) -> dict[str, Any]:
        redacted = {"api_secret"}
        out: dict[str, Any] = {}
        for f in dataclass_fields(self):
            if f.name in redacted:
                out[f.name] = "***set***" if getattr(self, f.name) else "(unset)"
                continue
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, (Decimal, Path)) else value
        return out

    # ------------------------------------------------------------ loading ---
    @classmethod
    def from_env(cls, **overrides: Any) -> "Config":
        load_dotenv()
        network = str(_env("ARCUS_NETWORK", "testnet")).lower()
        default_rest = MAINNET_REST if network == "mainnet" else TESTNET_REST
        default_ws = MAINNET_WS if network == "mainnet" else TESTNET_WS

        cfg = cls(
            network=network,
            venue=str(_env("ARCUS_VENUE", "arcus")).lower(),
            rest_url=str(_env("ARCUS_REST_URL", default_rest)).rstrip("/"),
            ws_url=str(_env("ARCUS_WS_URL", default_ws)),
            address=str(_env("ARCUS_ADDRESS", "")),
            account_index=_int("ARCUS_ACCOUNT_INDEX", 0),
            api_secret=str(_env("ARCUS_API_SECRET", "")),
            mode=str(_env("BOT_MODE", "dry-run")).lower(),
            strategy=str(_env("BOT_STRATEGY", "volume-maker")).lower(),
            markets=_list("BOT_MARKETS", "BTC-USD,ETH-USD"),
            spot_markets=_list("BOT_SPOT_MARKETS", "AAPL,TSLA"),
            enable_spot=_bool("BOT_ENABLE_SPOT", False),
            aggressive=_bool("BOT_AGGRESSIVE", True),
            order_notional_usd=_dec("BOT_ORDER_NOTIONAL_USD", "25"),
            max_position_notional_usd=_dec("BOT_MAX_POSITION_NOTIONAL_USD", "150"),
            max_inventory_notional_usd=_dec("BOT_MAX_INVENTORY_NOTIONAL_USD", "75"),
            leverage=_int("BOT_LEVERAGE", 3),
            quote_levels=_int("BOT_QUOTE_LEVELS", 1),
            level_step_bps=_dec("BOT_LEVEL_STEP_BPS", "4"),
            capital_usd=_dec("BOT_CAPITAL_USD", "0"),
            capital_pct=_dec("BOT_CAPITAL_PCT", "0"),
            reserve_usd=_dec("BOT_RESERVE_USD", "0"),
            capital_utilisation=_dec("BOT_CAPITAL_UTILISATION", "0.5"),
            capital_clips=_int("BOT_CAPITAL_CLIPS", 3),
            capital_inventory_fraction=_dec("BOT_CAPITAL_INVENTORY_FRACTION", "0.5"),
            capital_resize_pct=_dec("BOT_CAPITAL_RESIZE_PCT", "20"),
            max_drawdown_pct=_dec("RISK_MAX_DRAWDOWN_PCT", "0"),
            referral_testnet=str(_env("ARCUS_REFERRAL_TESTNET", DEFAULT_REFERRAL_TESTNET)),
            referral_mainnet=str(_env("ARCUS_REFERRAL_MAINNET", DEFAULT_REFERRAL_MAINNET)),
            show_referral=_bool("BOT_SHOW_REFERRAL", True),
            session_regime=str(_env("BOT_SESSION_REGIME", "")),
            session_label=str(_env("BOT_SESSION_LABEL", "")),
            persist_state=_bool("BOT_PERSIST_STATE", True),
            carry_drawdown=_bool("RISK_CARRY_DRAWDOWN", True),
            max_restart_crashes=_int("RISK_MAX_RESTART_CRASHES", 0),
            spread_bps=_dec("BOT_SPREAD_BPS", "10"),
            min_edge_bps=_dec("BOT_MIN_EDGE_BPS", "1"),
            join_bbo=_bool("BOT_JOIN_BBO", True),
            requote_bps=_dec("BOT_REQUOTE_BPS", "2"),
            requote_interval_s=_float("BOT_REQUOTE_INTERVAL_S", 1.5),
            max_quote_age_s=_float("BOT_MAX_QUOTE_AGE_S", 20.0),
            inventory_max_age_s=_float("BOT_INVENTORY_MAX_AGE_S", 90.0),
            flatten_tif=str(_env("BOT_FLATTEN_TIF", "IOC")).upper(),
            taker_slippage_bps=_int("BOT_TAKER_SLIPPAGE_BPS", 25),
            vol_edge_multiplier=_dec("BOT_VOL_EDGE_MULTIPLIER", "1.5"),
            inventory_skew_bps=_dec("BOT_INVENTORY_SKEW_BPS", "8"),
            max_drawdown_usd=_dec("RISK_MAX_DRAWDOWN_USD", "25"),
            max_daily_loss_usd=_dec("RISK_MAX_DAILY_LOSS_USD", "40"),
            min_free_collateral_usd=_dec("RISK_MIN_FREE_COLLATERAL_USD", "50"),
            max_open_orders=_int("RISK_MAX_OPEN_ORDERS", 12),
            max_orders_per_min=_int("RISK_MAX_ORDERS_PER_MIN", 90),
            max_consecutive_errors=_int("RISK_MAX_CONSECUTIVE_ERRORS", 12),
            stale_price_s=_float("RISK_STALE_PRICE_S", 20.0),
            cancel_all_on_exit=_bool("RISK_CANCEL_ALL_ON_EXIT", True),
            dead_mans_switch_s=_int("RISK_DEAD_MANS_SWITCH_S", 0),
            volume_target_usd=_dec("BOT_VOLUME_TARGET_USD", "0"),
            max_runtime_s=_int("BOT_MAX_RUNTIME_S", 0),
            fee_buffer_bps=_dec("BOT_FEE_BUFFER_BPS", "1"),
            loop_interval_s=_float("BOT_LOOP_INTERVAL_S", 0.75),
            reconnect_max_s=_float("BOT_RECONNECT_MAX_S", 30.0),
            state_dir=Path(str(_env("BOT_STATE_DIR", REPO_ROOT / "state"))),
            log_dir=Path(str(_env("BOT_LOG_DIR", REPO_ROOT / "logs"))),
            log_level=str(_env("BOT_LOG_LEVEL", "INFO")).upper(),
            metrics_port=_int("BOT_METRICS_PORT", 0),
            metrics_host=str(_env("BOT_METRICS_HOST", "0.0.0.0")),
            report_interval_s=_float("BOT_REPORT_INTERVAL_S", 15.0),
            print_summary=_bool("BOT_PRINT_SUMMARY", True),
            sim_seed=_int("SIM_SEED", 7),
            sim_maker_fill_prob=_float("SIM_MAKER_FILL_PROB", 0.55),
            sim_vol_bps=_float("SIM_VOL_BPS", 6.0),
            sim_speed=_float("SIM_SPEED", 1.0),
            sim_uninformed_rate=_float("SIM_UNINFORMED_RATE", 0.25),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        cfg.__post_init__()
        return cfg

    def validate(self) -> list[str]:
        """Returns a list of fatal problems (empty means good to run)."""
        problems: list[str] = []
        if self.mode not in {"dry-run", "live"}:
            problems.append(f"BOT_MODE must be dry-run or live, got {self.mode!r}")
        if self.venue not in {"arcus", "sim"}:
            problems.append(f"ARCUS_VENUE must be arcus or sim, got {self.venue!r}")
        if self.strategy not in {"volume-maker", "ping-pong", "spot-rfq"}:
            problems.append(f"unknown BOT_STRATEGY {self.strategy!r}")
        if self.venue == "arcus" and self.mode == "live":
            if not self.address.startswith("0x") or len(self.address) != 42:
                problems.append("ARCUS_ADDRESS must be a 0x-prefixed 42-char address")
            if len(self.api_secret.removeprefix("0x")) != 64:
                problems.append("ARCUS_API_SECRET must be 64 hex chars (32-byte Ed25519 key)")
        capital_mode = self.capital_usd > 0 or self.capital_pct > 0
        if not capital_mode:
            # In capital mode these are derived at runtime from equity, so only
            # validate them when the operator is setting them by hand.
            if self.order_notional_usd < Decimal("5"):
                problems.append("BOT_ORDER_NOTIONAL_USD must be >= 5 (engine min order notional)")
            if self.max_position_notional_usd < self.order_notional_usd:
                problems.append("BOT_MAX_POSITION_NOTIONAL_USD must be >= BOT_ORDER_NOTIONAL_USD")

        if self.capital_pct < 0 or self.capital_pct > 100:
            problems.append(f"BOT_CAPITAL_PCT must be between 0 and 100, got {self.capital_pct}")
        if self.capital_usd < 0:
            problems.append("BOT_CAPITAL_USD cannot be negative")
        if self.reserve_usd < 0:
            problems.append("BOT_RESERVE_USD cannot be negative")
        if not (Decimal(0) < self.capital_utilisation <= Decimal(1)):
            problems.append(
                f"BOT_CAPITAL_UTILISATION must be in (0, 1], got {self.capital_utilisation}. "
                "It is the share of available leverage the bot deploys; 1.0 means "
                "running at full margin, which risks liquidation."
            )
        if self.capital_clips < 1:
            problems.append("BOT_CAPITAL_CLIPS must be >= 1")
        if not (Decimal(0) < self.capital_inventory_fraction <= Decimal(1)):
            problems.append(
                f"BOT_CAPITAL_INVENTORY_FRACTION must be in (0, 1], "
                f"got {self.capital_inventory_fraction}"
            )
        if self.max_drawdown_pct < 0 or self.max_drawdown_pct > 100:
            problems.append("RISK_MAX_DRAWDOWN_PCT must be between 0 and 100")

        if self.flatten_tif not in {"IOC", "FOK", "GTT", "ALO"}:
            problems.append(f"BOT_FLATTEN_TIF must be IOC/FOK/GTT/ALO, got {self.flatten_tif!r}")
        # Mainnet live execution is gated by arcusbot/mainnet.py, which requires
        # several explicit opt-ins and fails closed on any malformed value.
        # Imported lazily to keep config free of heavier imports.
        from .mainnet import evaluate_gate

        gate = evaluate_gate(self)
        if gate.is_mainnet_live_attempt and not gate.allowed:
            problems.append(
                "refusing to run live on mainnet — " + "; ".join(gate.reasons)
                + ". See docs/BOT_OPERATIONS.md (mainnet section)."
            )
        return problems
