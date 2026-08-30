from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

# hyperliquid-python-sdk Exchange.market_close DEFAULT_SLIPPAGE
HL_MARKET_CLOSE_SLIPPAGE = 0.05


def min_notional_target(min_notional: float) -> float:
    """Notional to aim for when an order would otherwise sit on HL's floor.

    HL rejects below ``min_notional``, and it judges the order with its own
    price, not the mid we sized against. Targeting the floor exactly leaves
    nothing for that disagreement or for lot rounding, so a $10.16 clip comes
    back "Order must have minimum value of $10". Aim over it instead.
    """
    return max(float(min_notional) * 1.2, 12.0)


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


def sell_close_top_up_sz(
    *,
    mid: float,
    sz: float,
    slippage: float,
    min_notional: float,
    sz_decimals: int,
) -> float:
    """Coins to buy so an IoC sell at ``mid*(1-slip)`` still clears HL's min.

    The add is itself an HL order, so it must also clear ``min_notional``. A
    $1 dust buy is rejected. Buffer above $10 so coins×limit cannot land on
    $9.999 after rounding.
    """
    if mid <= 0 or sz <= 0:
        return 0.0
    sell_px = mid * (1.0 - max(0.0, float(slippage)))
    if sell_px <= 0:
        return 0.0
    if sz * sell_px + 1e-9 >= float(min_notional):
        return 0.0
    return size_clearing_min_notional(
        mid=mid,
        notional=min_notional_target(min_notional),
        min_notional=float(min_notional),
        sz_decimals=sz_decimals,
    )


def flatten_limit_px(
    *,
    mid: float,
    sz: float,
    is_buy: bool,
    slippage: float = HL_MARKET_CLOSE_SLIPPAGE,
    min_notional: float = 10.0,
    round_price: Callable[[float], float],
) -> float:
    """IoC cap for a flatten: SDK 5% slip, then raise *buys* until sz×px ≥ $10.

    HL scores the ticket as coins × this limit, not entry notional. A winning
    min-clip short (0.57 COMP @ 17.20) is ~$9.81 at mid and can sit under $10
    at 2% slip. Raising the buy cap is more aggressive and still fills.

    Do not raise a sell the same way. ``$10/sz`` on a losing min-clip long sits
    above mid (and above the bid), so IoC cannot match. ``close_position``
    tops up size first; this then keeps the crossing slip price.
    """
    if mid <= 0 or sz <= 0:
        return float(mid) if mid > 0 else 0.0
    px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
    px = float(round_price(px))
    floor = float(min_notional)
    if floor <= 0 or sz * px + 1e-9 >= floor:
        return px
    if not is_buy:
        return px
    need = floor / sz
    px = max(px, need)
    px = float(round_price(px))
    for _ in range(8):
        if sz * px + 1e-9 >= floor:
            return px
        bumped = px * 1.0001 if px > 0 else need
        nxt = float(round_price(bumped))
        if nxt <= px:
            nxt = float(round_price(px + max(abs(px) * 1e-4, 1e-6)))
        if nxt <= px:
            break
        px = nxt
    return px


def plan_resting_adopt(
    *,
    posted_sz: float,
    filled_sz: float,
    booked_sz: float,
    hl_sz: float,
    only_copy: bool,
) -> tuple[float, bool]:
    """Size to store for a resting GTC, and whether leftover can still fill.

    Never shrink ``posted_sz`` — leftover is posted minus held. Held is the
    best of oid fills and the booked copy; when this is the only copy on the
    coin, also take live HL (the rest of our GTC may have filled untracked).
    Keep pending while held is short of posted. Do not trust a missing
    open-orders row: that is how LINK 0.6/1.1 finalized early.
    """
    held = max(float(filled_sz or 0.0), float(booked_sz or 0.0), 0.0)
    if only_copy:
        held = max(held, float(hl_sz or 0.0))
    leftover = max(0.0, float(posted_sz or 0.0) - held)
    return held, leftover > 1e-9


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
