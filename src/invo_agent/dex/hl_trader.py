from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import httpx
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import (
    action_hash,
    construct_phantom_agent,
    get_timestamp_ms,
    l1_payload,
    order_request_to_order_wire,
    order_wires_to_order_action,
)
from hyperliquid.utils.types import Cloid

from invo_agent.dex.turnkey_client import TurnkeySigner


class HlTraderError(RuntimeError):
    pass


# Returned by sync_position_tpsl when resting TP/SL already match — no HL write.
TPSL_UNCHANGED = "unchanged"


def _order_tpsl_kind(order: dict[str, Any]) -> str | None:
    raw = order.get("tpsl")
    if raw in ("tp", "sl"):
        return str(raw)
    order_type = order.get("orderType")
    if isinstance(order_type, dict):
        trigger = order_type.get("trigger") or {}
        raw = trigger.get("tpsl")
        if raw in ("tp", "sl"):
            return str(raw)
    if isinstance(order_type, str):
        low = order_type.lower()
        if "take profit" in low:
            return "tp"
        if "stop" in low:
            return "sl"
    return None


def _order_trigger_px(order: dict[str, Any]) -> float | None:
    for key in ("triggerPx", "trigger_px"):
        raw = order.get(key)
        if raw is None:
            continue
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    order_type = order.get("orderType")
    if isinstance(order_type, dict):
        trigger = order_type.get("trigger") or {}
        raw = trigger.get("triggerPx")
        if raw is not None:
            try:
                px = float(raw)
            except (TypeError, ValueError):
                return None
            if px > 0:
                return px
    return None


def _order_trigger_sz(order: dict[str, Any]) -> float | None:
    raw = order.get("sz") or order.get("origSz")
    if raw is None:
        return None
    try:
        sz = float(raw)
    except (TypeError, ValueError):
        return None
    return sz if sz > 0 else None


def resting_tpsl(orders: list[dict[str, Any]] | None, coin: str) -> tuple[
    float | None, float | None, float | None
]:
    """Return (tp_px, sl_px, trigger_sz) for reduce triggers on coin."""
    coin_u = coin.upper()
    tp: float | None = None
    sl: float | None = None
    sz: float | None = None
    for order in orders or []:
        if str(order.get("coin") or "").upper() != coin_u:
            continue
        if not (order.get("isTrigger") or order.get("isPositionTpsl")):
            continue
        kind = _order_tpsl_kind(order)
        px = _order_trigger_px(order)
        trig_sz = _order_trigger_sz(order)
        if trig_sz is not None:
            sz = trig_sz
        if kind == "tp":
            tp = px
        elif kind == "sl":
            sl = px
    return tp, sl, sz


def tpsl_already_resting(
    *,
    have_tp: float | None,
    have_sl: float | None,
    have_sz: float | None,
    want_tp: float | None,
    want_sl: float | None,
    want_sz: float | None,
) -> bool:
    def _eq(left: float | None, right: float | None) -> bool:
        if left is None and right is None:
            return True
        if left is None or right is None:
            return False
        scale = max(abs(left), abs(right), 1e-12)
        return abs(left - right) <= max(1e-8, 1e-6 * scale)

    if not _eq(have_tp, want_tp) or not _eq(have_sl, want_sl):
        return False
    if want_sz is not None and have_sz is not None and not _eq(have_sz, want_sz):
        return False
    return True


class InsufficientMarginError(HlTraderError):
    """Raised when HL has no free margin for the intended order (expected, not a bug)."""

    pass


class SameCoinOpenError(HlTraderError):
    """Raised when we already track an open copy on this coin (one position per coin)."""

    pass


class NotOpenCopyError(HlTraderError):
    """CLOSE/UPDATE for an investment we never opened (or already closed)."""

    pass


class PriceNotBetterError(HlTraderError):
    """Mid is worse than the trader's entry — queue instead of chasing."""

    pass


class IocUnfilledError(HlTraderError):
    """IoC at trader entry did not fill — stay pending."""

    pass


class SideMismatchError(HlTraderError):
    """HL side does not match the tracked copy — do not trade or drop the row."""

    pass


class GhostCopyError(HlTraderError):
    """Tracked copy is flat on HL; local row was cleared."""

    pass


@dataclass
class HlBookSnapshot:
    """Last info-API read: mids + free margin. Not an order."""

    mids: dict[str, float]
    available: float
    equity: float
    at: float


