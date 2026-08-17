from __future__ import annotations

import math
from typing import Any


def queued_ready_locally(
    *,
    reason: str,
    mid: float | None = None,
    available: float | None = None,
    trader_entry: float | None = None,
    is_long: bool = True,
    leftover_notional: float = 0.0,
    hl_min_order_usd: float = 10.0,
    leverage: float = 1.0,
    coin_free: bool = False,
    need_margin: float | None = None,
) -> bool:
    """True when cached book says a queued open/add can fill — no order yet."""
    why = (reason or "").lower()
    if why == "price":
        if mid is None or mid <= 0:
            return False
        return price_is_at_or_better(
            mid=mid, trader_entry=trader_entry, is_long=is_long
        )
    if why in {"margin", "size", "dust", "below_hl_min"}:
        if available is None:
            return False
        if need_margin is not None:
            return available + 1e-9 >= float(need_margin)
        lev = max(1.0, float(leverage or 1))
        return available * 0.98 * lev + 1e-9 >= float(hl_min_order_usd)
    if why == "same_coin":
        return bool(coin_free)
    return False


def classify_pending_wait(
    *,
    coin_free: bool,
    mid: float | None = None,
    trader_entry: float | None = None,
    is_long: bool = True,
    available: float | None = None,
    leverage: float = 1.0,
    hl_min_order_usd: float = 10.0,
    need_margin: float | None = None,
    can_spend: bool | None = None,
    prior: str | None = None,
) -> str:
    """Current queue token from the live book — not the error that first queued it.

    First blocker that actually stops *this* row:
    same_coin → price → margin (cash < this clip's need) → ratelimit.
    A 20% open can be margin while a $10 bump on the same book is ratelimit
    or price. Rows that are ready to send all share ratelimit when the cap is hit.
    """
    if not coin_free:
        return "same_coin"
    if mid is not None and mid > 0 and not price_is_at_or_better(
        mid=mid, trader_entry=trader_entry, is_long=is_long
    ):
        return "price"
    if available is not None:
        need = need_margin
        if need is None:
            lev = max(1.0, float(leverage or 1))
            need = float(hl_min_order_usd) / lev
        if available + 1e-9 < float(need):
            return "margin"
    if can_spend is False:
        return "ratelimit"
    prior = (prior or "").strip().lower()
    if prior == "budget":
        return "ratelimit"
    if prior in {"size", "dust", "below_hl_min", "under min"}:
        return "margin"
    return prior or "margin"


def size_clearing_min_notional(
    *,
    mid: float,
    notional: float,
    min_notional: float,
    sz_decimals: int,
) -> float:
    """Floor-round size, then add lots until mid×sz still clears HL's min.

    Targeting exactly $10 and then flooring the lot is what put ETH/BTC under
    the minimum. One extra lot is a few cents of notional, not a new wait state.
    """
    if mid <= 0:
        return 0.0
    factor = 10 ** max(0, int(sz_decimals))
    sz = math.floor(max(0.0, float(notional)) / mid * factor + 1e-12) / factor
    floor = float(min_notional)
    if floor <= 0:
        return sz
    for _ in range(8):
        if sz * mid + 1e-9 >= floor:
            return sz
        sz = math.floor((sz + 1.0 / factor) * factor + 1e-12) / factor
    return sz


def price_is_at_or_better(
    *,
    mid: float,
    trader_entry: float | None,
    is_long: bool,
) -> bool:
    """True if we can IoC now without paying worse than the trader's entry.

    No trader entry → treat as ok (caller falls back to mid IoC).
    """
    if trader_entry is None or trader_entry <= 0 or mid <= 0:
        return True
    if is_long:
        return mid <= trader_entry
    return mid >= trader_entry


def fill_gaps(
    *,
    our_px: float,
    trader_entry: float | None,
    is_long: bool,
    leverage: float,
) -> tuple[float | None, float | None]:
    """Return (price_gap_pct, roe_gap_pct). Positive = we are worse."""
    if trader_entry is None or trader_entry <= 0 or our_px <= 0:
        return None, None
    if is_long:
        price_gap_pct = (our_px - trader_entry) / trader_entry * 100.0
    else:
        price_gap_pct = (trader_entry - our_px) / trader_entry * 100.0
    return price_gap_pct, price_gap_pct * max(1.0, float(leverage))


def extract_avg_px(hl_resp: dict[str, Any] | None) -> float | None:
    if not hl_resp:
        return None
    data = hl_resp.get("response", {}).get("data", {})
    for st in data.get("statuses") or []:
        if not isinstance(st, dict):
            continue
        filled = st.get("filled") or {}
        raw = filled.get("avgPx")
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def ioc_unfilled_message(error: str) -> bool:
    msg = error.lower()
    return (
        "could not immediately match" in msg
        or "ioc" in msg and "unfill" in msg
        or "no fill" in msg
    )
