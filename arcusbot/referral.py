"""Referral links and the VIP volume milestone.

Two related things live here.

**Referral attribution.** Arcus signups can be attributed to a referrer via a
``/ref/<CODE>`` link. The bot's own trading is unaffected — attribution happens
at *signup*, in a browser, not over the trading API. So this module never tries
to inject a code into an order; it surfaces the right link at the moments a
human is actually onboarding (onboarding tooling, preflight, the dashboard,
docs).

The code is configurable (``ARCUS_REFERRAL_TESTNET`` / ``ARCUS_REFERRAL_MAINNET``)
so anyone forking this repo can put in their own, and can disable the banner
entirely with ``BOT_SHOW_REFERRAL=false``.

**VIP progress.** Both networks advertise a "Trade $1B volume to unlock VIP"
milestone. Since generating volume is this bot's whole purpose, it tracks
progress toward that target and — more usefully — reports the honest arithmetic:
at the current rate, how long $1B would actually take, and what it would cost
in fees. That number is normally sobering, which is the point: it is better to
see it up front than after a week of running.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .scaling import dec_str

# Default attribution for this tool. Override in .env to use your own.
DEFAULT_REFERRAL_TESTNET = "ARCUS"
DEFAULT_REFERRAL_MAINNET = "IN"

TESTNET_REF_BASE = "https://testnet.arcus.xyz/ref/"
MAINNET_REF_BASE = "https://app.arcus.xyz/ref/"

VIP_VOLUME_TARGET_USD = Decimal("1000000000")   # $1B unlocks VIP


def referral_code(cfg: Any) -> str:
    return (cfg.referral_mainnet if cfg.network == "mainnet" else cfg.referral_testnet) or ""


def referral_link(cfg: Any) -> str:
    """The signup link for the network currently configured."""
    code = referral_code(cfg)
    if not code:
        return ""
    base = MAINNET_REF_BASE if cfg.network == "mainnet" else TESTNET_REF_BASE
    return base + code


def all_referral_links(cfg: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if cfg.referral_testnet:
        out["testnet"] = TESTNET_REF_BASE + cfg.referral_testnet
    if cfg.referral_mainnet:
        out["mainnet"] = MAINNET_REF_BASE + cfg.referral_mainnet
    return out


def banner(cfg: Any) -> str:
    """A short attribution block for human-facing command output."""
    if not cfg.show_referral:
        return ""
    links = all_referral_links(cfg)
    if not links:
        return ""
    lines = ["", "Signing up for Arcus? Use these links to support this tool:"]
    for network, url in links.items():
        lines.append(f"  {network:8} {url}")
    lines.append("  (set ARCUS_REFERRAL_* in .env to use your own, "
                 "or BOT_SHOW_REFERRAL=false to hide this)")
    return "\n".join(lines)


@dataclass(slots=True)
class VipProgress:
    """Progress toward the $1B VIP volume milestone."""

    volume_usd: Decimal
    target_usd: Decimal
    volume_per_hour_usd: Decimal
    net_bps_of_volume: Decimal

    @property
    def pct(self) -> Decimal:
        if self.target_usd <= 0:
            return Decimal(0)
        return (self.volume_usd / self.target_usd * 100)

    @property
    def remaining_usd(self) -> Decimal:
        return max(Decimal(0), self.target_usd - self.volume_usd)

    @property
    def hours_remaining(self) -> Decimal | None:
        if self.volume_per_hour_usd <= 0:
            return None
        return self.remaining_usd / self.volume_per_hour_usd

    @property
    def projected_pnl_usd(self) -> Decimal:
        """What reaching the target would cost (or earn) at the current edge.

        Negative net bps means the milestone has a price tag. This is the
        number that decides whether chasing VIP is rational.
        """
        return self.remaining_usd * self.net_bps_of_volume / Decimal(10_000)

    def as_dict(self) -> dict[str, Any]:
        hours = self.hours_remaining
        return {
            "targetUsd": dec_str(self.target_usd),
            "volumeUsd": dec_str(self.volume_usd),
            "pctComplete": dec_str(self.pct.quantize(Decimal("0.000001"))),
            "remainingUsd": dec_str(self.remaining_usd),
            "volumePerHourUsd": dec_str(self.volume_per_hour_usd),
            "hoursRemaining": dec_str(hours.quantize(Decimal("0.1"))) if hours is not None else None,
            "daysRemaining": dec_str((hours / 24).quantize(Decimal("0.1"))) if hours is not None else None,
            "projectedPnlAtTargetUsd": dec_str(self.projected_pnl_usd.quantize(Decimal("0.01"))),
        }

    def describe(self) -> str:
        pct = self.pct
        head = (
            f"VIP progress: ${dec_str(self.volume_usd.quantize(Decimal('0.01')))} of "
            f"${dec_str(self.target_usd)} ({dec_str(pct.quantize(Decimal('0.000001')))}%)"
        )
        hours = self.hours_remaining
        if hours is None:
            return head
        days = hours / 24
        tail = f" — {dec_str(days.quantize(Decimal('0.1')))} days at the current rate"
        pnl = self.projected_pnl_usd
        if pnl < 0:
            tail += f", costing ~${dec_str(abs(pnl).quantize(Decimal('0.01')))} in net PnL"
        elif pnl > 0:
            tail += f", earning ~${dec_str(pnl.quantize(Decimal('0.01')))} at the current edge"
        return head + tail


def vip_progress(pnl_snapshot: dict[str, Any],
                 target_usd: Decimal = VIP_VOLUME_TARGET_USD,
                 lifetime_volume_usd: Decimal | None = None) -> VipProgress:
    """Progress toward VIP.

    ``lifetime_volume_usd`` (from the persisted session store) is used when
    available: the milestone is a lifetime figure, so counting only the current
    process would understate it and permanently mis-state the ETA.
    """
    def _d(key: str) -> Decimal:
        raw = pnl_snapshot.get(key)
        return Decimal(str(raw)) if raw not in (None, "") else Decimal(0)

    session_volume = _d("volumeUsd")
    volume = session_volume if lifetime_volume_usd is None else (
        lifetime_volume_usd + session_volume
    )
    return VipProgress(
        volume_usd=volume,
        target_usd=target_usd,
        volume_per_hour_usd=_d("volumePerHourUsd"),
        net_bps_of_volume=_d("netBpsOfVolume"),
    )
