from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from invo_agent.api.client import InvolioClient
from invo_agent.api.models import ActionType, InvestmentUpdate, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB, OpenCopy
from invo_agent.dex.cloid import (
    mimic_close_cloid,
    mimic_entry_cloid,
    mimic_size_increase_cloid,
    mimic_sl_cloid,
    mimic_tp_cloid,
)
from invo_agent.dex.hl_trader import (
    HlTrader,
    HlTraderError,
    InsufficientMarginError,
    IocUnfilledError,
    NotOpenCopyError,
    PriceNotBetterError,
    SameCoinOpenError,
)
from invo_agent.dex.pricing import (
    extract_avg_px,
    fill_gaps,
    ioc_unfilled_message,
    price_is_at_or_better,
)
from invo_agent.dex.recovery import RecoveryDecision, decide_missed_close
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
        except InsufficientMarginError as exc:
            logger.warning("LIVE %s skipped (margin): %s", action.action.value, exc)
            log_event(
                "live_skip_margin",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except SameCoinOpenError as exc:
            logger.warning("LIVE %s skipped (same coin): %s", action.action.value, exc)
            log_event(
                "live_skip_same_coin",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except NotOpenCopyError as exc:
            logger.info("LIVE %s skipped (not open): %s", action.action.value, exc)
            log_event(
                "live_skip_not_open",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except PriceNotBetterError as exc:
            logger.info("LIVE %s queued (price): %s", action.action.value, exc)
            log_event(
                "live_queue_price",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except IocUnfilledError as exc:
            logger.info("LIVE %s queued (ioc unfilled): %s", action.action.value, exc)
            log_event(
                "live_queue_ioc",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
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
        # One tracked copy per coin — HL nets a single position; stacking duplicates
        # size and muddies close attribution across traders.
        self._refuse_same_coin_open(
            coin,
            exclude_base_id=str(inv.base_id) if inv.base_id is not None else None,
        )
        leverage = int(inv.leverage or 1)
        margin, size_meta = self._resolve_margin(
            equity=equity, leverage=leverage, entry_size=inv.entry_size
        )
        available = trader.available_margin()
        size_meta["available_margin_usd"] = available
        if available + 1e-9 < self.settings.min_order_usd:
            raise InsufficientMarginError(
                f"No free margin for {coin} (available=${available:.2f}, "
                f"need≥${self.settings.min_order_usd:g}; "
                f"account fully allocated — close something or deposit)"
            )
        if margin > available:
            # Fit into free margin; may drop below trader % / HL min.
            old_margin = margin
            margin = available * 0.98
            lev = max(1, leverage)
            order_notional = margin * lev
            size_meta["final_pct"] = (margin / equity) * 100.0 if equity > 0 else 0.0
            size_meta["margin_usd"] = margin
            size_meta["order_notional_usd"] = order_notional
            size_meta["shrunk_to_available"] = True
            size_meta["target_margin_usd"] = old_margin
            logger.info(
                "OPEN %s shrunk margin %.4f → %.4f (available=%.4f)",
                coin,
                old_margin,
                margin,
                available,
            )
        order_notional = float(size_meta["order_notional_usd"])
        if margin < self.settings.min_order_usd:
            raise InsufficientMarginError(
                f"Margin ${margin:.2f} < MIN_ORDER_USD={self.settings.min_order_usd}"
            )
        if order_notional + 1e-9 < float(self.settings.hl_min_order_usd):
            raise InsufficientMarginError(
                f"Order notional ${order_notional:.2f} < HL_MIN_ORDER_USD="
                f"{self.settings.hl_min_order_usd} "
                f"(available=${available:.2f}; free margin too small for a new open)"
            )

        mid = trader.mid_price(coin)
        trader_entry = float(inv.entry_price) if inv.entry_price else None
        if trader_entry is not None and trader_entry <= 0:
            trader_entry = None
        if not price_is_at_or_better(mid=mid, trader_entry=trader_entry, is_long=is_long):
            raise PriceNotBetterError(
                f"{coin} mid={mid} worse than trader entry={trader_entry} "
                f"({'long' if is_long else 'short'}) — queue"
            )
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
            "OPEN %s %s lev=%sx mid=%s trader_entry=%s sz=%s margin=%.4f notional=%.4f "
            "equity=%.4f pct=%.4f source=%s bumped=%s",
            coin,
            "LONG" if is_long else "SHORT",
            leverage,
            mid,
            trader_entry,
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
            trader_entry=trader_entry,
            **{
                k: v
                for k, v in size_meta.items()
                if k not in {"margin_usd", "order_notional_usd", "leverage"}
            },
        )

        try:
            trader.update_leverage(coin, leverage, is_cross=True)
        except HlTraderError as exc:
            if "insufficient margin" in str(exc).lower():
                raise InsufficientMarginError(str(exc)) from exc
            raise
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
            coin,
            is_buy=is_long,
            sz=sz,
            limit_px=trader_entry,
            cloid=entry_cloid,
        )
        try:
            self._check_hl_ok(hl_resp, "open")
        except IocUnfilledError:
            raise
        except HlTraderError as exc:
            if "insufficient margin" in str(exc).lower():
                raise InsufficientMarginError(str(exc)) from exc
            raise
        avg_px = extract_avg_px(hl_resp)
        our_px = avg_px if avg_px is not None else mid
        price_gap_pct, roe_gap_pct = fill_gaps(
            our_px=our_px,
            trader_entry=trader_entry,
            is_long=is_long,
            leverage=leverage,
        )
        logger.info(
            "OPEN HL ok coin=%s cloid=%s trader_entry=%s mid=%s avgPx=%s "
            "price_gap_pct=%s roe_gap_pct=%s resp=%s",
            coin,
            entry_cloid.to_raw() if entry_cloid else None,
            trader_entry,
            mid,
            avg_px,
            price_gap_pct,
            roe_gap_pct,
            hl_resp,
        )
        log_event(
            "open_hl",
            coin=coin,
            sz=sz,
            nonce=nonce,
            cloid=entry_cloid.to_raw() if entry_cloid else None,
            trader_entry=trader_entry,
            mid=mid,
            avg_px=avg_px,
            price_gap_pct=price_gap_pct,
            roe_gap_pct=roe_gap_pct,
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
        # Prefer filled size for TP/SL so triggers match the live position.
        tpsl_sz = sz
        try:
            detail = trader.position_detail(coin)
            if detail and detail.get("sz"):
                tpsl_sz = float(detail["sz"])
        except Exception:  # noqa: BLE001
            pass
        tpsl_placed = self._sync_tpsl(
            trader,
            coin=coin,
            is_long=is_long,
            sz=tpsl_sz,
            tp=inv.price_target,
            sl=inv.stop_loss,
            context="open",
            base_short_id=inv.base_short_id,
            copy_update=False,
        )

        try:
            submissions: list[dict[str, Any]] = [
                {
                    "kind": "size_increase",
                    "hlOrder": hl_order,
                    "nonceMs": nonce,
                    "hlResponse": hl_resp,
                }
            ]
            if tpsl_placed is not None:
                tpsl_order, tpsl_resp, tpsl_nonce = tpsl_placed
                # Involio only accepts submission kind "tpsl" (not "tp"/"sl").
                submissions.append(
                    {
                        "kind": "tpsl",
                        "hlOrder": tpsl_order,
                        "nonceMs": tpsl_nonce,
                        "hlResponse": tpsl_resp,
                    }
                )
            recorded = self._mimic_copy(
                inv=inv,
                update_type="entry",
                coin=coin,
                asset_index=asset_index,
                client_tx=client_tx,
                submissions=submissions,
                summary=summary,
                # Required by /dex/mimic/copy validation; source short id is what
                # the app resolves after a Mimic fill (same as Copy on updates).
                include_update_base_short_id=True,
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
        # Only sync updates for investments we opened. Another trader updating the
        # same coin must not change leverage / TP/SL on our HL position.
        inv = action.investment
        if inv is None or not inv.base_short_id:
            raise HlTraderError("Cannot update without investment/baseShortId")
        coin = self._coin(action)
        copy = self._get_tracked_copy(inv)
        if copy is None:
            console.print(
                f"[yellow]UPDATE {coin}: not open "
                f"(shortId={inv.base_short_id}) — skip[/yellow]"
            )
            logger.info(
                "UPDATE %s skipped — not open base_id=%s shortId=%s",
                coin,
                str(inv.base_id) if inv.base_id else None,
                inv.base_short_id,
            )
            log_event(
                "update_skip_not_open",
                coin=coin,
                base_id=str(inv.base_id) if inv.base_id else None,
                base_short_id=inv.base_short_id,
                summary=action.summary,
            )
            raise NotOpenCopyError(
                f"UPDATE {coin}: not open (shortId={inv.base_short_id}) — skip"
            )

        pos = trader.position_size(coin)
        if pos is None:
            available = trader.available_margin()
            if available + 1e-9 < self.settings.min_order_usd:
                raise InsufficientMarginError(
                    f"UPDATE {coin}: flat on HL and no free margin "
                    f"(available=${available:.2f})"
                )
            console.print(
                f"[yellow]UPDATE {coin}: tracked but flat on HL — treating as OPEN[/yellow]"
            )
            self._open(action, trader, equity)
            return

        sz, is_long = pos
        if copy.side:
            want_long = copy.side.lower() == "long"
            if want_long != is_long:
                console.print(
                    f"[yellow]UPDATE {coin}: side mismatch copy={copy.side} "
                    f"hl={'long' if is_long else 'short'} — skip HL[/yellow]"
                )
                logger.warning(
                    "UPDATE %s side mismatch copy=%s hl=%s base_id=%s — skip HL",
                    coin,
                    copy.side,
                    "long" if is_long else "short",
                    copy.investment_base_id,
                )
                log_event(
                    "update_skip_side_mismatch",
                    coin=coin,
                    copy_side=copy.side,
                    hl_side="long" if is_long else "short",
                    base_id=copy.investment_base_id,
                    base_short_id=copy.base_short_id,
                )
                return

        console.print(
            f"[cyan]LIVE UPDATE[/cyan] {coin} existing sz={sz} "
            f"(copy={copy.base_short_id or copy.investment_base_id}; "
            f"tp={inv.price_target} sl={inv.stop_loss} "
            f"entrySize={inv.entry_size})"
        )
        leverage = int(inv.leverage or 1)
        trader.update_leverage(coin, leverage, is_cross=True)

        qty_before = sz
        size_sub: dict[str, Any] | None = None
        if self._should_increase_size(inv):
            sz, size_sub = self._apply_size_increase(
                trader,
                inv=inv,
                coin=coin,
                is_long=is_long,
                current_sz=sz,
                equity=equity,
            )

        # Re-apply TP/SL at (possibly new) size — same as Copy on TP/SL updates.
        tpsl_placed = self._sync_tpsl(
            trader,
            coin=coin,
            is_long=is_long,
            sz=sz,
            tp=inv.price_target,
            sl=inv.stop_loss,
            context="update",
            base_short_id=inv.base_short_id,
            copy_update=True,
        )

        client_tx = new_client_tx_id()
        submissions: list[dict[str, Any]] = []
        has_tp = inv.price_target is not None and float(inv.price_target) > 0
        # One Copy card → one primary updateType. Prefer size_increase when we added.
        if size_sub is not None:
            update_type = "increase"
            submissions.append(size_sub)
        elif tpsl_placed is not None:
            tpsl_order, tpsl_resp, tpsl_nonce = tpsl_placed
            # copy.updateType: "tp" / "sl" (not "tpsl" — that 500s).
            # submissions[].kind: must be "tpsl" per Involio validation.
            update_type = "tp" if has_tp else "sl"
            submissions.append(
                {
                    "kind": "tpsl",
                    "hlOrder": tpsl_order,
                    "nonceMs": tpsl_nonce,
                    "hlResponse": tpsl_resp,
                }
            )
        else:
            update_type = "entry"
            nonce = __import__("time").time_ns() // 1_000_000
            lev_action = {
                "type": "updateLeverage",
                "asset": trader.asset_index(coin),
                "isCross": True,
                "leverage": leverage,
            }
            sig = trader.sign_action(lev_action, nonce)
            hl_order = {"action": lev_action, "nonce": nonce, "signature": sig}
            submissions.append(
                {"kind": "leverage", "hlOrder": hl_order, "nonceMs": nonce}
            )

        try:
            recorded = self._mimic_copy(
                inv=inv,
                update_type=update_type,
                coin=coin,
                asset_index=trader.asset_index(coin),
                client_tx=client_tx,
                submissions=submissions,
                summary={
                    "qtyBefore": str(qty_before),
                    "qtyAfter": str(sz),
                    "intendedLeverage": leverage,
                    "tpAfter": str(inv.price_target) if inv.price_target is not None else None,
                    "slAfter": str(inv.stop_loss) if inv.stop_loss is not None else None,
                },
                include_update_base_short_id=True,
            )
            console.print(
                f"[green]Recorded UPDATE via mimic/copy[/green] "
                f"updateType={update_type} {recorded}"
            )
            logger.info(
                "UPDATE recorded coin=%s sz=%s→%s updateType=%s: %s",
                coin,
                qty_before,
                sz,
                update_type,
                recorded,
            )
            log_event(
                "update_recorded",
                coin=coin,
                sz=sz,
                qty_before=qty_before,
                leverage=leverage,
                update_type=update_type,
                tp=inv.price_target,
                sl=inv.stop_loss,
                base_id=copy.investment_base_id,
                base_short_id=copy.base_short_id,
                response=recorded,
            )
            self._mimic_seen(inv.base_short_id)
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
                update_type=update_type,
                error=str(exc),
            )

    def _close(self, action: TradeAction, trader: HlTrader) -> None:
        inv = action.investment
        coin = self._coin(action)
        # Only reduce if we still track this investment. Otherwise a CLOSE for a
        # trade we never opened (or already closed) must not touch HL — including
        # when another trader's copy of the same coin is still open.
        copy = self._get_tracked_copy(inv)
        if copy is None:
            console.print(
                f"[yellow]CLOSE {coin}: not open "
                f"(shortId={inv.base_short_id if inv else None}) — skip[/yellow]"
            )
            logger.info(
                "CLOSE %s skipped — not open base_id=%s shortId=%s",
                coin,
                str(inv.base_id) if inv and inv.base_id else None,
                inv.base_short_id if inv else None,
            )
            log_event(
                "close_skip_not_open",
                coin=coin,
                base_id=str(inv.base_id) if inv and inv.base_id else None,
                base_short_id=inv.base_short_id if inv else None,
                summary=action.summary,
            )
            raise NotOpenCopyError(
                f"CLOSE {coin}: not open "
                f"(shortId={inv.base_short_id if inv else None}) — skip"
            )
        pos = trader.position_size(coin)
        if pos is None:
            console.print(f"[yellow]CLOSE {coin}: already flat on HL[/yellow]")
            logger.info("CLOSE %s already flat", coin)
            log_event("close_flat", coin=coin)
            return
        hl_sz, is_long = pos
        if copy.side:
            want_long = copy.side.lower() == "long"
            if want_long != is_long:
                # Net HL side no longer matches this copy (e.g. later opposite
                # fills). Do not trade the wrong side; drop local tracking via
                # remember() after handle returns.
                console.print(
                    f"[yellow]CLOSE {coin}: side mismatch copy={copy.side} "
                    f"hl={'long' if is_long else 'short'} — skip HL[/yellow]"
                )
                logger.warning(
                    "CLOSE %s side mismatch copy=%s hl=%s base_id=%s — skip HL",
                    coin,
                    copy.side,
                    "long" if is_long else "short",
                    copy.investment_base_id,
                )
                log_event(
                    "close_skip_side_mismatch",
                    coin=coin,
                    copy_side=copy.side,
                    hl_side="long" if is_long else "short",
                    base_id=copy.investment_base_id,
                    base_short_id=copy.base_short_id,
                    hl_sz=hl_sz,
                )
                return
        sz = self._close_size_for_copy(copy, coin=coin, hl_sz=hl_sz)
        qty_after = max(hl_sz - sz, 0.0)
        console.print(
            f"[cyan]LIVE CLOSE[/cyan] {coin} sz={sz}/{hl_sz} "
            f"{'LONG' if is_long else 'SHORT'} "
            f"(copy={copy.base_short_id or copy.investment_base_id})"
        )
        logger.info(
            "CLOSE %s sz=%s hl=%s %s copy=%s stored=%s",
            coin,
            sz,
            hl_sz,
            "LONG" if is_long else "SHORT",
            copy.base_short_id or copy.investment_base_id,
            copy.size,
        )
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
            hl_sz_before=hl_sz,
            side="long" if is_long else "short",
            cloid=close_cloid.to_raw() if close_cloid else None,
            base_id=copy.investment_base_id,
            base_short_id=copy.base_short_id,
            hl_response=hl_resp,
        )

        client_tx = new_client_tx_id()
        asset_index = trader.asset_index(coin)
        summary = {"qtyBefore": str(hl_sz), "qtyAfter": str(qty_after)}
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
                console.print(
                    f"[yellow]mimic/copy close failed, trying position/close:[/yellow] {exc}"
                )
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

    def _get_tracked_copy(self, inv: InvestmentUpdate | None) -> OpenCopy | None:
        """Return open_copies row for this source investment, if any."""
        if self.db is None or inv is None:
            return None
        if inv.base_id is not None:
            row = self.db.get_open_copy(str(inv.base_id))
            if row is not None:
                return row
        if inv.base_short_id:
            return self.db.get_open_copy_by_short_id(inv.base_short_id)
        return None

    def _have_open_copy(self, inv: InvestmentUpdate | None) -> bool:
        """True if open_copies still has this source investment as open."""
        return self._get_tracked_copy(inv) is not None

    def _other_open_copies_for_coin(
        self, coin: str, exclude_base_id: str | None
    ) -> list[OpenCopy]:
        if self.db is None:
            return []
        coin_u = coin.upper()
        out: list[OpenCopy] = []
        for row in self.db.list_open_copies():
            if exclude_base_id and row.investment_base_id == exclude_base_id:
                continue
            tick = (row.ticker or "").upper()
            if tick == coin_u:
                out.append(row)
        return out

    def _close_size_for_copy(
        self, copy: OpenCopy, *, coin: str, hl_sz: float
    ) -> float:
        """Size to reduce on CLOSE — never more than this copy's stored slice."""
        if copy.size is not None and float(copy.size) > 0:
            return min(float(copy.size), hl_sz)
        others = self._other_open_copies_for_coin(coin, copy.investment_base_id)
        if others:
            raise HlTraderError(
                f"CLOSE {coin}: open_copy has no stored size and other copies "
                f"exist ({[o.base_short_id or o.investment_base_id for o in others]}) "
                f"— refuse full flatten"
            )
        return hl_sz

    @staticmethod
    def _should_increase_size(inv: InvestmentUpdate) -> bool:
        """True when Involio marks a position increase (Copy on Position Increased)."""
        changes = inv.changes or {}
        if changes.get("simIncrease") is True:
            return True
        prev = changes.get("entrySize")
        if prev is None or inv.entry_size is None:
            return False
        try:
            return float(inv.entry_size) > float(prev) * 1.02
        except (TypeError, ValueError):
            return False

    def _apply_size_increase(
        self,
        trader: HlTrader,
        *,
        inv: InvestmentUpdate,
        coin: str,
        is_long: bool,
        current_sz: float,
        equity: float,
    ) -> tuple[float, dict[str, Any] | None]:
        """Buy up toward trader's new entrySize allocation. Returns (new_sz, submission)."""
        leverage = int(inv.leverage or 1)
        mid = trader.mid_price(coin)
        current_notional = float(current_sz) * mid
        _margin, meta = self._resolve_margin(
            equity=equity, leverage=leverage, entry_size=inv.entry_size
        )
        target_notional = float(meta["order_notional_usd"])
        hl_min = float(self.settings.hl_min_order_usd)
        delta_notional = target_notional - current_notional
        pending_add = 0.0
        if self.db is not None and inv.base_id is not None:
            copy = self.db.get_open_copy(str(inv.base_id))
            if copy is not None:
                pending_add = float(copy.pending_add_notional or 0)
        effective_delta = delta_notional + pending_add
        if effective_delta + 1e-9 < hl_min:
            accumulated = pending_add + max(0.0, delta_notional)
            if self.db is not None and inv.base_id is not None:
                self.db.set_pending_add_notional(str(inv.base_id), accumulated)
            console.print(
                f"[dim]UPDATE {coin}: size increase too small "
                f"(Δnotional=${delta_notional:.2f} + pending=${pending_add:.2f} "
                f"< HL min ${hl_min:g}) — accumulated ${accumulated:.2f}[/dim]"
            )
            log_event(
                "size_increase_skip",
                coin=coin,
                reason="below_hl_min",
                current_notional=current_notional,
                target_notional=target_notional,
                delta_notional=delta_notional,
                pending_add_notional=accumulated,
            )
            return current_sz, None
        delta_notional = effective_delta

        available = trader.available_margin()
        need_margin = delta_notional / max(1, leverage)
        if available + 1e-9 < self.settings.min_order_usd:
            raise InsufficientMarginError(
                f"UPDATE {coin} size increase: no free margin "
                f"(available=${available:.2f})"
            )
        if need_margin > available:
            delta_notional = available * 0.98 * max(1, leverage)
            if delta_notional + 1e-9 < hl_min:
                raise InsufficientMarginError(
                    f"UPDATE {coin} size increase: free margin too small "
                    f"(available=${available:.2f})"
                )
            console.print(
                f"[yellow]UPDATE {coin}: shrunk size-increase notional to "
                f"${delta_notional:.2f} (available margin)[/yellow]"
            )

        trader_entry = float(inv.entry_price) if inv.entry_price else None
        if trader_entry is not None and trader_entry <= 0:
            trader_entry = None
        if not price_is_at_or_better(mid=mid, trader_entry=trader_entry, is_long=is_long):
            if self.db is not None and inv.base_id is not None:
                self.db.set_pending_add_notional(str(inv.base_id), effective_delta)
            console.print(
                f"[dim]UPDATE {coin}: size increase waiting for entry-or-better "
                f"(mid={mid} trader={trader_entry}) — accumulated "
                f"${effective_delta:.2f}[/dim]"
            )
            log_event(
                "size_increase_skip",
                coin=coin,
                reason="price_worse",
                mid=mid,
                trader_entry=trader_entry,
                pending_add_notional=effective_delta,
            )
            return current_sz, None

        add_sz = trader.round_size(coin, delta_notional / mid)
        if add_sz <= 0:
            return current_sz, None

        cloid = mimic_size_increase_cloid(inv.base_short_id, copy_update=True)
        console.print(
            f"[cyan]LIVE SIZE INCREASE[/cyan] {coin} "
            f"{'LONG' if is_long else 'SHORT'} +sz={add_sz} "
            f"(~${add_sz * mid:.2f} notional; {current_sz} → ~{current_sz + add_sz})"
        )
        logger.info(
            "SIZE INCREASE %s add_sz=%s current=%s target_notional=%.4f "
            "delta_notional=%.4f cloid=%s",
            coin,
            add_sz,
            current_sz,
            target_notional,
            delta_notional,
            cloid.to_raw() if cloid else None,
        )
        try:
            hl_order, hl_resp, nonce = trader.market_order(
                coin,
                is_buy=is_long,
                sz=add_sz,
                limit_px=trader_entry,
                cloid=cloid,
            )
            self._check_hl_ok(hl_resp, "size_increase")
        except IocUnfilledError:
            if self.db is not None and inv.base_id is not None:
                self.db.set_pending_add_notional(str(inv.base_id), effective_delta)
            log_event(
                "size_increase_skip",
                coin=coin,
                reason="ioc_unfilled",
                pending_add_notional=effective_delta,
            )
            return current_sz, None
        except HlTraderError as exc:
            if "insufficient margin" in str(exc).lower():
                raise InsufficientMarginError(str(exc)) from exc
            raise

        new_sz = current_sz + add_sz
        entry_px = mid
        try:
            detail = trader.position_detail(coin)
            if detail:
                new_sz = float(detail["sz"] or new_sz)
                if detail.get("entry_px"):
                    entry_px = float(detail["entry_px"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIZE INCREASE fill lookup failed coin=%s: %s", coin, exc)

        if self.db is not None and inv.base_id is not None:
            self.db.set_open_copy_fill(
                str(inv.base_id),
                our_entry_px=entry_px,
                side="long" if is_long else "short",
                size=new_sz,
                leverage=float(leverage),
            )
            self.db.set_pending_add_notional(str(inv.base_id), 0.0)

        avg_px = extract_avg_px(hl_resp)
        our_px = avg_px if avg_px is not None else entry_px
        price_gap_pct, roe_gap_pct = fill_gaps(
            our_px=our_px,
            trader_entry=trader_entry,
            is_long=is_long,
            leverage=leverage,
        )
        log_event(
            "size_increase_hl",
            coin=coin,
            add_sz=add_sz,
            qty_before=current_sz,
            qty_after=new_sz,
            cloid=cloid.to_raw() if cloid else None,
            trader_entry=trader_entry,
            mid=mid,
            avg_px=avg_px,
            price_gap_pct=price_gap_pct,
            roe_gap_pct=roe_gap_pct,
            hl_response=hl_resp,
            **{k: v for k, v in meta.items() if k not in {"margin_usd", "order_notional_usd"}},
        )
        return new_sz, {
            "kind": "size_increase",
            "hlOrder": hl_order,
            "nonceMs": nonce,
            "hlResponse": hl_resp,
        }

    def _refuse_same_coin_open(
        self, coin: str, *, exclude_base_id: str | None = None
    ) -> None:
        """Block a second open while we already mimic someone on this coin."""
        if self.db is None:
            return
        existing = self._other_open_copies_for_coin(coin, exclude_base_id)
        if not existing:
            return
        row = existing[0]
        raise SameCoinOpenError(
            f"Already mimicking {coin} via "
            f"{row.base_short_id or row.investment_base_id}"
            f"{f' ({row.side})' if row.side else ''}; "
            f"skip second same-coin open"
        )

    def _sync_tpsl(
        self,
        trader: HlTrader,
        *,
        coin: str,
        is_long: bool,
        sz: float,
        tp: float | None,
        sl: float | None,
        context: str,
        base_short_id: str | None = None,
        copy_update: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], int] | None:
        """Best-effort: replace HL reduce-only TP/SL from the trader's levels.

        When ``copy_update`` is True, cloids use Involio ``mimicCopyUpdate`` (same
        as pressing Copy on a Trade Update). Returns the placed HL payload for
        ``/dex/mimic/copy``, or None if nothing was placed.
        """
        tp_px = float(tp) if tp is not None and float(tp) > 0 else None
        sl_px = float(sl) if sl is not None and float(sl) > 0 else None

        # Drop legs on the wrong side of mid (HL rejects / would trigger immediately).
        try:
            mid = trader.mid_price(coin)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s %s: mid lookup failed for TP/SL: %s", context, coin, exc)
            mid = None
        if mid is not None and mid > 0:
            if tp_px is not None:
                bad_tp = tp_px <= mid if is_long else tp_px >= mid
                if bad_tp:
                    logger.warning(
                        "%s %s: skip TP=%s (mid=%s %s) — wrong side of market",
                        context,
                        coin,
                        tp_px,
                        mid,
                        "long" if is_long else "short",
                    )
                    console.print(
                        f"[yellow]{context.upper()} {coin}: skip TP={tp_px} "
                        f"(mid={mid}) — wrong side[/yellow]"
                    )
                    tp_px = None
            if sl_px is not None:
                bad_sl = sl_px >= mid if is_long else sl_px <= mid
                if bad_sl:
                    logger.warning(
                        "%s %s: skip SL=%s (mid=%s %s) — wrong side of market",
                        context,
                        coin,
                        sl_px,
                        mid,
                        "long" if is_long else "short",
                    )
                    console.print(
                        f"[yellow]{context.upper()} {coin}: skip SL={sl_px} "
                        f"(mid={mid}) — wrong side[/yellow]"
                    )
                    sl_px = None

        if tp_px is None and sl_px is None:
            # Still clear stale triggers when trader removed both (update only).
            if context == "update":
                try:
                    cancelled = trader.cancel_reduce_triggers(coin)
                    if cancelled:
                        console.print(
                            f"[dim]UPDATE {coin}: cleared TP/SL triggers "
                            f"oids={cancelled}[/dim]"
                        )
                        log_event(
                            "tpsl_cleared",
                            context=context,
                            coin=coin,
                            cancelled_oids=cancelled,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("UPDATE %s: clear TP/SL failed: %s", coin, exc)
            return None

        tp_cloid = (
            mimic_tp_cloid(base_short_id, copy_update=copy_update)
            if tp_px is not None
            else None
        )
        sl_cloid = (
            mimic_sl_cloid(base_short_id, copy_update=copy_update)
            if sl_px is not None
            else None
        )
        try:
            placed = trader.sync_position_tpsl(
                coin,
                is_long=is_long,
                sz=sz,
                tp_px=tp_px,
                sl_px=sl_px,
                tp_cloid=tp_cloid,
                sl_cloid=sl_cloid,
            )
            if placed is None:
                return None
            hl_order, hl_resp, nonce = placed
            self._check_hl_ok(hl_resp, f"{context}_tpsl")
            console.print(
                f"[green]{context.upper()} {coin} TP/SL set[/green] "
                f"tp={tp_px} sl={sl_px} sz={sz} "
                f"copy_update={copy_update}"
            )
            logger.info(
                "%s %s TP/SL ok tp=%s sl=%s sz=%s copy_update=%s "
                "tp_cloid=%s sl_cloid=%s resp=%s",
                context,
                coin,
                tp_px,
                sl_px,
                sz,
                copy_update,
                tp_cloid.to_raw() if tp_cloid else None,
                sl_cloid.to_raw() if sl_cloid else None,
                hl_resp,
            )
            log_event(
                "tpsl_set",
                context=context,
                coin=coin,
                side="long" if is_long else "short",
                sz=sz,
                tp=tp_px,
                sl=sl_px,
                copy_update=copy_update,
                tp_cloid=tp_cloid.to_raw() if tp_cloid else None,
                sl_cloid=sl_cloid.to_raw() if sl_cloid else None,
                hl_response=hl_resp,
            )
            return placed
        except Exception as exc:  # noqa: BLE001
            # Position already open — don't fail the trade for TP/SL placement.
            console.print(
                f"[yellow]{context.upper()} {coin}: TP/SL place failed "
                f"(position kept):[/yellow] {exc}"
            )
            logger.warning("%s %s TP/SL failed: %s", context, coin, exc)
            log_event(
                "tpsl_error",
                context=context,
                coin=coin,
                tp=tp_px,
                sl=sl_px,
                sz=sz,
                copy_update=copy_update,
                error=str(exc),
            )
            return None

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
        # Keep the order size we just submitted — never replace with net HL size.
        # Net size includes other same-coin copies and would break partial closes.
        fill_sz = float(sz)
        try:
            trader = self._ensure_trader()
            detail = trader.position_detail(coin)
            if detail and detail.get("entry_px"):
                entry_px = float(detail["entry_px"])
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
            size=fill_sz,
            leverage=float(leverage),
            trader=inv.owner.username if inv.owner else None,
        )
        log_event(
            "open_fill_persisted",
            base_id=base_id,
            coin=coin,
            our_entry_px=entry_px,
            trader_entry=inv.entry_price,
            mid=mid,
            side="long" if is_long else "short",
            size=fill_sz,
            leverage=leverage,
        )

    def recover_missed_close(
        self,
        *,
        coin: str,
        inv: InvestmentUpdate,
        our_entry_px: float | None = None,
        our_side: str | None = None,
        our_size: float | None = None,
        copy_base_id: str | None = None,
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
        exclude_id = copy_base_id or (
            str(inv.base_id) if inv.base_id is not None else None
        )
        shared = self._other_open_copies_for_coin(coin, exclude_id)

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

        hl_sz = float(detail["sz"])
        is_long = bool(detail["is_long"])
        if is_long_hint is not None and is_long_hint != is_long:
            # Another copy may have flipped the net side — do not trade against it.
            msg = (
                f"RECOVERY {coin}: side mismatch copy="
                f"{'long' if is_long_hint else 'short'} hl="
                f"{'long' if is_long else 'short'} — skip HL"
            )
            console.print(f"[yellow]{msg}[/yellow]")
            logger.warning(msg)
            log_event(
                "recovery_skip_side_mismatch",
                coin=coin,
                copy_side="long" if is_long_hint else "short",
                hl_side="long" if is_long else "short",
                base_id=exclude_id,
            )
            return RecoveryResult(
                status="already_flat",
                coin=coin,
                reason="side_mismatch",
                message=msg,
            )
        if our_size is not None and float(our_size) > 0:
            sz = min(float(our_size), hl_sz)
        elif shared:
            raise HlTraderError(
                f"RECOVERY {coin}: no stored size and other same-coin copies exist "
                f"({[o.base_short_id or o.investment_base_id for o in shared]}) "
                f"— refuse full flatten"
            )
        else:
            sz = hl_sz
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
        # Parking positionTpsl cancels other coin triggers — when another open_copy
        # shares the coin, always reduce-only close our slice instead.
        if decision.action == "park" and shared:
            logger.info(
                "RECOVERY %s: forcing close (not park) — other copies on coin: %s",
                coin,
                [o.base_short_id or o.investment_base_id for o in shared],
            )
            decision = RecoveryDecision(
                action="close",
                reason=f"{decision.reason}+shared_coin_no_park",
                our_pnl_pct=decision.our_pnl_pct,
                trader_pnl_pct=decision.trader_pnl_pct,
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
            close_sz=sz,
            hl_sz=hl_sz,
            shared_copies=len(shared),
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
                f"RECOVERY close {coin} sz={sz}/{hl_sz} reason={decision.reason} "
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
                hl_sz=hl_sz,
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
            if not isinstance(st, dict):
                continue
            err = st.get("error")
            if not err:
                continue
            err_s = str(err)
            if ioc_unfilled_message(err_s):
                raise IocUnfilledError(f"HL {label} IoC unfilled: {err_s}")
            raise HlTraderError(f"HL {label} order error: {err_s}")


def _pct(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value * 100:.2f}%"
