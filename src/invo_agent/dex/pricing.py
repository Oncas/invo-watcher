from __future__ import annotations

from typing import Any


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