class TurnkeyWalletAdapter:
    """Minimal wallet duck-type for hyperliquid sign_inner / LocalAccount.sign_message."""

    def __init__(self, signer: TurnkeySigner):
        self._signer = signer

    def sign_message(self, signable_message) -> dict[str, Any]:
        # Rebuild typed data is awkward from SignableMessage; we sign via custom path.
        raise NotImplementedError("Use HlTrader.sign_action instead")


class HlTrader:
    def __init__(self, signer: TurnkeySigner, address: str, *, mainnet: bool = True):
        self.signer = signer
        self.address = address
        self.mainnet = mainnet
        self.base_url = constants.MAINNET_API_URL if mainnet else constants.TESTNET_API_URL
        self.info = Info(self.base_url, skip_ws=True)
        self._http = httpx.Client(timeout=30.0)
        self._book: HlBookSnapshot | None = None

    def close(self) -> None:
        self._http.close()

    def refresh_book(self) -> HlBookSnapshot:
        """One all_mids + user_state. Used to decide drains without placing."""
        raw_mids = self.info.all_mids() or {}
        mids = {str(k).upper(): float(v) for k, v in raw_mids.items() if v is not None}
        state = self.info.user_state(self.address)
        summary = state.get("crossMarginSummary") or state.get("marginSummary") or {}
        equity = float(summary.get("accountValue") or 0)
        used = float(summary.get("totalMarginUsed") or 0)
        self._book = HlBookSnapshot(
            mids=mids,
            available=max(0.0, equity - used),
            equity=equity,
            at=time.monotonic(),
        )
        return self._book

    def cached_mid(self, coin: str) -> float | None:
        if self._book is None:
            return None
        px = self._book.mids.get(coin.upper())
        return float(px) if px is not None and float(px) > 0 else None

    def cached_available(self) -> float | None:
        if self._book is None:
            return None
        return float(self._book.available)

    def account_value(self) -> float:
        state = self.info.user_state(self.address)
        summary = state.get("marginSummary") or {}
        return float(summary.get("accountValue") or 0)

    def available_margin(self) -> float:
        """Free cross margin ≈ accountValue − totalMarginUsed.

        Do not clamp to ``withdrawable`` — HL often reports withdrawable=0 while
        open positions exist even though new cross orders can still use free margin.
        """
        state = self.info.user_state(self.address)
        summary = state.get("crossMarginSummary") or state.get("marginSummary") or {}
        account_value = float(summary.get("accountValue") or 0)
        margin_used = float(summary.get("totalMarginUsed") or 0)
        return max(0.0, account_value - margin_used)

    def asset_index(self, coin: str) -> int:
        return int(self.info.name_to_asset(coin))

    def mid_price(self, coin: str) -> float:
        mids = self.info.all_mids()
        px = mids.get(coin)
        if px is None:
            raise HlTraderError(f"No mid price for {coin}")
        return float(px)

    def sz_decimals(self, coin: str) -> int:
        # Newer hyperliquid-python-sdk: asset index → sz decimals
        return int(self.info.asset_to_sz_decimals[self.asset_index(coin)])

    def round_size(self, coin: str, sz: float) -> float:
        decimals = self.sz_decimals(coin)
        factor = 10**decimals
        return math.floor(sz * factor) / factor

    def round_price(self, coin: str, px: float) -> float:
        """Match hyperliquid-python-sdk Exchange._slippage_price rounding."""
        asset = self.asset_index(coin)
        is_spot = asset >= 10_000
        decimals = (8 if is_spot else 6) - self.sz_decimals(coin)
        return round(float(f"{px:.5g}"), decimals)

    def slippage_price(self, coin: str, is_buy: bool, slippage: float = 0.02) -> float:
        mid = self.mid_price(coin)
        px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
        return self.round_price(coin, px)

    def sign_action(self, action: dict[str, Any], nonce: int) -> dict[str, Any]:
        digest = action_hash(action, None, nonce, None)
        phantom = construct_phantom_agent(digest, self.mainnet)
        typed = l1_payload(phantom)
        return self.signer.sign_eip712(typed)

    def post_action(self, action: dict[str, Any], signature: dict[str, Any], nonce: int) -> dict:
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
        }
        response = self._http.post(f"{self.base_url}/exchange", json=payload)
        try:
            data = response.json()
        except Exception as exc:
            raise HlTraderError(f"HL non-JSON ({response.status_code}): {response.text}") from exc
        if response.status_code >= 400:
            raise HlTraderError(f"HL exchange error ({response.status_code}): {data}")
        return data

    def update_leverage(self, coin: str, leverage: int, *, is_cross: bool = True) -> dict:
        nonce = get_timestamp_ms()
        action = {
            "type": "updateLeverage",
            "asset": self.asset_index(coin),
            "isCross": is_cross,
            "leverage": int(leverage),
        }
        sig = self.sign_action(action, nonce)
        return self.post_action(action, sig, nonce)

    def market_order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        *,
        reduce_only: bool = False,
        slippage: float = 0.02,
        limit_px: float | None = None,
        cloid: Cloid | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        """Place IoC order. Returns (hl_order_payload, hl_response, nonce).

        ``limit_px`` overrides the mid±slippage cap (used for trader-entry-or-better).
        """
        if limit_px is not None:
            limit_px = self.round_price(coin, float(limit_px))
        else:
            limit_px = self.slippage_price(coin, is_buy, slippage)
        sz = self.round_size(coin, sz)
        if sz <= 0:
            raise HlTraderError(f"Size rounded to zero for {coin}")

        order_req: dict[str, Any] = {
            "coin": coin,
            "is_buy": is_buy,
            "sz": sz,
            "limit_px": limit_px,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": reduce_only,
        }
        if cloid is not None:
            order_req["cloid"] = cloid
        order_wire = order_request_to_order_wire(order_req, self.asset_index(coin))
        action = order_wires_to_order_action([order_wire], None)
        nonce = get_timestamp_ms()
        signature = self.sign_action(action, nonce)
        hl_order = {"action": action, "nonce": nonce, "signature": signature}
        response = self.post_action(action, signature, nonce)
        return hl_order, response, nonce

    def close_position(
        self,
        coin: str,
        sz: float,
        is_long: bool,
        *,
        slippage: float = 0.02,
        cloid: Cloid | None = None,
    ) -> tuple[dict, dict, int]:
        # closing a long => sell; closing short => buy
        return self.market_order(
            coin,
            is_buy=not is_long,
            sz=sz,
            reduce_only=True,
            slippage=slippage,
            cloid=cloid,
        )

    def position_size(self, coin: str) -> tuple[float, bool] | None:
        """Return (abs_size, is_long) for coin, or None if flat."""
        detail = self.position_detail(coin)
        if detail is None:
            return None
        return detail["sz"], detail["is_long"]

    def open_coins(self) -> set[str]:
        """Coins with a non-zero HL position. Empty set is a successful flat book."""
        state = self.info.user_state(self.address)
        held: set[str] = set()
        for item in state.get("assetPositions") or []:
            pos = item.get("position") or {}
            coin = pos.get("coin")
            szi = float(pos.get("szi") or 0)
            if coin and szi != 0:
                held.add(str(coin).upper())
        return held

    def position_detail(self, coin: str) -> dict[str, Any] | None:
        """Return sz/is_long/entry_px/leverage for coin, or None if flat."""
        state = self.info.user_state(self.address)
        for item in state.get("assetPositions") or []:
            pos = item.get("position") or {}
            if pos.get("coin") != coin:
                continue
            szi = float(pos.get("szi") or 0)
            if szi == 0:
                return None
            leverage_raw = pos.get("leverage") or {}
            lev = leverage_raw.get("value") if isinstance(leverage_raw, dict) else leverage_raw
            entry_px = pos.get("entryPx")
            return {
                "sz": abs(szi),
                "is_long": szi > 0,
                "entry_px": float(entry_px) if entry_px is not None else None,
                "leverage": float(lev) if lev is not None else None,
            }
        return None

    def cancel_reduce_triggers(
        self, coin: str, orders: list[dict[str, Any]] | None = None
    ) -> list[int]:
        """Cancel open trigger / position TP-SL orders for coin. Returns cancelled oids."""
        orders = (
            orders
            if orders is not None
            else self.info.frontend_open_orders(self.address)
        )
        cancelled: list[int] = []
        for order in orders or []:
            if order.get("coin") != coin:
                continue
            if not (order.get("isTrigger") or order.get("isPositionTpsl")):
                continue
            oid = order.get("oid")
            if oid is None:
                continue
            action = {
                "type": "cancel",
                "cancels": [{"a": self.asset_index(coin), "o": int(oid)}],
            }
            nonce = get_timestamp_ms()
            sig = self.sign_action(action, nonce)
            self.post_action(action, sig, nonce)
            cancelled.append(int(oid))
        return cancelled

    def place_reduce_trigger(
        self,
        coin: str,
        *,
        is_buy: bool,
        sz: float,
        trigger_px: float,
        tpsl: str,
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        """Place a reduce-only TP/SL trigger (market on trigger)."""
        if tpsl not in ("tp", "sl"):
            raise HlTraderError(f"tpsl must be 'tp' or 'sl', got {tpsl!r}")
        sz = self.round_size(coin, sz)
        if sz <= 0:
            raise HlTraderError(f"Size rounded to zero for {coin}")
        trigger_px = self.round_price(coin, trigger_px)
        order_req: dict[str, Any] = {
            "coin": coin,
            "is_buy": is_buy,
            "sz": sz,
            "limit_px": trigger_px,
            "order_type": {
                "trigger": {
                    "triggerPx": trigger_px,
                    "isMarket": True,
                    "tpsl": tpsl,
                }
            },
            "reduce_only": True,
        }
        order_wire = order_request_to_order_wire(order_req, self.asset_index(coin))
        action = order_wires_to_order_action([order_wire], None, grouping="na")
        nonce = get_timestamp_ms()
        signature = self.sign_action(action, nonce)
        hl_order = {"action": action, "nonce": nonce, "signature": signature}
        response = self.post_action(action, signature, nonce)
        return hl_order, response, nonce

    def place_position_tpsl(
        self,
        coin: str,
        *,
        is_long: bool,
        sz: float,
        tp_px: float,
        sl_px: float,
    ) -> tuple[dict[str, Any], dict[str, Any], int] | str:
        """Cancel existing triggers, then place reduce-only TP+SL as positionTpsl."""
        return self.sync_position_tpsl(
            coin, is_long=is_long, sz=sz, tp_px=tp_px, sl_px=sl_px
        )

    def sync_position_tpsl(
        self,
        coin: str,
        *,
        is_long: bool,
        sz: float,
        tp_px: float | None,
        sl_px: float | None,
        tp_cloid: Cloid | None = None,
        sl_cloid: Cloid | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], int] | str | None:
        """Cancel existing reduce triggers, then place whatever TP/SL legs are set.

        Returns (hl_order, response, nonce) when at least one leg is placed,
        ``TPSL_UNCHANGED`` when resting triggers already match, or None after a
        clear-only cancel.
        """
        sz = self.round_size(coin, sz)
        want_tp = (
            self.round_price(coin, float(tp_px))
            if tp_px is not None and float(tp_px) > 0
            else None
        )
        want_sl = (
            self.round_price(coin, float(sl_px))
            if sl_px is not None and float(sl_px) > 0
            else None
        )
        open_orders = self.info.frontend_open_orders(self.address)
        have_tp, have_sl, have_sz = resting_tpsl(open_orders, coin)
        if tpsl_already_resting(
            have_tp=have_tp,
            have_sl=have_sl,
            have_sz=have_sz,
            want_tp=want_tp,
            want_sl=want_sl,
            want_sz=sz,
        ):
            return TPSL_UNCHANGED
        self.cancel_reduce_triggers(coin, open_orders)
        if sz <= 0:
            raise HlTraderError(f"Size rounded to zero for {coin}")

        legs: list[tuple[float, str, Cloid | None]] = []
        if want_tp is not None:
            legs.append((want_tp, "tp", tp_cloid))
        if want_sl is not None:
            legs.append((want_sl, "sl", sl_cloid))
        if not legs:
            return None

        is_buy = not is_long  # flatten direction
        wires = []
        for trigger_px, tpsl, cloid in legs:
            order_req: dict[str, Any] = {
                "coin": coin,
                "is_buy": is_buy,
                "sz": sz,
                "limit_px": trigger_px,
                "order_type": {
                    "trigger": {
                        "triggerPx": trigger_px,
                        "isMarket": True,
                        "tpsl": tpsl,
                    }
                },
                "reduce_only": True,
            }
            if cloid is not None:
                order_req["cloid"] = cloid
            wires.append(order_request_to_order_wire(order_req, self.asset_index(coin)))
        grouping = "positionTpsl" if len(legs) == 2 else "na"
        action = order_wires_to_order_action(wires, None, grouping=grouping)
        nonce = get_timestamp_ms()
        signature = self.sign_action(action, nonce)
        hl_order = {"action": action, "nonce": nonce, "signature": signature}
        response = self.post_action(action, signature, nonce)
        return hl_order, response, nonce
