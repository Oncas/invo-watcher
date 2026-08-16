from __future__ import annotations

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
    min_order_usd: float = 1.0,
    leverage: float = 1.0,
    coin_free: bool = False,
) -> bool:
    """True when cached book says a queued open/add can fill — no order yet."""
    why = (reason or "").lower()
    if why == "price":
        if mid is None or mid <= 0:
            return False
        return price_is_at_or_better(
            mid=mid, trader_entry=trader_entry, is_long=is_long
        )
    if why == "margin":
        if available is None:
            return False
        lev = max(1.0, float(leverage or 1))
        return (
            available + 1e-9 >= float(min_order_usd)
            and available * 0.98 * lev + 1e-9 >= float(hl_min_order_usd)
        )
    if why in {"size", "dust"}:
        return leftover_notional + 1e-9 >= float(hl_min_order_usd)
    if why == "same_coin":
        return bool(coin_free)
    return False


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
