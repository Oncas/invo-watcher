from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from invo_agent.api.client import InvolioClient
from invo_agent.api.models import ActionType, InvestmentUpdate, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB
from invo_agent.dex.cloid import mimic_close_cloid, mimic_entry_cloid
from invo_agent.dex.hl_trader import HlTrader, HlTraderError
from invo_agent.dex.recovery import decide_missed_close
from invo_agent.dex.session import DexSessionError, DexSessionStore, load_signer
from invo_agent.dex.turnkey_crypto import new_client_tx_id
from invo_agent.events import log_event

console = Console()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryResult:
    status: str  # closed | parked | already_flat | skipped | dry_run
    coin: str
    reason: str
    message: str
    tp: float | None = None
    sl: float | None = None
    our_pnl_pct: float | None = None
    trader_pnl_pct: float | None = None


class DexExecutor:
    """Executes mimic open/update/close against Hyperliquid + Involio DEX APIs."""

    def __init__(
        self,
        settings: Settings,
        client: InvolioClient | None = None,
        dex_store: DexSessionStore | None = None,
        db: AgentDB | None = None,
    ):
        self.settings = settings
        self.client = client
        self.dex_store = dex_store or DexSessionStore(settings.dex_session_path)
        self.db = db
        self._trader: HlTrader | None = None
        self._dex_session = None

    @property
    def live_enabled(self) -> bool:
        return bool(self.settings.live) and not bool(self.settings.dry_run)

    def invalidate_trader(self) -> None:
        """Drop cached HL trader (e.g. after Turnkey API key expiry)."""
        self._trader = None
        self._dex_session = None

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
            inv = action.investment
            trader_pct = self._trader_allocation_pct(inv.entry_size if inv else None)
            pct_note = (
                f"trader {trader_pct:.2f}%"
                if trader_pct is not None
                else f"fallback {self.settings.position_pct}%"
            )
            console.print(
                f"[dim]dry-run would {action.action.value} "
                f"sizing={pct_note} (floor HL ${self.settings.hl_min_order_usd:g} notional)[/dim]"
            )
            logger.info(
                "DRY-RUN %s %s trader_pct=%s fallback_pct=%s",
                action.action.value,
                action.summary,
                trader_pct,
                self.settings.position_pct,
            )
            log_event(
                "dry_run",
                action=action.action.value,
                summary=action.summary,
                notification_id=str(action.notification.id),
                content=action.notification.content,
                position_pct=self.settings.position_pct,
                trader_pct=trader_pct,
                ticker=(action.investment.ticker if action.investment else None),
                leverage=(action.investment.leverage if action.investment else None),
                entry_size=(action.investment.entry_size if action.investment else None),
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

    @staticmethod
    def _trader_allocation_pct(entry_size: float | None) -> float | None:
        """Involio entrySize is usually a fraction of portfolio (0.1 → 10%)."""
        if entry_size is None or entry_size <= 0:
            return None
        if entry_size <= 1.0:
            return float(entry_size) * 100.0
        if entry_size <= 100.0:
            return float(entry_size)
        return None

    def _resolve_margin(
        self, *, equity: float, leverage: int, entry_size: float | None
    ) -> tuple[float, dict[str, Any]]:
        """Copy trader % of our equity; bump up if HL $min notional wouldn't clear."""
        trader_pct = self._trader_allocation_pct(entry_size)
        if trader_pct is not None:
            target_pct = trader_pct
            source = "trader_entry_size"
        else:
            target_pct = float(self.settings.position_pct)
            source = "fallback_position_pct"

        lev = max(1, int(leverage or 1))
        hl_min = float(self.settings.hl_min_order_usd)
        min_margin = hl_min / lev
        min_pct = (min_margin / equity) * 100.0 if equity > 0 else target_pct

        final_pct = max(target_pct, min_pct)
        bumped = final_pct > target_pct + 1e-9
        capped = False
        max_pct = float(self.settings.max_position_pct)
        if final_pct > max_pct:
            final_pct = max_pct
            capped = True

        margin = max(0.0, equity * final_pct / 100.0)
        order_notional = margin * lev
        meta = {
            "source": source,
            "trader_pct": trader_pct,
            "target_pct": target_pct,
            "min_pct_for_hl": min_pct,
            "final_pct": final_pct,
            "bumped_to_hl_min": bumped and not capped,
            "capped": capped,
            "margin_usd": margin,
            "order_notional_usd": order_notional,
            "hl_min_order_usd": hl_min,
            "leverage": lev,
        }
        return margin, meta

    def _open(self, action: TradeAction, trader: HlTrader, equity: float) -> None:
        inv = action.investment
        if inv is None:
            raise HlTraderError("Cannot open without enriched investment")
        coin = self._coin(action)
        is_long = bool(inv.direction_long)
        leverage = int(inv.leverage or 1)
        margin, size_meta = self._resolve_margin(
            equity=equity, leverage=leverage, entry_size=inv.entry_size
        )
        order_notional = float(size_meta["order_notional_usd"])
        if margin < self.settings.min_order_usd:
            raise HlTraderError(
                f"Margin ${margin:.2f} < MIN_ORDER_USD={self.settings.min_order_usd}"
            )
        if order_notional + 1e-9 < float(self.settings.hl_min_order_usd):
            raise HlTraderError(
                f"Order notional ${order_notional:.2f} < HL_MIN_ORDER_USD="
                f"{self.settings.hl_min_order_usd} "
                f"(pct={size_meta['final_pct']:.2f}% lev={leverage}x; "
                f"raise MAX_POSITION_PCT or deposit more)"
            )

        mid = trader.mid_price(coin)
        sz = trader.round_size(coin, order_notional / mid)
        bump_note = ""
        if size_meta["bumped_to_hl_min"]:
            bump_note = (
                f" (bumped from {size_meta['target_pct']:.2f}% → "
                f"{size_meta['final_pct']:.2f}% for HL ${self.settings.hl_min_order_usd:g} min)"
            )
        elif size_meta["source"] == "trader_entry_size":
            bump_note = f" (copied trader {size_meta['final_pct']:.2f}%)"
        console.print(
            f"[cyan]LIVE OPEN[/cyan] {coin} {'LONG' if is_long else 'SHORT'} "
            f"{leverage}x sz={sz} (~${margin:.2f} margin / ${order_notional:.2f} notional)"
            f"{bump_note}"
        )
        logger.info(
            "OPEN %s %s lev=%sx mid=%s sz=%s margin=%.4f notional=%.4f "
            "equity=%.4f pct=%.4f source=%s bumped=%s",
            coin,
            "LONG" if is_long else "SHORT",
            leverage,
            mid,
            sz,
            margin,
            order_notional,
            equity,
            size_meta["final_pct"],
            size_meta["source"],
            size_meta["bumped_to_hl_min"],
        )
        log_event(
            "open_size",
            coin=coin,
            side="long" if is_long else "short",
            leverage=leverage,
            mid=mid,
            sz=sz,
            margin_usd=margin,
            order_notional_usd=order_notional,
            equity=equity,
            tp=inv.price_target,
            sl=inv.stop_loss,
            base_short_id=inv.base_short_id,
            entry_size=inv.entry_size,
            **{k: v for k, v in size_meta.items() if k not in {"margin_usd", "order_notional_usd", "leverage"}},
        )

        trader.update_leverage(coin, leverage, is_cross=True)
        # App stamps source baseShortId + tradeType=mimic into the HL cloid so
        # Involio attributes the fill to the creator (Edit Trade shows @user).
        entry_cloid = mimic_entry_cloid(inv.base_short_id)
        if entry_cloid is None:
            logger.warning(
                "OPEN %s: missing/invalid source baseShortId=%r — order will not "
                "carry a mimic cloid (UI may show as self-created)",
                coin,
                inv.base_short_id,
            )
        hl_order, hl_resp, nonce = trader.market_order(
            coin, is_buy=is_long, sz=sz, cloid=entry_cloid
        )
        self._check_hl_ok(hl_resp, "open")
        logger.info(
            "OPEN HL ok coin=%s cloid=%s resp=%s",
            coin,
            entry_cloid.to_raw() if entry_cloid else None,
            hl_resp,
        )
        log_event(
            "open_hl",
            coin=coin,
            sz=sz,
            nonce=nonce,
            cloid=entry_cloid.to_raw() if entry_cloid else None,
            hl_response=hl_resp,
        )

        client_tx = new_client_tx_id()
        asset_index = trader.asset_index(coin)
        summary = {
            "qtyBefore": "0",
            "qtyAfter": str(sz),
            "intendedLeverage": leverage,
            "tpAfter": str(inv.price_target) if inv.price_target is not None else None,
            "slAfter": str(inv.stop_loss) if inv.stop_loss is not None else None,
        }
        # App Mimic: HL fill → /dex/mimic/copy. Attribution failures are ignored
        # after HL success so we do the same (position still lives on HL).
        self._persist_open_fill(
            inv=inv,
            coin=coin,
            mid=mid,
            sz=sz,
            is_long=is_long,
            leverage=leverage,
        )

        try:
            recorded = self._mimic_copy(
                inv=inv,
                update_type="entry",
                coin=coin,
                asset_index=asset_index,
                client_tx=client_tx,
                submissions=[
                    {
                        "kind": "size_increase",
                        "hlOrder": hl_order,
                        "nonceMs": nonce,
                        "hlResponse": hl_resp,
                    }
                ],
                summary=summary,
                # App builds update via b32(); do not force source short id as
                # *our* position baseShortId on first entry.
                include_update_base_short_id=False,
            )
            console.print(f"[green]Recorded OPEN via mimic/copy[/green] {recorded}")
            logger.info("OPEN recorded mimic/copy: %s", recorded)
            log_event("open_recorded", via="mimic/copy", coin=coin, response=recorded)
            self._mimic_seen(inv.base_short_id)
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]mimic/copy attribution failed after HL open "
                f"(ignored, same as app):[/yellow] {exc}"
            )
            logger.warning("OPEN mimic/copy failed after HL success coin=%s: %s", coin, exc)
            log_event(
                "open_recorded",
                via="hl_only",
                coin=coin,
                error=str(exc),
                cloid=entry_cloid.to_raw() if entry_cloid else None,
            )

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
        nonce = __import__("time").time_ns() // 1_000_000
        lev_action = {
            "type": "updateLeverage",
            "asset": trader.asset_index(coin),
            "isCross": True,
            "leverage": leverage,
        }
        sig = trader.sign_action(lev_action, nonce)
        hl_order = {"action": lev_action, "nonce": nonce, "signature": sig}
        try:
            recorded = self._mimic_copy(
                inv=inv,
                update_type="entry",
                coin=coin,
                asset_index=trader.asset_index(coin),
                client_tx=client_tx,
                submissions=[{"kind": "leverage", "hlOrder": hl_order, "nonceMs": nonce}],
                summary={
                    "qtyBefore": str(sz),
                    "qtyAfter": str(sz),
                    "intendedLeverage": leverage,
                    "tpAfter": str(inv.price_target) if inv.price_target is not None else None,
                    "slAfter": str(inv.stop_loss) if inv.stop_loss is not None else None,
                },
                include_update_base_short_id=False,
            )
            console.print(f"[green]Recorded UPDATE via mimic/copy[/green] {recorded}")
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
        except Exception as exc:  # noqa: BLE001
            # Older self-opened positions may not be mimic-linked; keep HL state.
            console.print(
                f"[yellow]UPDATE mimic/copy failed (ignored — HL position kept):[/yellow] {exc}"
            )
            logger.warning("UPDATE mimic/copy failed coin=%s: %s", coin, exc)
            log_event(
                "update_recorded",
                via="hl_only",
                coin=coin,
                sz=sz,
                error=str(exc),
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
        # Prefer source short id for cloid (matches app mimic close stamping).
        # Older self-opened fills without a mimic cloid still close fine on HL.
        close_cloid = mimic_close_cloid(inv.base_short_id if inv else None)
        hl_order, hl_resp, nonce = trader.close_position(
            coin, sz, is_long, cloid=close_cloid
        )
        self._check_hl_ok(hl_resp, "close")
        log_event(
            "close_hl",
            coin=coin,
            sz=sz,
            side="long" if is_long else "short",
            cloid=close_cloid.to_raw() if close_cloid else None,
            hl_response=hl_resp,
        )

        client_tx = new_client_tx_id()
        asset_index = trader.asset_index(coin)
        summary = {"qtyBefore": str(sz), "qtyAfter": "0"}
        submissions = [
            {
                "kind": "close",
                "hlOrder": hl_order,
                "nonceMs": nonce,
                "hlResponse": hl_resp,
            }
        ]
        # Involio bookkeeping is best-effort after HL success — same as the app
        # ("mimic copy attribution failed (ignored) AFTER HL success"). Critical
        # for older self-opened orphans that were never linked in the UI.
        if inv is not None and inv.id and inv.base_short_id:
            try:
                recorded = self._mimic_copy(
                    inv=inv,
                    update_type="close",
                    coin=coin,
                    asset_index=asset_index,
                    client_tx=client_tx,
                    submissions=submissions,
                    summary=summary,
                    include_update_base_short_id=False,
                )
                console.print(f"[green]Recorded CLOSE via mimic/copy[/green] {recorded}")
                logger.info("CLOSE recorded mimic/copy coin=%s: %s", coin, recorded)
                log_event("close_recorded", via="mimic/copy", coin=coin, sz=sz, response=recorded)
                return
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]mimic/copy close failed, trying position/close:[/yellow] {exc}")
                logger.warning("mimic/copy close failed: %s", exc)

        body: dict[str, Any] = {
            "clientTxId": client_tx,
            "coin": coin,
            "assetIndex": asset_index,
            "submission": {
                "hlOrder": hl_order,
                "nonceMs": nonce,
                "hlResponse": hl_resp,
            },
            "summary": summary,
        }
        # Do not send the *source* trader baseShortId as our position id — that
        # breaks orphan/self-opened closes. Omit and let Involio match by fill.
        try:
            recorded = self.client.dex_post("/dex/position/close", body)
            console.print(f"[green]Recorded CLOSE via position/close[/green] {recorded}")
            logger.info("CLOSE recorded coin=%s: %s", coin, recorded)
            log_event("close_recorded", via="position/close", coin=coin, sz=sz, response=recorded)
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]Involio close record failed (HL already flat — OK for "
                f"older self-opened positions):[/yellow] {exc}"
            )
            logger.warning(
                "CLOSE Involio record failed after HL success coin=%s: %s", coin, exc
            )
            log_event(
                "close_recorded",
                via="hl_only",
                coin=coin,
                sz=sz,
                error=str(exc),
            )

    def _persist_open_fill(
        self,
        *,
        inv: InvestmentUpdate,
        coin: str,
        mid: float,
        sz: float,
        is_long: bool,
        leverage: int,
    ) -> None:
        """Store our HL entry on open_copies for later missed-close recovery."""
        if self.db is None or inv.base_id is None:
            return
        entry_px = mid
        try:
            trader = self._ensure_trader()
            detail = trader.position_detail(coin)
            if detail and detail.get("entry_px"):
                entry_px = float(detail["entry_px"])
                sz = float(detail["sz"] or sz)
                if detail.get("leverage") is not None:
                    leverage = int(detail["leverage"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("OPEN fill lookup failed coin=%s: %s", coin, exc)
        base_id = str(inv.base_id)
        # Row may not exist yet (remember runs after handle) — upsert lightly.
        self.db.upsert_open_copy(
            investment_base_id=base_id,
            base_short_id=inv.base_short_id,
            ticker=inv.ticker or inv.name or coin,
            portfolio_id=str(inv.portfolio.id)
            if inv.portfolio and inv.portfolio.id
            else None,
            notification_id=None,
            our_entry_px=entry_px,
            side="long" if is_long else "short",
            size=sz,
            leverage=float(leverage),
        )
        log_event(
            "open_fill_persisted",
            base_id=base_id,
            coin=coin,
            our_entry_px=entry_px,
            side="long" if is_long else "short",
            size=sz,
            leverage=leverage,
        )

    def recover_missed_close(
        self,
        *,
        coin: str,
        inv: InvestmentUpdate,
        our_entry_px: float | None = None,
        our_side: str | None = None,
    ) -> RecoveryResult:
        """Flatten or park TP/SL after detecting Involio closed while we were offline."""
        coin = coin.upper()
        if not self.settings.recovery_enabled:
            return RecoveryResult(
                status="skipped",
                coin=coin,
                reason="disabled",
                message=f"RECOVERY skipped {coin} (RECOVERY_ENABLED=false)",
            )

        is_long_hint: bool | None = None
        if our_side:
            is_long_hint = our_side.lower() == "long"
        elif inv.direction_long is not None:
            is_long_hint = bool(inv.direction_long)

        if not self.live_enabled:
            # Dry-run: compute decision if DEX session + HL position are available.
            try:
                trader = self._ensure_trader()
            except Exception as exc:  # noqa: BLE001
                msg = f"RECOVERY dry-run {coin}: no DEX session ({exc})"
                console.print(f"[dim]{msg}[/dim]")
                return RecoveryResult(
                    status="dry_run",
                    coin=coin,
                    reason="no_dex",
                    message=msg,
                )
            try:
                mid = trader.mid_price(coin)
                detail = trader.position_detail(coin)
                if detail is None:
                    return RecoveryResult(
                        status="already_flat",
                        coin=coin,
                        reason="flat",
                        message=f"RECOVERY {coin}: already flat on HL (dry-run)",
                    )
                entry = our_entry_px or detail.get("entry_px")
                if entry is None:
                    raise HlTraderError("missing our entry price")
                is_long = detail["is_long"]
                decision_preview = decide_missed_close(
                    our_entry=float(entry),
                    mid=mid,
                    is_long=is_long,
                    trader_entry=inv.entry_price,
                    trader_close=inv.closing_price,
                    recovery_sl_pct=self.settings.recovery_sl_pct,
                )
                msg = (
                    f"RECOVERY dry-run {decision_preview.action} {coin} "
                    f"reason={decision_preview.reason} "
                    f"tp={decision_preview.tp} sl={decision_preview.sl}"
                )
                console.print(f"[dim]{msg}[/dim]")
                log_event(
                    "recovery_dry_run",
                    coin=coin,
                    action=decision_preview.action,
                    reason=decision_preview.reason,
                    tp=decision_preview.tp,
                    sl=decision_preview.sl,
                    our_pnl_pct=decision_preview.our_pnl_pct,
                    trader_pnl_pct=decision_preview.trader_pnl_pct,
                )
                return RecoveryResult(
                    status="dry_run",
                    coin=coin,
                    reason=decision_preview.reason,
                    message=msg,
                    tp=decision_preview.tp,
                    sl=decision_preview.sl,
                    our_pnl_pct=decision_preview.our_pnl_pct,
                    trader_pnl_pct=decision_preview.trader_pnl_pct,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"RECOVERY dry-run {coin} failed to price: {exc}"
                logger.warning(msg)
                return RecoveryResult(
                    status="dry_run",
                    coin=coin,
                    reason="dry_run_error",
                    message=msg,
                )

        if self.client is None:
            raise DexSessionError("InvolioClient required for recovery")

        trader = self._ensure_trader()
        detail = trader.position_detail(coin)
        if detail is None:
            msg = f"RECOVERY {coin}: already flat on HL"
            console.print(f"[yellow]{msg}[/yellow]")
            log_event("recovery_close", coin=coin, status="already_flat")
            return RecoveryResult(
                status="already_flat",
                coin=coin,
                reason="flat",
                message=msg,
            )

        sz = float(detail["sz"])
        is_long = bool(detail["is_long"])
        if is_long_hint is not None and is_long_hint != is_long:
            logger.warning(
                "RECOVERY %s side mismatch db=%s hl=%s — using HL",
                coin,
                "long" if is_long_hint else "short",
                "long" if is_long else "short",
            )
        entry = our_entry_px or detail.get("entry_px")
        if entry is None or float(entry) <= 0:
            raise HlTraderError(f"RECOVERY {coin}: no entry price available")
        entry = float(entry)
        mid = trader.mid_price(coin)

        decision = decide_missed_close(
            our_entry=entry,
            mid=mid,
            is_long=is_long,
            trader_entry=inv.entry_price,
            trader_close=inv.closing_price,
            recovery_sl_pct=self.settings.recovery_sl_pct,
        )
        log_event(
            "missed_close_reconcile",
            coin=coin,
            action=decision.action,
            reason=decision.reason,
            our_entry=entry,
            mid=mid,
            tp=decision.tp,
            sl=decision.sl,
            our_pnl_pct=decision.our_pnl_pct,
            trader_pnl_pct=decision.trader_pnl_pct,
            trader_closing_px=inv.closing_price,
            base_id=str(inv.base_id) if inv.base_id else None,
        )

        if decision.action == "close":
            close_cloid = mimic_close_cloid(inv.base_short_id)
            hl_order, hl_resp, nonce = trader.close_position(
                coin, sz, is_long, cloid=close_cloid
            )
            self._check_hl_ok(hl_resp, "recovery_close")
            self._record_recovery_close(
                inv=inv,
                coin=coin,
                sz=sz,
                hl_order=hl_order,
                hl_resp=hl_resp,
                nonce=nonce,
            )
            msg = (
                f"RECOVERY close {coin} reason={decision.reason} "
                f"our_pnl={_pct(decision.our_pnl_pct)} "
                f"trader_pnl={_pct(decision.trader_pnl_pct)}"
            )
            console.print(f"[red]{msg}[/red]")
            logger.info(msg)
            log_event(
                "recovery_close",
                coin=coin,
                reason=decision.reason,
                sz=sz,
                our_pnl_pct=decision.our_pnl_pct,
                trader_pnl_pct=decision.trader_pnl_pct,
                hl_response=hl_resp,
            )
            return RecoveryResult(
                status="closed",
                coin=coin,
                reason=decision.reason,
                message=msg,
                our_pnl_pct=decision.our_pnl_pct,
                trader_pnl_pct=decision.trader_pnl_pct,
            )

        assert decision.tp is not None and decision.sl is not None
        hl_order, hl_resp, nonce = trader.place_position_tpsl(
            coin,
            is_long=is_long,
            sz=sz,
            tp_px=decision.tp,
            sl_px=decision.sl,
        )
        self._check_hl_ok(hl_resp, "recovery_park")
        msg = (
            f"RECOVERY parked TP/SL on {coin} reason={decision.reason} "
            f"tp={decision.tp} sl={decision.sl} "
            f"our_pnl={_pct(decision.our_pnl_pct)} "
            f"trader_pnl={_pct(decision.trader_pnl_pct)}"
        )
        console.print(f"[yellow]{msg}[/yellow]")
        logger.info(msg)
        log_event(
            "recovery_park",
            coin=coin,
            reason=decision.reason,
            tp=decision.tp,
            sl=decision.sl,
            sz=sz,
            our_pnl_pct=decision.our_pnl_pct,
            trader_pnl_pct=decision.trader_pnl_pct,
            trader_closing_px=inv.closing_price,
            hl_response=hl_resp,
            nonce=nonce,
            hl_order=hl_order,
        )
        return RecoveryResult(
            status="parked",
            coin=coin,
            reason=decision.reason,
            message=msg,
            tp=decision.tp,
            sl=decision.sl,
            our_pnl_pct=decision.our_pnl_pct,
            trader_pnl_pct=decision.trader_pnl_pct,
        )

    def _record_recovery_close(
        self,
        *,
        inv: InvestmentUpdate,
        coin: str,
        sz: float,
        hl_order: dict[str, Any],
        hl_resp: dict[str, Any],
        nonce: int,
    ) -> None:
        if self.client is None:
            return
        client_tx = new_client_tx_id()
        asset_index = self._ensure_trader().asset_index(coin)
        summary = {"qtyBefore": str(sz), "qtyAfter": "0"}
        submissions = [
            {
                "kind": "close",
                "hlOrder": hl_order,
                "nonceMs": nonce,
                "hlResponse": hl_resp,
            }
        ]
        if inv.id and inv.base_short_id:
            try:
                recorded = self._mimic_copy(
                    inv=inv,
                    update_type="close",
                    coin=coin,
                    asset_index=asset_index,
                    client_tx=client_tx,
                    submissions=submissions,
                    summary=summary,
                    include_update_base_short_id=False,
                )
                log_event(
                    "recovery_close_recorded",
                    via="mimic/copy",
                    coin=coin,
                    response=recorded,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("recovery mimic/copy close failed: %s", exc)
        body: dict[str, Any] = {
            "clientTxId": client_tx,
            "coin": coin,
            "assetIndex": asset_index,
            "submission": {
                "hlOrder": hl_order,
                "nonceMs": nonce,
                "hlResponse": hl_resp,
            },
            "summary": summary,
        }
        try:
            recorded = self.client.dex_post("/dex/position/close", body)
            log_event(
                "recovery_close_recorded",
                via="position/close",
                coin=coin,
                response=recorded,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "recovery Involio close record failed after HL success coin=%s: %s",
                coin,
                exc,
            )
            log_event(
                "recovery_close_recorded",
                via="hl_only",
                coin=coin,
                error=str(exc),
            )

    def _mimic_copy(
        self,
        *,
        inv: Any,
        update_type: str,
        coin: str,
        asset_index: int,
        client_tx: str,
        submissions: list[dict[str, Any]],
        summary: dict[str, Any],
        include_update_base_short_id: bool = True,
    ) -> dict[str, Any]:
        """Record a copied trade the same way the Involio web app does."""
        if self.client is None:
            raise DexSessionError("InvolioClient required for mimic/copy")
        if not inv.id or not inv.base_short_id:
            raise HlTraderError("mimic/copy needs investment id + baseShortId")
        update: dict[str, Any] = {
            "clientTxId": client_tx,
            "coin": coin,
            "assetIndex": asset_index,
            "submissions": submissions,
            "summary": {k: v for k, v in summary.items() if v is not None},
        }
        if include_update_base_short_id:
            update["baseShortId"] = inv.base_short_id
        body = {
            "copy": {
                "investmentId": str(inv.id),
                "investmentBaseShortId": inv.base_short_id,
                "updateType": update_type,
            },
            "update": update,
        }
        return self.client.dex_post("/dex/mimic/copy", body)

    def _mimic_seen(self, base_short_id: str | None) -> None:
        """Mark the source trade seen (app does this after Mimic). Best-effort."""
        if self.client is None or not base_short_id:
            return
        try:
            self.client.dex_post("/dex/mimic/seen", {"baseShortId": base_short_id})
            log_event("mimic_seen", base_short_id=base_short_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mimic/seen failed baseShortId=%s: %s", base_short_id, exc)

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


def _pct(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value * 100:.2f}%"
