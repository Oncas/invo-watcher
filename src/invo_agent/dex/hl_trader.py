from __future__ import annotations

import math
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

from invo_agent.dex.turnkey_client import TurnkeySigner


class HlTraderError(RuntimeError):
    pass


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

    def close(self) -> None:
        self._http.close()

    def account_value(self) -> float:
        state = self.info.user_state(self.address)
        summary = state.get("marginSummary") or {}
        return float(summary.get("accountValue") or 0)

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
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        """Place IoC market-style order. Returns (hl_order_payload, hl_response, nonce)."""
        limit_px = self.slippage_price(coin, is_buy, slippage)
        sz = self.round_size(coin, sz)
        if sz <= 0:
            raise HlTraderError(f"Size rounded to zero for {coin}")

        order_wire = order_request_to_order_wire(
            {
                "coin": coin,
                "is_buy": is_buy,
                "sz": sz,
                "limit_px": limit_px,
                "order_type": {"limit": {"tif": "Ioc"}},
                "reduce_only": reduce_only,
            },
            self.asset_index(coin),
        )
        action = order_wires_to_order_action([order_wire], None)
        nonce = get_timestamp_ms()
        signature = self.sign_action(action, nonce)
        hl_order = {"action": action, "nonce": nonce, "signature": signature}
        response = self.post_action(action, signature, nonce)
        return hl_order, response, nonce

    def close_position(self, coin: str, sz: float, is_long: bool, *, slippage: float = 0.02) -> tuple[dict, dict, int]:
        # closing a long => sell; closing short => buy
        return self.market_order(coin, is_buy=not is_long, sz=sz, reduce_only=True, slippage=slippage)

    def position_size(self, coin: str) -> tuple[float, bool] | None:
        """Return (abs_size, is_long) for coin, or None if flat."""
        state = self.info.user_state(self.address)
        for item in state.get("assetPositions") or []:
            pos = item.get("position") or {}
            if pos.get("coin") != coin:
                continue
            szi = float(pos.get("szi") or 0)
            if szi == 0:
                return None
            return abs(szi), szi > 0
        return None
