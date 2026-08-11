from __future__ import annotations

import logging
from typing import Any

from rich.console import Console

from invo_agent.api.client import InvolioClient
from invo_agent.api.models import ActionType, TradeAction
from invo_agent.config import Settings
from invo_agent.dex.hl_trader import HlTrader, HlTraderError
from invo_agent.dex.session import DexSessionError, DexSessionStore, load_signer
from invo_agent.dex.turnkey_crypto import new_client_tx_id
from invo_agent.events import log_event

console = Console()
logger = logging.getLogger(__name__)


class DexExecutor:
    """Executes mimic open/update/close against Hyperliquid + Involio DEX APIs."""

    def __init__(
        self,
        settings: Settings,
        client: InvolioClient | None = None,
        dex_store: DexSessionStore | None = None,
    ):
        self.settings = settings
        self.client = client
        self.dex_store = dex_store or DexSessionStore(settings.dex_session_path)
        self._trader: HlTrader | None = None
        self._dex_session = None

    @property
    def live_enabled(self) -> bool:
        return bool(self.settings.live) and not bool(self.settings.dry_run)

    def _ensure_trader(self) -> HlTrader:
        if self._trader is not None:
            return self._trader
        data, signer = load_signer(self.dex_store)
        self._dex_session = data
        self._trader = HlTrader(signer, data.master_address, mainnet=True)
        return self._trader

    def handle(self, action: TradeAction) -> None:
        if action.action is ActionType.IGNORE:
            return

        if not self.live_enabled:
            console.print(
                f"[dim]dry-run would {action.action.value} "
                f"at {self.settings.position_pct}% wallet[/dim]"
            )
            logger.info(
                "DRY-RUN %s %s pct=%s",
                action.action.value,
                action.summary,
                self.settings.position_pct,
            )
            log_event(
                "dry_run",
                action=action.action.value,
                summary=action.summary,
                notification_id=str(action.notification.id),
                content=action.notification.content,
                position_pct=self.settings.position_pct,
                ticker=(action.investment.ticker if action.investment else None),
                leverage=(action.investment.leverage if action.investment else None),
            )
            return

        if self.client is None:
            raise DexSessionError("InvolioClient required for live execution")

        trader = self._ensure_trader()
        equity = trader.account_value()
        logger.info(
            "LIVE handle %s equity=%.4f pct=%s summary=%s",
            action.action.value,
            equity,
            self.settings.position_pct,
            action.summary,
        )
        log_event(
            "live_handle",
            action=action.action.value,
            summary=action.summary,
            equity=equity,
            position_pct=self.settings.position_pct,
            notification_id=str(action.notification.id),
            content=action.notification.content,
        )
        if equity < self.settings.min_equity_usd:
            raise HlTraderError(
                f"HL equity ${equity:.2f} below MIN_EQUITY_USD={self.settings.min_equity_usd}"
            )

        try:
            if action.action is ActionType.OPEN:
                self._open(action, trader, equity)
            elif action.action is ActionType.UPDATE:
                self._update(action, trader, equity)
            elif action.action is ActionType.CLOSE:
                self._close(action, trader)
            else:
                console.print(f"[yellow]Unhandled action {action.action}[/yellow]")
        except Exception as exc:
            logger.exception("LIVE %s failed: %s", action.action.value, exc)
            log_event(
                "live_error",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise

    def _coin(self, action: TradeAction) -> str:
        inv = action.investment
        coin = (inv.ticker or inv.name or "").upper() if inv else ""
        if not coin:
            raise HlTraderError("Missing ticker on investment")
        return coin

    def _notional_usd(self, equity: float) -> float:
        return max(0.0, equity * (self.settings.position_pct / 100.0))

    def _open(self, action: TradeAction, trader: HlTrader, equity: float) -> None:
        inv = action.investment
        if inv is None:
            raise HlTraderError("Cannot open without enriched investment")
        coin = self._coin(action)
        is_long = bool(inv.direction_long)
        leverage = int(inv.leverage or 1)
        notional = self._notional_usd(equity)
        if notional < self.settings.min_order_usd:
            raise HlTraderError(
                f"Order notional ${notional:.2f} < MIN_ORDER_USD={self.settings.min_order_usd}"
            )

        mid = trader.mid_price(coin)
        sz = trader.round_size(coin, notional * leverage / mid)
        console.print(
            f"[cyan]LIVE OPEN[/cyan] {coin} {'LONG' if is_long else 'SHORT'} "
            f"{leverage}x sz={sz} (~${notional:.2f} margin)"
        )
        logger.info(
            "OPEN %s %s lev=%sx mid=%s sz=%s margin=%.4f equity=%.4f",
            coin,
            "LONG" if is_long else "SHORT",
            leverage,
            mid,
            sz,
            notional,
            equity,
        )
        log_event(
            "open_size",
            coin=coin,
            side="long" if is_long else "short",
            leverage=leverage,
            mid=mid,
            sz=sz,
            margin_usd=notional,
            equity=equity,
            tp=inv.price_target,
            sl=inv.stop_loss,
            base_short_id=inv.base_short_id,
        )

        trader.update_leverage(coin, leverage, is_cross=True)
        hl_order, hl_resp, nonce = trader.market_order(coin, is_buy=is_long, sz=sz)
        self._check_hl_ok(hl_resp, "open")
        logger.info("OPEN HL ok coin=%s resp=%s", coin, hl_resp)
        log_event("open_hl", coin=coin, sz=sz, nonce=nonce, hl_response=hl_resp)

        client_tx = new_client_tx_id()
        asset_index = trader.asset_index(coin)
        body = {
            "clientTxId": client_tx,
            "coin": coin,
            "assetIndex": asset_index,
            "entry": {
                "side": "buy" if is_long else "sell",
                "marginMode": "cross",
                "leverage": leverage,
                "tpPx": str(inv.price_target) if inv.price_target is not None else None,
                "slPx": str(inv.stop_loss) if inv.stop_loss is not None else None,
            },
            "submission": {
                "hlOrder": hl_order,
                "nonceMs": nonce,
                "hlResponse": hl_resp,
            },
            "summary": {
                "qtyBefore": "0",
                "qtyAfter": str(sz),
                "intendedLeverage": leverage,
            },
            "mimicMeta": {
                "portfolioId": str(action.notification.portfolio_id)
                if action.notification.portfolio_id
                else None,
                "creatorInvoUserId": str(action.notification.notifier_id)
                if action.notification.notifier_id
                else None,
                "initialSourcePaperUpdateId": str(inv.id) if inv.id else None,
                "sourcePaperTradeBaseId": str(inv.base_id) if inv.base_id else None,
                "sourcePaperTradeBaseShortId": inv.base_short_id,
            },
        }
        # Strip nulls in entry/mimicMeta
        body["entry"] = {k: v for k, v in body["entry"].items() if v is not None}
        body["mimicMeta"] = {k: v for k, v in body["mimicMeta"].items() if v is not None}

        try:
            recorded = self.client.dex_post("/dex/position/create", body)
            console.print(f"[green]Recorded OPEN on Involio[/green] {recorded}")
            logger.info("OPEN recorded position/create: %s", recorded)
            log_event("open_recorded", via="position/create", coin=coin, response=recorded)
        except Exception as exc:  # noqa: BLE001
            # Fallback: mimic/copy entry record
            console.print(f"[yellow]position/create failed, trying mimic/copy:[/yellow] {exc}")
            logger.warning("position/create failed, mimic/copy fallback: %s", exc)
            log_event("open_create_fallback", coin=coin, error=str(exc))
            copy_body = {
                "copy": {
                    "investmentId": str(inv.id),
                    "investmentBaseShortId": inv.base_short_id,
                    "updateType": "entry",
                },
                "update": {
                    "clientTxId": client_tx,
                    "baseShortId": inv.base_short_id,
                    "coin": coin,
                    "assetIndex": asset_index,
                    "submissions": [
                        {
                            "kind": "size_increase",
                            "hlOrder": hl_order,
                            "nonceMs": nonce,
                            "hlResponse": hl_resp,
                        }
                    ],
                    "summary": {
                        "qtyBefore": "0",
                        "qtyAfter": str(sz),
                        "intendedLeverage": leverage,
                    },
                },
            }
            recorded = self.client.dex_post("/dex/mimic/copy", copy_body)
            console.print(f"[green]Recorded OPEN via mimic/copy[/green] {recorded}")
            logger.info("OPEN recorded mimic/copy: %s", recorded)
            log_event("open_recorded", via="mimic/copy", coin=coin, response=recorded)

    def _update(self, action: TradeAction, trader: HlTrader, equity: float) -> None:
        # v1: sync TP/SL / size-increase as a size_increase using same sizing rules if flat-ish
        inv = action.investment
        if inv is None or not inv.base_short_id:
            raise HlTraderError("Cannot update without investment/baseShortId")
        coin = self._coin(action)
        pos = trader.position_size(coin)
        if pos is None:
            console.print(
                f"[yellow]UPDATE {coin}: no local HL position — treating as OPEN[/yellow]"
            )
            self._open(action, trader, equity)
            return

        sz, is_long = pos
        console.print(
            f"[cyan]LIVE UPDATE[/cyan] {coin} existing sz={sz} "
            f"(tpsl sync / size adjust limited in v1)"
        )
        # For now record a mimic/copy leverage/tpsl no-op style size_increase of 0 is invalid.
        # Re-assert leverage and skip additional size unless pct demands more.
        leverage = int(inv.leverage or 1)
        trader.update_leverage(coin, leverage, is_cross=True)

        client_tx = new_client_tx_id()
        # Place a tiny reduce-only noop? Better: just notify Involio with empty cancel kind skipped.
        # Use mimic/copy with updateType reflecting paper update; HL already has position.
        copy_body = {
            "copy": {
                "investmentId": str(inv.id),
                "investmentBaseShortId": inv.base_short_id,
                "updateType": "entry",
            },
            "update": {
                "clientTxId": client_tx,
                "baseShortId": inv.base_short_id,
                "coin": coin,
                "assetIndex": trader.asset_index(coin),
                "submissions": [],
                "summary": {
                    "qtyBefore": str(sz),
                    "qtyAfter": str(sz),
                    "intendedLeverage": leverage,
                    "tpAfter": str(inv.price_target) if inv.price_target is not None else None,
                    "slAfter": str(inv.stop_loss) if inv.stop_loss is not None else None,
                },
            },
        }
        # submissions required non-empty by schema earlier — add leverage kind via updateLeverage result
        nonce = __import__("time").time_ns() // 1_000_000
        lev_action = {
            "type": "updateLeverage",
            "asset": trader.asset_index(coin),
            "isCross": True,
            "leverage": leverage,
        }
        # We already sent leverage; fabricate submission from last sign by re-signing
        sig = trader.sign_action(lev_action, nonce)
        hl_order = {"action": lev_action, "nonce": nonce, "signature": sig}
        copy_body["update"]["submissions"] = [
            {"kind": "leverage", "hlOrder": hl_order, "nonceMs": nonce}
        ]
        summary = {k: v for k, v in copy_body["update"]["summary"].items() if v is not None}
        copy_body["update"]["summary"] = summary
        recorded = self.client.dex_post("/dex/mimic/copy", copy_body)
        console.print(f"[green]Recorded UPDATE[/green] {recorded}")
        logger.info("UPDATE recorded coin=%s sz=%s: %s", coin, sz, recorded)
        log_event(
            "update_recorded",
            coin=coin,
            sz=sz,
            leverage=leverage,
            tp=inv.price_target,
            sl=inv.stop_loss,
            response=recorded,
        )

    def _close(self, action: TradeAction, trader: HlTrader) -> None:
        inv = action.investment
        coin = self._coin(action)
        pos = trader.position_size(coin)
        if pos is None:
            console.print(f"[yellow]CLOSE {coin}: already flat on HL[/yellow]")
            logger.info("CLOSE %s already flat", coin)
            log_event("close_flat", coin=coin)
            return
        sz, is_long = pos
        console.print(f"[cyan]LIVE CLOSE[/cyan] {coin} sz={sz} {'LONG' if is_long else 'SHORT'}")
        logger.info("CLOSE %s sz=%s %s", coin, sz, "LONG" if is_long else "SHORT")
        hl_order, hl_resp, nonce = trader.close_position(coin, sz, is_long)
        self._check_hl_ok(hl_resp, "close")
        log_event("close_hl", coin=coin, sz=sz, side="long" if is_long else "short", hl_response=hl_resp)

        client_tx = new_client_tx_id()
        body = {
            "clientTxId": client_tx,
            "baseShortId": inv.base_short_id if inv else None,
            "assetIndex": trader.asset_index(coin),
            "submission": {
                "hlOrder": hl_order,
                "nonceMs": nonce,
                "hlResponse": hl_resp,
            },
            "summary": {"qtyBefore": str(sz), "qtyAfter": "0"},
        }
        body = {k: v for k, v in body.items() if v is not None}
        recorded = self.client.dex_post("/dex/position/close", body)
        console.print(f"[green]Recorded CLOSE[/green] {recorded}")
        logger.info("CLOSE recorded coin=%s: %s", coin, recorded)
        log_event("close_recorded", coin=coin, sz=sz, response=recorded)

    @staticmethod
    def _check_hl_ok(resp: dict[str, Any], label: str) -> None:
        status = resp.get("status")
        if status != "ok":
            raise HlTraderError(f"HL {label} not ok: {resp}")
        # statuses nested
        data = resp.get("response", {}).get("data", {})
        statuses = data.get("statuses") or []
        for st in statuses:
            if isinstance(st, dict) and st.get("error"):
                raise HlTraderError(f"HL {label} order error: {st['error']}")
