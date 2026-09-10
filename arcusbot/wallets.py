"""Named wallet profiles — several keys in one `.env`, selected by name.

Motivation: testing with two testnet wallets and one mainnet wallet means three
sets of credentials. Keeping them in one file and switching with `--wallet t1`
beats editing `.env` between runs (and beats the classic accident of running a
mainnet key while believing you are on testnet).

Layout in `.env` — the suffix is the profile name, upper-cased:

    ARCUS_ADDRESS_T1=0x...
    ARCUS_API_SECRET_T1=<64 hex>
    ARCUS_NETWORK_T1=testnet          # optional, inferred from the name otherwise
    ARCUS_PRIVATE_KEY_T1=0x...        # OPTIONAL, withdrawal-capable — see below

The unsuffixed `ARCUS_ADDRESS` / `ARCUS_API_SECRET` remain the default profile,
so nothing changes for anyone not using profiles.

## On wallet private keys

`ARCUS_PRIVATE_KEY_*` is **not needed to trade**. The API signing key authorises
trading only and cannot move funds; a wallet private key can withdraw
everything. It is supported here solely because onboarding and spot RFQ require
a wallet signature, and it carries deliberate friction:

  * it is never loaded unless explicitly requested (`require_private_key`)
  * a mainnet profile additionally demands `ARCUS_ALLOW_MAINNET_PRIVATE_KEY=true`
  * it is redacted from every representation (`describe`, `as_dict`, logs)
  * `arcus wallets` warns whenever one is present

The safest arrangement remains: no wallet private key in `.env` at all, passed
as a one-off `--private-key` argument to `tools/onboard.py` when needed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Env keys that make up a profile. Suffixed with _<NAME> for named profiles.
_ADDRESS = "ARCUS_ADDRESS"
_SECRET = "ARCUS_API_SECRET"
_NETWORK = "ARCUS_NETWORK"
_PRIVATE = "ARCUS_PRIVATE_KEY"
_INDEX = "ARCUS_ACCOUNT_INDEX"
_LABEL = "ARCUS_LABEL"

# Profile names beginning with these are assumed to target that network when
# ARCUS_NETWORK_<NAME> is not given. "t1"/"test2" -> testnet, "m1" -> mainnet.
_TESTNET_PREFIXES = ("t", "test")
_MAINNET_PREFIXES = ("m", "main")

ALLOW_MAINNET_PK = "ARCUS_ALLOW_MAINNET_PRIVATE_KEY"

_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PK_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")

REDACTED = "<redacted>"


def _norm(name: str) -> str:
    return name.strip().upper()


def _infer_network(name: str) -> str:
    low = name.strip().lower()
    for prefix in _MAINNET_PREFIXES:
        if low.startswith(prefix):
            return "mainnet"
    for prefix in _TESTNET_PREFIXES:
        if low.startswith(prefix):
            return "testnet"
    # Unknown naming: default to the safe network rather than guessing mainnet.
    return "testnet"


@dataclass
class WalletProfile:
    """One named set of credentials. Secrets are never rendered."""

    name: str
    address: str = ""
    api_secret: str = ""
    network: str = "testnet"
    account_index: int = 0
    label: str = ""
    # Present only when explicitly requested; otherwise always "".
    private_key: str = ""
    has_private_key: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def is_mainnet(self) -> bool:
        return self.network == "mainnet"

    @property
    def usable(self) -> bool:
        """Enough to trade: an address and a signing key, both well-formed."""
        return bool(self.address and self.api_secret and not self.problems)

    def short_address(self) -> str:
        if not self.address:
            return "(none)"
        return f"{self.address[:6]}…{self.address[-4:]}"

    def describe(self) -> str:
        bits = [f"{self.name}", f"{self.network}", self.short_address()]
        if self.label:
            bits.append(self.label)
        if self.has_private_key:
            bits.append("HAS WALLET KEY")
        if not self.usable:
            bits.append("UNUSABLE")
        return " · ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "network": self.network,
            "address": self.address,
            "accountIndex": self.account_index,
            "label": self.label,
            "usable": self.usable,
            "hasPrivateKey": self.has_private_key,
            # Deliberately never the values themselves.
            "apiSecret": REDACTED if self.api_secret else "",
            "privateKey": REDACTED if self.has_private_key else "",
            "problems": self.problems,
        }


def _validate(profile: WalletProfile) -> None:
    if profile.address and not _ADDR_RE.match(profile.address):
        profile.problems.append(
            f"ARCUS_ADDRESS{_suffix(profile.name)} must be 0x + 40 hex chars")
    if profile.api_secret and not _HEX64_RE.match(profile.api_secret):
        profile.problems.append(
            f"ARCUS_API_SECRET{_suffix(profile.name)} must be 64 hex chars")
    if not profile.address:
        profile.problems.append(f"ARCUS_ADDRESS{_suffix(profile.name)} is not set")
    if not profile.api_secret:
        profile.problems.append(f"ARCUS_API_SECRET{_suffix(profile.name)} is not set")
    if profile.network not in {"testnet", "mainnet"}:
        profile.problems.append(
            f"ARCUS_NETWORK{_suffix(profile.name)}={profile.network!r} "
            f"must be testnet or mainnet")


def _suffix(name: str) -> str:
    return "" if name == "default" else f"_{_norm(name)}"


def discover_profiles(env: dict[str, str] | None = None) -> list[str]:
    """Names of every profile defined in the environment, plus 'default'."""
    src = env if env is not None else os.environ
    names: set[str] = set()
    for key in src:
        if key.startswith(_ADDRESS + "_"):
            names.add(key[len(_ADDRESS) + 1:].lower())
        elif key.startswith(_SECRET + "_"):
            names.add(key[len(_SECRET) + 1:].lower())
    out = sorted(names)
    if src.get(_ADDRESS) or src.get(_SECRET):
        out.insert(0, "default")
    return out


def load_profile(
    name: str = "default",
    env: dict[str, str] | None = None,
    *,
    require_private_key: bool = False,
) -> WalletProfile:
    """Load one profile. Secrets are only read when needed.

    ``require_private_key`` opts in to reading ``ARCUS_PRIVATE_KEY_*``. Without
    it the field stays empty even when set, so ordinary trading can never
    accidentally hold a withdrawal-capable key in memory.
    """
    src = dict(env) if env is not None else dict(os.environ)
    name = (name or "default").strip() or "default"
    if name != "default" and not _NAME_RE.match(name):
        profile = WalletProfile(name=name)
        profile.problems.append(
            f"wallet name {name!r} may only contain letters, digits and underscores")
        return profile

    sfx = _suffix(name)
    network = str(src.get(_NETWORK + sfx) or "").strip().lower()
    if not network:
        network = _infer_network(name) if name != "default" else \
            str(src.get(_NETWORK) or "testnet").strip().lower()

    try:
        index = int(str(src.get(_INDEX + sfx) or src.get(_INDEX) or 0))
    except ValueError:
        index = 0

    profile = WalletProfile(
        name=name,
        address=str(src.get(_ADDRESS + sfx) or "").strip(),
        api_secret=str(src.get(_SECRET + sfx) or "").strip(),
        network=network,
        account_index=index,
        label=str(src.get(_LABEL + sfx) or "").strip(),
    )
    _validate(profile)

    raw_pk = str(src.get(_PRIVATE + sfx) or "").strip()
    profile.has_private_key = bool(raw_pk)
    if raw_pk and require_private_key:
        if not _PK_RE.match(raw_pk):
            profile.problems.append(
                f"ARCUS_PRIVATE_KEY{sfx} must be 64 hex chars (with or without 0x)")
        elif profile.is_mainnet and not _truthy(src.get(ALLOW_MAINNET_PK)):
            # A mainnet wallet key can drain the account. Loading one must be a
            # separate, conscious decision from merely having it in the file.
            profile.problems.append(
                f"refusing to load a MAINNET wallet private key for profile "
                f"{name!r}: set {ALLOW_MAINNET_PK}=true to allow it")
        else:
            profile.private_key = raw_pk

    return profile


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_all(env: dict[str, str] | None = None) -> list[WalletProfile]:
    return [load_profile(n, env) for n in discover_profiles(env)]


def apply_to_config(cfg: Any, profile: WalletProfile) -> list[str]:
    """Point a Config at this profile. Returns human-readable changes."""
    changes: list[str] = []
    if profile.address and profile.address != cfg.address:
        changes.append(f"address -> {profile.short_address()}")
        cfg.address = profile.address
    if profile.api_secret:
        cfg.api_secret = profile.api_secret
        changes.append("api secret -> profile key")
    if profile.network and profile.network != cfg.network:
        changes.append(f"network -> {profile.network}")
        cfg.network = profile.network
        # URLs follow the network, otherwise a testnet profile would keep
        # talking to whatever host the previous config had.
        from .config import MAINNET_REST, MAINNET_WS, TESTNET_REST, TESTNET_WS
        cfg.rest_url = MAINNET_REST if profile.is_mainnet else TESTNET_REST
        cfg.ws_url = MAINNET_WS if profile.is_mainnet else TESTNET_WS
    if profile.account_index != cfg.account_index:
        changes.append(f"account index -> {profile.account_index}")
        cfg.account_index = profile.account_index
    return changes


def format_table(profiles: Iterable[WalletProfile]) -> str:
    rows = list(profiles)
    if not rows:
        return ("no wallet profiles found.\n"
                "Add ARCUS_ADDRESS_<NAME> and ARCUS_API_SECRET_<NAME> to .env, "
                "e.g. ARCUS_ADDRESS_T1 / ARCUS_API_SECRET_T1, then use --wallet t1.")
    out = [f"{'name':<10} {'network':<9} {'address':<16} {'status':<10} notes",
           "-" * 78]
    for p in rows:
        status = "ok" if p.usable else "unusable"
        notes = []
        if p.label:
            notes.append(p.label)
        if p.has_private_key:
            notes.append("WALLET PRIVATE KEY PRESENT")
        if p.problems:
            notes.append(p.problems[0])
        out.append(f"{p.name:<10} {p.network:<9} {p.short_address():<16} "
                   f"{status:<10} {'; '.join(notes)[:40]}")
    warn = [p.name for p in rows if p.has_private_key]
    if warn:
        out.append("")
        verb = "holds" if len(warn) == 1 else "hold"
        out.append(f"WARNING: {', '.join(warn)} {verb} a wallet private key, which "
                   f"can withdraw funds.")
        out.append("         The bot does not need one to trade. Remove it unless "
                   "onboarding.")
    return "\n".join(out)
