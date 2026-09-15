from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryDecision:
    action: str  # "close" | "park"
    reason: str
    tp: float | None = None
    sl: float | None = None
    our_pnl_pct: float | None = None
    trader_pnl_pct: float | None = None


def unrealized_pnl_pct(entry: float, mark: float, is_long: bool) -> float:
    if entry <= 0:
        raise ValueError("entry must be positive")
    if is_long:
        return (mark - entry) / entry
    return (entry - mark) / entry


def price_at_pnl(entry: float, pnl_pct: float, is_long: bool) -> float:
    if is_long:
        return entry * (1.0 + pnl_pct)
    return entry * (1.0 - pnl_pct)


def adverse_sl_price(
    mid: float,
    sl_pct: float,
    is_long: bool,
    leverage: float = 1.0,
) -> float:
    """``sl_pct`` is ROE / margin points (e.g. 3.0 → 3% of our equity on this clip).

    Price distance is ``sl_pct / leverage`` so 3x and 40x risk the same cash.
    """
    lev = max(1.0, float(leverage or 1))
    frac = abs(sl_pct) / 100.0 / lev
    if is_long:
        return mid * (1.0 - frac)
    return mid * (1.0 + frac)


def looks_like_unlevered_park_sl(
    sl: float,
    mid: float,
    *,
    sl_pct: float,
    leverage: float,
) -> bool:
    """True when a parked SL is still the old price-% stop (not ROE / lev)."""
    if mid <= 0 or sl <= 0:
        return False
    lev = max(1.0, float(leverage or 1))
    if lev <= 1.0 + 1e-9:
        return False
    frac = abs(float(sl) - mid) / mid
    unlev = abs(sl_pct) / 100.0
    levered = unlev / lev
    return frac > levered * 1.2 and abs(frac - unlev) < 0.012


def tp_already_reached(mid: float, tp: float, is_long: bool) -> bool:
    if is_long:
        return mid >= tp
    return mid <= tp


def decide_missed_close(
    *,
    our_entry: float,
    mid: float,
    is_long: bool,
    trader_entry: float | None,
    trader_close: float | None,
    recovery_sl_pct: float,
    leverage: float = 1.0,
) -> RecoveryDecision:
    """Decide flatten-now vs park reduce-only TP/SL for a missed Involio close.

    ``trader_entry`` must be the trader's entry *as of when we copied*, not their
    average entry at close. A trader who scales into a loser walks their average
    toward the price, so their loss at exit shrinks while ours cannot -- we hold
    the one clip. Measuring them from the moved average makes us look redder than
    them on nothing but the scale-in, which parks a position that should have
    followed them out.
    """
    our_pnl = unrealized_pnl_pct(our_entry, mid, is_long)
    sl = adverse_sl_price(mid, recovery_sl_pct, is_long, leverage)

    if our_pnl >= 0:
        return RecoveryDecision(
            action="close",
            reason="winning",
            our_pnl_pct=our_pnl,
        )

    trader_pnl: float | None = None
    if (
        trader_entry is not None
        and trader_close is not None
        and trader_entry > 0
        and trader_close > 0
    ):
        trader_pnl = unrealized_pnl_pct(trader_entry, trader_close, is_long)

    # Losing + trader green/unknown → TP at our entry (breakeven).
    if trader_pnl is None or trader_pnl >= 0:
        tp = our_entry
        if tp_already_reached(mid, tp, is_long):
            return RecoveryDecision(
                action="close",
                reason="already_past_breakeven_tp",
                tp=tp,
                our_pnl_pct=our_pnl,
                trader_pnl_pct=trader_pnl,
            )
        return RecoveryDecision(
            action="park",
            reason="losing_trader_green_or_unknown",
            tp=tp,
            sl=sl,
            our_pnl_pct=our_pnl,
            trader_pnl_pct=trader_pnl,
        )

    # Trader closed red: if we are less red (or equal), take the smaller loss now.
    if our_pnl >= trader_pnl:
        return RecoveryDecision(
            action="close",
            reason="less_red_than_trader",
            our_pnl_pct=our_pnl,
            trader_pnl_pct=trader_pnl,
        )

    # We are redder → park TP at trader's loss % applied to our entry.
    tp = price_at_pnl(our_entry, trader_pnl, is_long)
    if tp_already_reached(mid, tp, is_long):
        return RecoveryDecision(
            action="close",
            reason="already_past_trader_tp",
            tp=tp,
            our_pnl_pct=our_pnl,
            trader_pnl_pct=trader_pnl,
        )
    return RecoveryDecision(
        action="park",
        reason="losing_worse_than_trader",
        tp=tp,
        sl=sl,
        our_pnl_pct=our_pnl,
        trader_pnl_pct=trader_pnl,
    )
