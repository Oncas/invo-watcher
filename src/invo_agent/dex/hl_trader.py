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

from invo_agent.dex.budget import (
    BudgetDeniedError,
    RequestBudget,
    action_leg_count,
    parse_rate_limit_message,
)
from invo_agent.dex.pricing import HL_MARKET_CLOSE_SLIPPAGE, flatten_limit_px
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


class RecoveryParkedError(HlTraderError):
    """Recovery already parked TP/SL for this copy — do not market-dump it."""

    pass


def extract_resting_oid(hl_resp: dict[str, Any] | None) -> int | None:
    if not hl_resp:
        return None
    data = hl_resp.get("response", {}).get("data", {})
    for st in data.get("statuses") or []:
        if not isinstance(st, dict):
            continue
        resting = st.get("resting") or {}
        raw = resting.get("oid")
        if raw is None:
            raw = (st.get("filled") or {}).get("oid")
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def extract_fill_sz_px(hl_resp: dict[str, Any] | None) -> tuple[float | None, float | None]:
    if not hl_resp:
        return None, None
    data = hl_resp.get("response", {}).get("data", {})
    sz = 0.0
    px: float | None = None
    found = False
    for st in data.get("statuses") or []:
        if not isinstance(st, dict):
            continue
        filled = st.get("filled") or {}
        raw_sz = filled.get("totalSz")
        raw_px = filled.get("avgPx")
        if raw_sz is None:
            continue
        try:
            sz += float(raw_sz)
            found = True
            if raw_px is not None:
                px = float(raw_px)
        except (TypeError, ValueError):
            continue
    return (sz if found else None), px


def _order_is_reduce(order: dict[str, Any]) -> bool:
    return bool(
        order.get("isTrigger")
        or order.get("isPositionTpsl")
        or order.get("reduceOnly")
        or order.get("isReduceOnly")
    )


@dataclass(frozen=True)
class HlFill:
    coin: str
    px: float
    sz: float
    side: str  # "B" buy / "A" sell
    time_ms: int
    oid: int | None = None
    cloid: str | None = None
    closed_pnl: float | None = None


@dataclass(frozen=True)
class HlPosition:
    """One open HL position, as reported by ``user_state``."""

    coin: str
    sz: float  # always positive; ``is_long`` carries the side
    is_long: bool
    entry_px: float | None
    leverage: float | None


@dataclass
class HlBookSnapshot:
    """One info-API read of the whole account. Never an order.

    Everything the executor needs to decide — mids, equity, free margin, open
    positions with their leverage, and resting orders — comes from here so a
    single poll reads HL once instead of five times.
    """

    mids: dict[str, float]
    available: float
    equity: float
    positions: dict[str, HlPosition]
    open_orders: list[dict[str, Any]]
    fills: list[HlFill]
    at: float

    def age(self) -> float:
        return time.monotonic() - self.at

    def position(self, coin: str) -> HlPosition | None:
        return self.positions.get(coin.upper())

    def resting_entry_orders(self, coin: str) -> list[dict[str, Any]]:
        coin_u = coin.upper()
        out: list[dict[str, Any]] = []
        for order in self.open_orders:
            if str(order.get("coin") or "").upper() != coin_u:
                continue
            if _order_is_reduce(order):
                continue
            out.append(order)
        return out


class TurnkeyWalletAdapter:
    """Minimal wallet duck-type for hyperliquid sign_inner / LocalAccount.sign_message."""

    def __init__(self, signer: TurnkeySigner):
        self._signer = signer

    def sign_message(self, signable_message) -> dict[str, Any]:
        # Rebuild typed data is awkward from SignableMessage; we sign via custom path.
        raise NotImplementedError("Use HlTrader.sign_action instead")


