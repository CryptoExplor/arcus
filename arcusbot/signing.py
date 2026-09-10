"""Ed25519 request signing for the Arcus API.

Two schemes exist (https://docs.arcus.xyz/api-reference/authentication):

Scheme 1 — typed canonical payload. Used by placeOrder (op 1), cancelOrder
    (op 2), modifyOrder (op 3) and untriggered TPSL (op 4). The signed message
    IS the compact, key-sorted JSON payload built from engine-native integers.
    No prefix, no timestamp concatenation: the timestamp lives inside as ``ct``
    and must equal the ``X-Timestamp`` header.

Scheme 2 — legacy message. Used by cancelAllOrders, setLeverage,
    scheduleCancelAllDeadMansSwitch and the WebSocket ``authenticate`` call:

        signature = ed25519(str(timestamp_ns) + action + canonical_json(body))

Batches are NOT a third scheme: every element is signed as its own Scheme 1
payload sharing one ``X-Timestamp`` as its ``ct``, and each element carries its
own ``signature`` field. The envelope ``X-Signature`` header must still be
present (its value is not verified) — set it to any element's signature.

Field notes that bite in practice:
  * ``ad`` is the ONLY case-folded field (lowercased before signing). ``c``
    (clientId) and ``id`` (orderId) are signed byte-for-byte as sent.
  * ``c`` is omitted entirely when empty.
  * ``g`` is goodTilTime in NANOseconds = body ``goodTilTime`` (µs) * 1000, and
    must be >= ~1 month in the future on EVERY order, including IOC and FOK.
  * ``r`` is an integer 0/1, not a JSON boolean.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

__all__ = [
    "SIDE",
    "TIF",
    "OP_PLACE",
    "OP_CANCEL",
    "OP_MODIFY",
    "OP_PLACE_UNTRIGGERED",
    "canonical_json",
    "compact_typed",
    "now_ns",
    "now_us",
    "good_til_us",
    "Signer",
    "place_payload",
    "cancel_payload",
    "modify_payload",
]

SIDE = {"BUY": 0, "SELL": 1}
TIF = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}

OP_PLACE = 1
OP_CANCEL = 2
OP_MODIFY = 3
OP_PLACE_UNTRIGGERED = 4

# goodTilTime must be at least one month ahead when the gateway handles the
# order. 45 days gives a wide margin for clock skew and queueing.
DEFAULT_GTT_DAYS = 45


def now_ns() -> int:
    return time.time_ns()


def now_us() -> int:
    return time.time_ns() // 1_000


def good_til_us(days: int = DEFAULT_GTT_DAYS) -> int:
    """Epoch MICROSECONDS for the request body's ``goodTilTime``."""
    return now_us() + int(days) * 86_400 * 1_000_000


def canonical_json(obj: Any) -> str:
    """Sorted-key, whitespace-free JSON — the Scheme 2 body serialization."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def compact_typed(fields: Mapping[str, Any]) -> str:
    """Scheme 1 canonical payload: sorted keys, no whitespace, None dropped."""
    return canonical_json({k: v for k, v in fields.items() if v is not None})


class Signer:
    """Holds the Ed25519 private key; the public half IS the API key."""

    def __init__(self, private_key_hex: str) -> None:
        raw = bytes.fromhex(private_key_hex.removeprefix("0x").strip())
        if len(raw) != 32:
            raise ValueError("Arcus API signing key must be 32 bytes of hex (64 chars)")
        self._key = Ed25519PrivateKey.from_private_bytes(raw)

    @property
    def api_key(self) -> str:
        """Hex-encoded Ed25519 public key — the ``X-API-Key`` value."""
        return self._key.public_key().public_bytes_raw().hex()

    def sign(self, message: str | bytes) -> str:
        data = message.encode() if isinstance(message, str) else message
        return self._key.sign(data).hex()

    def sign_typed(self, fields: Mapping[str, Any]) -> tuple[str, str]:
        """Scheme 1. Returns (canonical_payload, signature_hex)."""
        payload = compact_typed(fields)
        return payload, self.sign(payload)

    def sign_legacy(self, timestamp_ns: int, action: str, body: Mapping[str, Any]) -> str:
        """Scheme 2: ed25519(timestamp + action + canonical_json(body))."""
        return self.sign(f"{timestamp_ns}{action}{canonical_json(body)}")

    def headers(self, timestamp_ns: int, signature: str) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "X-Timestamp": str(timestamp_ns),
            "X-Signature": signature,
        }


# --------------------------------------------------------------------------- #
# Scheme 1 payload builders. `price_ticks` / `size_quantums` are already
# converted by arcusbot.scaling so this module stays free of market lookups.
# --------------------------------------------------------------------------- #


def place_payload(
    *,
    address: str,
    account_index: int,
    ts_ns: int,
    gtt_us: int,
    market_id: int,
    price_ticks: int,
    size_quantums: int,
    side: str,
    tif: str,
    reduce_only: bool = False,
    client_id: str | None = None,
    untriggered: bool = False,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "g": int(gtt_us) * 1_000,
        "m": int(market_id),
        "op": OP_PLACE_UNTRIGGERED if untriggered else OP_PLACE,
        "p": int(price_ticks),
        "q": int(size_quantums),
        "r": 1 if reduce_only else 0,
        "s": SIDE[side],
        "t": TIF[tif],
        "v": 1,
    }
    if client_id:
        fields["c"] = client_id
    return fields


def cancel_payload(
    *,
    address: str,
    account_index: int,
    ts_ns: int,
    market_id: int,
    order_id: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    if bool(order_id) == bool(client_id):
        raise ValueError("cancel requires exactly one of order_id or client_id")
    fields: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "m": int(market_id),
        "op": OP_CANCEL,
        "v": 1,
    }
    if order_id:
        fields["id"] = str(order_id)
    if client_id:
        fields["c"] = client_id
    return fields


def modify_payload(
    *,
    address: str,
    account_index: int,
    ts_ns: int,
    gtt_us: int,
    market_id: int,
    order_id: str,
    price_ticks: int,
    size_quantums: int,
    side: str,
    tif: str,
    reduce_only: bool = False,
    client_id: str | None = None,
) -> dict[str, Any]:
    """modifyOrder ALWAYS requires ``id``; g/r/s/t echo the resting order."""
    if not order_id:
        raise ValueError("modify requires the server order_id")
    fields: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "g": int(gtt_us) * 1_000,
        "id": str(order_id),
        "m": int(market_id),
        "op": OP_MODIFY,
        "p": int(price_ticks),
        "q": int(size_quantums),
        "r": 1 if reduce_only else 0,
        "s": SIDE[side],
        "t": TIF[tif],
        "v": 1,
    }
    if client_id:
        fields["c"] = client_id
    return fields
