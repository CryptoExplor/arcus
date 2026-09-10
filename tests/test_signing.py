"""Signing correctness — the part that is unforgiving on a live venue.

If any of these break, the gateway answers `invalid order signature` and no
order ever reaches the matching engine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402

from arcusbot.signing import (  # noqa: E402
    OP_CANCEL,
    OP_MODIFY,
    OP_PLACE,
    Signer,
    canonical_json,
    cancel_payload,
    compact_typed,
    modify_payload,
    place_payload,
)

KEY = "4f" * 32
ADDR = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"


def test_api_key_is_public_half() -> None:
    signer = Signer(KEY)
    assert len(signer.api_key) == 64
    assert Signer("0x" + KEY).api_key == signer.api_key


def test_signature_verifies_against_public_key() -> None:
    signer = Signer(KEY)
    payload = '{"ad":"0x1","ct":1}'
    sig = signer.sign(payload)
    assert len(sig) == 128
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(signer.api_key)).verify(
        bytes.fromhex(sig), payload.encode()
    )


def test_typed_payload_is_sorted_and_compact() -> None:
    fields = place_payload(
        address=ADDR, account_index=0, ts_ns=1712345678000000000,
        gtt_us=4102444800000000, market_id=1, price_ticks=500000,
        size_quantums=100, side="BUY", tif="GTT",
    )
    payload = compact_typed(fields)
    assert " " not in payload
    keys = list(json.loads(payload).keys())
    assert keys == sorted(keys)
    assert payload.startswith('{"ad":')


def test_address_is_lowercased_but_client_id_is_not() -> None:
    """`ad` is the ONLY case-folded field; `c` must be byte-for-byte verbatim."""
    fields = place_payload(
        address=ADDR, account_index=0, ts_ns=1, gtt_us=2, market_id=1,
        price_ticks=1, size_quantums=1, side="BUY", tif="GTT", client_id="MiXeD-Case-1",
    )
    assert fields["ad"] == ADDR.lower()
    assert fields["c"] == "MiXeD-Case-1"


def test_client_id_omitted_when_empty() -> None:
    for empty in (None, ""):
        fields = place_payload(
            address=ADDR, account_index=0, ts_ns=1, gtt_us=2, market_id=1,
            price_ticks=1, size_quantums=1, side="BUY", tif="GTT", client_id=empty,
        )
        assert "c" not in json.loads(compact_typed(fields))


def test_goodtiltime_is_nanoseconds_in_payload() -> None:
    gtt_us = 4102444800000000
    fields = place_payload(
        address=ADDR, account_index=0, ts_ns=1, gtt_us=gtt_us, market_id=1,
        price_ticks=1, size_quantums=1, side="BUY", tif="IOC",
    )
    assert fields["g"] == gtt_us * 1000  # body is microseconds, payload nanoseconds


def test_reduce_only_is_integer_not_boolean() -> None:
    fields = place_payload(
        address=ADDR, account_index=0, ts_ns=1, gtt_us=2, market_id=1,
        price_ticks=1, size_quantums=1, side="SELL", tif="GTT", reduce_only=True,
    )
    assert fields["r"] == 1 and not isinstance(fields["r"], bool)
    assert '"r":1' in compact_typed(fields)


def test_side_and_tif_enums() -> None:
    buy = place_payload(address=ADDR, account_index=0, ts_ns=1, gtt_us=2, market_id=1,
                        price_ticks=1, size_quantums=1, side="BUY", tif="GTT")
    sell = place_payload(address=ADDR, account_index=0, ts_ns=1, gtt_us=2, market_id=1,
                         price_ticks=1, size_quantums=1, side="SELL", tif="ALO")
    assert (buy["s"], buy["t"], buy["op"]) == (0, 0, OP_PLACE)
    assert (sell["s"], sell["t"]) == (1, 3)


def test_cancel_requires_exactly_one_identifier() -> None:
    ok = cancel_payload(address=ADDR, account_index=0, ts_ns=1, market_id=1, order_id="ord-1")
    assert ok["op"] == OP_CANCEL and ok["id"] == "ord-1" and "c" not in ok
    for kwargs in ({}, {"order_id": "a", "client_id": "b"}):
        try:
            cancel_payload(address=ADDR, account_index=0, ts_ns=1, market_id=1, **kwargs)
        except ValueError:
            continue
        raise AssertionError("cancel accepted an invalid identifier combination")


def test_cancel_payload_has_no_order_fields() -> None:
    fields = cancel_payload(address=ADDR, account_index=0, ts_ns=1, market_id=1, client_id="x")
    assert not ({"g", "p", "q", "r", "s", "t"} & set(fields))


def test_modify_always_requires_order_id() -> None:
    fields = modify_payload(
        address=ADDR, account_index=0, ts_ns=1, gtt_us=2, market_id=1, order_id="ord-9",
        price_ticks=5, size_quantums=6, side="BUY", tif="GTT",
    )
    assert fields["op"] == OP_MODIFY and fields["id"] == "ord-9"
    try:
        modify_payload(address=ADDR, account_index=0, ts_ns=1, gtt_us=2, market_id=1,
                       order_id="", price_ticks=1, size_quantums=1, side="BUY", tif="GTT")
    except ValueError:
        return
    raise AssertionError("modify accepted an empty order id")


def test_scheme2_legacy_message_layout() -> None:
    """ed25519(timestamp + action + canonical_json(body)) — no delimiters."""
    signer = Signer(KEY)
    ts, body = 1712345678000000000, {"b": 2, "a": 1}
    expected = f"{ts}cancelAllOrders" + canonical_json(body)
    assert canonical_json(body) == '{"a":1,"b":2}'
    assert signer.sign_legacy(ts, "cancelAllOrders", body) == signer.sign(expected)


def test_batch_elements_share_one_timestamp() -> None:
    ts = 1712345678000000000
    elements = [
        place_payload(address=ADDR, account_index=0, ts_ns=ts, gtt_us=2, market_id=1,
                      price_ticks=p, size_quantums=1, side="BUY", tif="GTT")
        for p in (100, 200, 300)
    ]
    assert {e["ct"] for e in elements} == {ts}


def test_untriggered_tpsl_uses_op_4() -> None:
    fields = place_payload(address=ADDR, account_index=0, ts_ns=1, gtt_us=2, market_id=1,
                           price_ticks=1, size_quantums=1, side="BUY", tif="GTT", untriggered=True)
    assert fields["op"] == 4
