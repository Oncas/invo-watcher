"""Involio-stamped Hyperliquid client order ids (cloids).

The mobile/web app embeds the source paper-trade ``baseShortId`` plus
trade/order type nibbles into the 16-byte HL ``c`` field. Involio uses that
to attribute a fill as a Mimic (showing @creator on Edit Trade) rather than a
self-created position.
"""

from __future__ import annotations

import re
import threading
import time

from hyperliquid.utils.types import Cloid

# CloidOrderType.xc.c
ORDER_ENTRY = 0
ORDER_TP = 1
ORDER_SL = 2
ORDER_CLOSE = 3
ORDER_SIZE_INCREASE = 4
ORDER_SIZE_REDUCE = 5

# CloidTradeType.LS.c
TRADE_MIMIC = 0
TRADE_MIMIC_MANUAL_EDIT = 1
TRADE_MIMIC_COPY_UPDATE = 2
TRADE_MANUAL = 3

_SHORT_ID_RE = re.compile(r"^[0-9A-Za-z]{10}$")
_lock = threading.Lock()
_last_sec: int = 0
_seq: int = 0


def normalize_base_short_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not _SHORT_ID_RE.match(s):
        return None
    return s


def _next_seq(timestamp_sec: int) -> int:
    global _last_sec, _seq
    with _lock:
        if timestamp_sec != _last_sec:
            _last_sec = timestamp_sec
            _seq = 0
        cur = _seq & 0xFF
        _seq = (_seq + 1) & 0xFF
        return cur


def stamp_cloid(
    base_short_id: str | None,
    *,
    order_type: int,
    trade_type: int,
    timestamp_sec: int | None = None,
    seq: int | None = None,
) -> Cloid | None:
    """Build an Involio v1 cloid, or None if short id is missing/invalid."""
    short = normalize_base_short_id(base_short_id)
    if short is None:
        return None

    ts = int(time.time()) if timestamp_sec is None else int(timestamp_sec)
    if ts < 0 or ts > 0xFFFFFFFF:
        raise ValueError(f"timestampSec out of u32 range: {ts}")
    n = _next_seq(ts) if seq is None else int(seq) & 0xFF

    raw = bytearray(16)
    raw[0:10] = short.encode("ascii")
    raw[10] = (ts >> 24) & 0xFF
    raw[11] = (ts >> 16) & 0xFF
    raw[12] = (ts >> 8) & 0xFF
    raw[13] = ts & 0xFF
    raw[14] = ((order_type & 0xF) << 4) | (trade_type & 0xF)
    raw[15] = n
    return Cloid.from_str("0x" + raw.hex())


def mimic_entry_cloid(source_base_short_id: str | None) -> Cloid | None:
    """Cloid used when Mimic-opening a portfolio trade."""
    return stamp_cloid(
        source_base_short_id,
        order_type=ORDER_ENTRY,
        trade_type=TRADE_MIMIC,
    )


def mimic_close_cloid(base_short_id: str | None) -> Cloid | None:
    """Cloid used when closing a mimicked (or source-linked) position."""
    return stamp_cloid(
        base_short_id,
        order_type=ORDER_CLOSE,
        trade_type=TRADE_MIMIC,
    )


def mimic_tp_cloid(
    base_short_id: str | None, *, copy_update: bool = False
) -> Cloid | None:
    """TP trigger cloid — copy_update matches pressing Copy on a trade update."""
    return stamp_cloid(
        base_short_id,
        order_type=ORDER_TP,
        trade_type=TRADE_MIMIC_COPY_UPDATE if copy_update else TRADE_MIMIC,
    )


def mimic_sl_cloid(
    base_short_id: str | None, *, copy_update: bool = False
) -> Cloid | None:
    """SL trigger cloid — copy_update matches pressing Copy on a trade update."""
    return stamp_cloid(
        base_short_id,
        order_type=ORDER_SL,
        trade_type=TRADE_MIMIC_COPY_UPDATE if copy_update else TRADE_MIMIC,
    )


def mimic_size_increase_cloid(
    base_short_id: str | None, *, copy_update: bool = True
) -> Cloid | None:
    """Size-increase cloid — copy_update matches Copy on Position Increased."""
    return stamp_cloid(
        base_short_id,
        order_type=ORDER_SIZE_INCREASE,
        trade_type=TRADE_MIMIC_COPY_UPDATE if copy_update else TRADE_MIMIC,
    )


def mimic_size_reduce_cloid(
    base_short_id: str | None, *, copy_update: bool = True
) -> Cloid | None:
    """Size-reduce cloid — copy_update matches Copy on a size cut."""
    return stamp_cloid(
        base_short_id,
        order_type=ORDER_SIZE_REDUCE,
        trade_type=TRADE_MIMIC_COPY_UPDATE if copy_update else TRADE_MIMIC,
    )