class HlTrader:
    def __init__(
        self,
        signer: TurnkeySigner,
        address: str,
        *,
        mainnet: bool = True,
        budget: RequestBudget | None = None,
        leverage_stale_seconds: float = 3 * 3600,
    ):
        self.signer = signer
        self.address = address
        self.mainnet = mainnet
        self.budget = budget
        self.leverage_stale_seconds = float(leverage_stale_seconds)
        self.base_url = constants.MAINNET_API_URL if mainnet else constants.TESTNET_API_URL
        self.info = Info(self.base_url, skip_ws=True)
        self._http = httpx.Client(timeout=30.0)
        self._book: HlBookSnapshot | None = None
        # coin → (leverage, monotonic_ts). Fed from snapshot + our writes.
        self._leverage: dict[str, tuple[int, float]] = {}

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _parse_positions(state: dict[str, Any]) -> dict[str, HlPosition]:
        out: dict[str, HlPosition] = {}
        for item in state.get("assetPositions") or []:
            pos = item.get("position") or {}
            coin = str(pos.get("coin") or "").upper()
            szi = float(pos.get("szi") or 0)
            if not coin or szi == 0:
                continue
            leverage_raw = pos.get("leverage") or {}
            lev = (
                leverage_raw.get("value")
                if isinstance(leverage_raw, dict)
                else leverage_raw
            )
            entry_px = pos.get("entryPx")
            out[coin] = HlPosition(
                coin=coin,
                sz=abs(szi),
                is_long=szi > 0,
                entry_px=float(entry_px) if entry_px is not None else None,
                leverage=float(lev) if lev is not None else None,
            )
        return out

    @staticmethod
    def _parse_fills(raw: list[dict[str, Any]] | None) -> list[HlFill]:
        fills: list[HlFill] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            coin = str(item.get("coin") or "").upper()
            if not coin:
                continue
            try:
                px = float(item.get("px") or 0)
                sz = float(item.get("sz") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0 or sz <= 0:
                continue
            oid_raw = item.get("oid")
            try:
                oid = int(oid_raw) if oid_raw is not None else None
            except (TypeError, ValueError):
                oid = None
            cloid = item.get("cloid") or item.get("c")
            pnl = item.get("closedPnl")
            try:
                closed_pnl = float(pnl) if pnl is not None else None
            except (TypeError, ValueError):
                closed_pnl = None
            fills.append(
                HlFill(
                    coin=coin,
                    px=px,
                    sz=sz,
                    side=str(item.get("side") or ""),
                    time_ms=int(item.get("time") or 0),
                    oid=oid,
                    cloid=str(cloid).lower().removeprefix("0x") if cloid else None,
                    closed_pnl=closed_pnl,
                )
            )
        return fills

    def refresh_book(self, *, fills_since_ms: int | None = None) -> HlBookSnapshot:
        """One info pass: mids + user_state + open orders (+ optional fills)."""
        raw_mids = self.info.all_mids() or {}
        mids = {str(k).upper(): float(v) for k, v in raw_mids.items() if v is not None}
        state = self.info.user_state(self.address)
        summary = state.get("crossMarginSummary") or state.get("marginSummary") or {}
        equity = float(summary.get("accountValue") or 0)
        used = float(summary.get("totalMarginUsed") or 0)
        positions = self._parse_positions(state)
        try:
            open_orders = list(self.info.frontend_open_orders(self.address) or [])
        except Exception:
            open_orders = []
        fills: list[HlFill] = []
        if fills_since_ms is not None:
            try:
                raw_fills = self.info.user_fills_by_time(
                    self.address, int(fills_since_ms), int(time.time() * 1000)
                )
                fills = self._parse_fills(raw_fills)
            except Exception:
                fills = []
        now = time.monotonic()
        for coin, pos in positions.items():
            if pos.leverage is not None:
                self._leverage[coin] = (int(pos.leverage), now)
        self._book = HlBookSnapshot(
            mids=mids,
            available=max(0.0, equity - used),
            equity=equity,
            positions=positions,
            open_orders=open_orders,
            fills=fills,
            at=now,
        )
        return self._book

    def fetch_user_fees(self) -> dict[str, Any]:
        response = self._http.post(
            f"{self.base_url}/info",
            json={"type": "userFees", "user": self.address},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise HlTraderError(f"userFees: unexpected payload {type(data)}")
        return data

    def _ensure_book(self, *, max_age: float = 8.0) -> HlBookSnapshot:
        if self._book is None or self._book.age() > max_age:
            return self.refresh_book()
        return self._book

    def _stale_book(self) -> None:
        if self._book is not None:
            self._book.at = 0.0

    def cached_mid(self, coin: str) -> float | None:
        if self._book is None:
            return None
        px = self._book.mids.get(coin.upper())
        return float(px) if px is not None and float(px) > 0 else None

    def cached_available(self) -> float | None:
        if self._book is None:
            return None
        return float(self._book.available)

    def cached_equity(self) -> float | None:
        if self._book is None:
            return None
        return float(self._book.equity)

    def account_value(self) -> float:
        return float(self._ensure_book().equity)

    def available_margin(self) -> float:
        """Free cross margin ≈ accountValue − totalMarginUsed.

        Do not clamp to ``withdrawable`` — HL often reports withdrawable=0 while
        open positions exist even though new cross orders can still use free margin.
        """
        return float(self._ensure_book().available)

    def asset_index(self, coin: str) -> int:
        return int(self.info.name_to_asset(coin))

    def mid_price(self, coin: str) -> float:
        book = self._ensure_book()
        px = book.mids.get(coin.upper())
        if px is None or float(px) <= 0:
            raise HlTraderError(f"No mid price for {coin}")
        return float(px)

    def sz_decimals(self, coin: str) -> int:
        # Newer hyperliquid-python-sdk: asset index → sz decimals
        return int(self.info.asset_to_sz_decimals[self.asset_index(coin)])

    def round_size(self, coin: str, sz: float) -> float:
        decimals = self.sz_decimals(coin)
        factor = 10**decimals
        return math.floor(sz * factor) / factor

    def size_for_notional(
        self,
        coin: str,
        notional: float,
        *,
        mid: float | None = None,
        min_notional: float = 0.0,
    ) -> float:
        from invo_agent.dex.pricing import size_clearing_min_notional

        px = float(mid) if mid is not None and mid > 0 else self.mid_price(coin)
        return size_clearing_min_notional(
            mid=px,
            notional=notional,
            min_notional=min_notional,
            sz_decimals=self.sz_decimals(coin),
        )

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

    def post_action(
        self,
        action: dict[str, Any],
        signature: dict[str, Any],
        nonce: int,
        *,
        legs: int | None = None,
        essential: bool = False,
    ) -> dict:
        n = action_leg_count(action) if legs is None else max(1, int(legs))
        if self.budget is not None:
            self.budget.wait_if_needed()
            if not self.budget.can_spend(n, essential=essential):
                raise BudgetDeniedError(
                    f"HL budget reserve: remaining={self.budget.remaining():.0f} "
                    f"need={n} reserve={self.budget.reserve:g}"
                )
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
        blob = str(data)
        hit = parse_rate_limit_message(blob)
        if hit is not None and self.budget is not None:
            self.budget.reanchor(hit)
        if response.status_code >= 400:
            if self.budget is not None:
                self.budget.note_posted(n, accepted=False)
            raise HlTraderError(f"HL exchange error ({response.status_code}): {data}")
        accepted = isinstance(data, dict) and data.get("status") == "ok"
        if self.budget is not None:
            self.budget.note_posted(n, accepted=accepted)
        if accepted:
            self._stale_book()
        return data

    def cached_leverage(self, coin: str) -> int | None:
        row = self._leverage.get(coin.upper())
        if row is None:
            return None
        lev, at = row
        if time.monotonic() - at > self.leverage_stale_seconds:
            return None
        return int(lev)

    def ensure_leverage(
        self, coin: str, leverage: int, *, is_cross: bool = True
    ) -> dict | None:
        """Write leverage only when the snapshot / cache says it differs."""
        want = int(leverage)
        have = self.cached_leverage(coin)
        if have is None and self._book is not None:
            pos = self._book.position(coin)
            if pos is not None and pos.leverage is not None:
                have = int(pos.leverage)
        if have == want:
            return None
        return self.update_leverage(coin, want, is_cross=is_cross)

    def update_leverage(self, coin: str, leverage: int, *, is_cross: bool = True) -> dict:
        nonce = get_timestamp_ms()
        action = {
            "type": "updateLeverage",
            "asset": self.asset_index(coin),
            "isCross": is_cross,
            "leverage": int(leverage),
        }
        sig = self.sign_action(action, nonce)
        resp = self.post_action(action, sig, nonce, legs=1, essential=False)
        self._leverage[coin.upper()] = (int(leverage), time.monotonic())
        return resp

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
        essential: bool | None = None,
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
        is_essential = bool(reduce_only) if essential is None else essential
        response = self.post_action(
            action, signature, nonce, legs=1, essential=is_essential
        )
        return hl_order, response, nonce

    def limit_order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        *,
        tif: str = "Gtc",
        reduce_only: bool = False,
        cloid: Cloid | None = None,
        essential: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        """Resting (or IoC) limit. Used for queued opens so we stop probing."""
        limit_px = self.round_price(coin, float(limit_px))
        sz = self.round_size(coin, sz)
        if sz <= 0:
            raise HlTraderError(f"Size rounded to zero for {coin}")
        order_req: dict[str, Any] = {
            "coin": coin,
            "is_buy": is_buy,
            "sz": sz,
            "limit_px": limit_px,
            "order_type": {"limit": {"tif": tif}},
            "reduce_only": reduce_only,
        }
        if cloid is not None:
            order_req["cloid"] = cloid
        order_wire = order_request_to_order_wire(order_req, self.asset_index(coin))
        action = order_wires_to_order_action([order_wire], None)
        nonce = get_timestamp_ms()
        signature = self.sign_action(action, nonce)
        hl_order = {"action": action, "nonce": nonce, "signature": signature}
        response = self.post_action(
            action, signature, nonce, legs=1, essential=essential
        )
        return hl_order, response, nonce

    def close_position(
        self,
        coin: str,
        sz: float,
        is_long: bool,
        *,
        slippage: float = HL_MARKET_CLOSE_SLIPPAGE,
        min_notional: float = 10.0,
        cloid: Cloid | None = None,
    ) -> tuple[dict, dict, int]:
        # closing a long => sell; closing short => buy
        is_buy = not is_long
        sz = self.round_size(coin, sz)
        mid = self.mid_price(coin)
        limit_px = flatten_limit_px(
            mid=mid,
            sz=sz,
            is_buy=is_buy,
            slippage=slippage,
            min_notional=min_notional,
            round_price=lambda px: self.round_price(coin, px),
        )
        return self.market_order(
            coin,
            is_buy=is_buy,
            sz=sz,
            reduce_only=True,
            limit_px=limit_px,
            cloid=cloid,
            essential=True,
        )

    def position_size(self, coin: str) -> tuple[float, bool] | None:
        """Return (abs_size, is_long) for coin, or None if flat."""
        detail = self.position_detail(coin)
        if detail is None:
            return None
        return detail["sz"], detail["is_long"]

    def open_coins(self) -> set[str]:
        """Coins with a non-zero HL position. Empty set is a successful flat book."""
        return set(self._ensure_book().positions)

    def position_detail(self, coin: str) -> dict[str, Any] | None:
        """Return sz/is_long/entry_px/leverage for coin, or None if flat."""
        pos = self._ensure_book().position(coin)
        if pos is None:
            return None
        return {
            "sz": pos.sz,
            "is_long": pos.is_long,
            "entry_px": pos.entry_px,
            "leverage": pos.leverage,
        }

    def cancel_oids(
        self, coin: str, oids: list[int], *, essential: bool = True
    ) -> list[int]:
        """One cancel action for every oid. Address cost is still N."""
        clean = [int(o) for o in oids if o is not None]
        if not clean:
            return []
        asset = self.asset_index(coin)
        action = {
            "type": "cancel",
            "cancels": [{"a": asset, "o": oid} for oid in clean],
        }
        nonce = get_timestamp_ms()
        sig = self.sign_action(action, nonce)
        self.post_action(action, sig, nonce, legs=len(clean), essential=essential)
        return clean

    def cancel_reduce_triggers(
        self,
        coin: str,
        orders: list[dict[str, Any]] | None = None,
        *,
        kinds: set[str] | None = None,
    ) -> list[int]:
        """Cancel open trigger / position TP-SL orders for coin. Returns cancelled oids."""
        orders = (
            orders
            if orders is not None
            else (
                self._book.open_orders
                if self._book is not None
                else self.info.frontend_open_orders(self.address)
            )
        )
        oids: list[int] = []
        coin_u = coin.upper()
        for order in orders or []:
            if str(order.get("coin") or "").upper() != coin_u:
                continue
            if not (order.get("isTrigger") or order.get("isPositionTpsl")):
                continue
            if kinds is not None:
                kind = _order_tpsl_kind(order)
                if kind not in kinds:
                    continue
            oid = order.get("oid")
            if oid is None:
                continue
            oids.append(int(oid))
        return self.cancel_oids(coin, oids, essential=True)

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
        response = self.post_action(action, signature, nonce, legs=1, essential=True)
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
        open_orders = (
            self._book.open_orders
            if self._book is not None
            else self.info.frontend_open_orders(self.address)
        )
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

        def _eq(left: float | None, right: float | None) -> bool:
            return tpsl_already_resting(
                have_tp=left,
                have_sl=None,
                have_sz=None,
                want_tp=right,
                want_sl=None,
                want_sz=None,
            )

        size_changed = (
            have_sz is not None
            and sz > 0
            and abs(have_sz - sz) > max(1e-8, 1e-6 * max(have_sz, sz))
        )
        replace_tp = want_tp is not None and (size_changed or not _eq(have_tp, want_tp))
        replace_sl = want_sl is not None and (size_changed or not _eq(have_sl, want_sl))
        clear_tp = want_tp is None and have_tp is not None
        clear_sl = want_sl is None and have_sl is not None
        kinds: set[str] = set()
        if replace_tp or clear_tp:
            kinds.add("tp")
        if replace_sl or clear_sl:
            kinds.add("sl")
        if kinds:
            self.cancel_reduce_triggers(coin, open_orders, kinds=kinds)
        if sz <= 0:
            raise HlTraderError(f"Size rounded to zero for {coin}")

        legs: list[tuple[float, str, Cloid | None]] = []
        if replace_tp and want_tp is not None:
            legs.append((want_tp, "tp", tp_cloid))
        if replace_sl and want_sl is not None:
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
        response = self.post_action(
            action, signature, nonce, legs=len(legs), essential=True
        )
        return hl_order, response, nonce
