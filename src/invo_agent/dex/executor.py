from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from invo_agent.api.client import InvolioClient
from invo_agent.api.models import ActionType, InvestmentUpdate, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB, OpenCopy
from invo_agent.dex.budget import BudgetDeniedError, RequestBudget, daily_volume_from_user_fees
from invo_agent.dex.cloid import (
    mimic_close_cloid,
    mimic_entry_cloid,
    mimic_size_increase_cloid,
    mimic_size_reduce_cloid,
    mimic_sl_cloid,
    mimic_tp_cloid,
)
from invo_agent.dex.hl_trader import (
    TPSL_UNCHANGED,
    GhostCopyError,
    HlTrader,
    HlTraderError,
    InsufficientMarginError,
    IocUnfilledError,
    NotOpenCopyError,
    PriceNotBetterError,
    RecoveryParkedError,
    SameCoinOpenError,
    SideMismatchError,
    extract_fill_sz_px,
    extract_resting_oid,
)
from invo_agent.dex.pricing import (
    extract_avg_px,
    fill_gaps,
    ioc_unfilled_message,
    price_is_at_or_better,
    size_clearing_min_notional,
)
from invo_agent.dex.recovery import (
    RecoveryDecision,
    decide_missed_close,
    looks_like_unlevered_park_sl,
)
from invo_agent.dex.session import DexSessionError, DexSessionStore, load_signer
from invo_agent.dex.turnkey_crypto import new_client_tx_id
from invo_agent.events import log_event, log_event_throttled
from invo_agent.labels import merge_update_tags, update_labels

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
    # False when nothing was sent to HL (park already resting) — callers use this
    # to stay quiet instead of logging the same park every reconcile.
    changed: bool = True


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
        self._budget: RequestBudget | None = None
        self.last_update_tags: list[str] = []

    @property
    def live_enabled(self) -> bool:
        return bool(self.settings.live) and not bool(self.settings.dry_run)

    def invalidate_trader(self) -> None:
        """Drop cached HL trader (e.g. after Turnkey API key expiry)."""
        self._trader = None
        self._dex_session = None

    def budget(self) -> RequestBudget | None:
        if self.db is None:
            return None
        if self._budget is None:
            self._budget = RequestBudget(
                self.db,
                reserve=float(self.settings.hl_budget_reserve),
                allow_deficit=bool(self.settings.hl_budget_allow_deficit),
            )
        return self._budget

    def _ensure_trader(self) -> HlTrader:
        if self._trader is not None:
            return self._trader
        data, signer = load_signer(self.dex_store)
        self._dex_session = data
        self._trader = HlTrader(
            signer,
            data.master_address,
            mainnet=True,
            budget=self.budget(),
            leverage_stale_seconds=float(self.settings.hl_leverage_stale_seconds),
        )
        return self._trader

    def handle(self, action: TradeAction) -> None:
        self.last_update_tags = []
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
        trader._ensure_book()
        equity = trader.account_value()
        logger.info(
            "LIVE handle %s equity=%.4f pct=%s summary=%s",
            action.action.value,
            equity,
            self.settings.position_pct,
            action.summary,
        )
        log_event_throttled(
            "live_handle",
            key=str(action.notification.id),
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
            log_event_throttled(
                "live_skip_margin",
                key=str(action.notification.id),
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except SameCoinOpenError as exc:
            logger.warning("LIVE %s skipped (same coin): %s", action.action.value, exc)
            log_event_throttled(
                "live_skip_same_coin",
                key=str(action.notification.id),
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
        except GhostCopyError as exc:
            logger.info("LIVE %s ghost cleared: %s", action.action.value, exc)
            log_event(
                "live_ghost_clear",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except RecoveryParkedError as exc:
            logger.info("LIVE %s kept parked: %s", action.action.value, exc)
            log_event(
                "live_skip_recovery_parked",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except SideMismatchError as exc:
            logger.warning("LIVE %s side mismatch: %s", action.action.value, exc)
            log_event(
                "live_skip_side_mismatch",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except BudgetDeniedError as exc:
            logger.info("LIVE %s deferred (budget): %s", action.action.value, exc)
            log_event_throttled(
                "live_budget_denied",
                key=str(action.notification.id),
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=str(action.notification.id),
            )
            raise
        except PriceNotBetterError as exc:
            logger.info("LIVE %s queued (price): %s", action.action.value, exc)
            log_event_throttled(
                "live_queue_price",
                key=str(action.notification.id),
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
        """Involio entrySize is already percent of the trader's book.

        UI 0.8% arrives as 0.801; 1% as 1.0; 12% as 12.54. Do not treat
        values ≤ 1 as a 0–1 fraction (that turned 0.8% into 80%).
        """
        if entry_size is None or entry_size <= 0:
            return None
        if entry_size > 100.0:
            return None
        return float(entry_size)

    @staticmethod
    def _copy_margin_usd(copy: OpenCopy) -> float:
        sz = float(copy.size or 0.0)
        px = float(copy.our_entry_px or 0.0)
        if sz <= 0 or px <= 0:
            return 0.0
        lev = max(1.0, float(copy.leverage or 1.0))
        return sz * px / lev

    def _book_margin_used_usd(self, *, exclude_base_id: str | None = None) -> float:
        if self.db is None:
            return 0.0
        total = 0.0
        for row in self.db.list_open_copies():
            if exclude_base_id and row.investment_base_id == exclude_base_id:
                continue
            total += self._copy_margin_usd(row)
        return total

    @staticmethod
    def _change_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:  # NaN
            return None
        return number

    @classmethod
    def _relative_add_ratio(cls, inv: InvestmentUpdate) -> float | None:
        """How much they added vs dollars already in the trade.

        Official modify is ``simDifference / previous entrySim`` (e.g. +$35 on
        $25 = +137% of the clip). ``entrySize`` is only a fallback: the *change*
        in their book % applied to our clip — never the new % of our account.
        """
        changes = inv.changes or {}
        sim_diff = cls._change_float(changes.get("simDifference"))
        old_sim = cls._change_float(changes.get("entrySim"))
        if (
            sim_diff is not None
            and old_sim is not None
            and old_sim > 0
            and sim_diff > 0
        ):
            return sim_diff / old_sim
        new_sim = cls._change_float(inv.entry_sim)
        if (
            new_sim is not None
            and old_sim is not None
            and old_sim > 0
            and new_sim > old_sim
        ):
            return (new_sim - old_sim) / old_sim
        old_pct = cls._change_float(changes.get("entrySize"))
        new_pct = cls._change_float(inv.entry_size)
        if (
            old_pct is not None
            and new_pct is not None
            and old_pct > 0
            and new_pct > old_pct
        ):
            return (new_pct - old_pct) / old_pct
        return None

    @classmethod
    def _relative_reduce_ratio(cls, inv: InvestmentUpdate) -> float | None:
        """How much they cut vs dollars already in the trade (0–1)."""
        changes = inv.changes or {}
        sim_diff = cls._change_float(changes.get("simDifference"))
        old_sim = cls._change_float(changes.get("entrySim"))
        if (
            sim_diff is not None
            and old_sim is not None
            and old_sim > 0
            and sim_diff > 0
        ):
            return min(1.0, sim_diff / old_sim)
        new_sim = cls._change_float(inv.entry_sim)
        if (
            new_sim is not None
            and old_sim is not None
            and old_sim > 0
            and new_sim < old_sim
        ):
            return (old_sim - new_sim) / old_sim
        old_pct = cls._change_float(changes.get("entrySize"))
        new_pct = cls._change_float(inv.entry_size)
        if (
            old_pct is not None
            and new_pct is not None
            and old_pct > 0
            and new_pct < old_pct
        ):
            return (old_pct - new_pct) / old_pct
        return None

    def _cap_position_notional(
        self,
        *,
        equity: float,
        leverage: int,
        desired_notional: float,
        exclude_base_id: str | None = None,
    ) -> tuple[float, dict[str, Any]]:
        """Shrink a desired position notional to per-copy and book caps."""
        lev = max(1, int(leverage or 1))
        max_pct = float(self.settings.max_position_pct)
        book_max_pct = float(self.settings.max_book_margin_pct)
        copy_cap = max(0.0, equity * max_pct / 100.0) * lev
        book_used = self._book_margin_used_usd(exclude_base_id=exclude_base_id)
        room_usd = (
            max(0.0, equity * book_max_pct / 100.0 - book_used) if equity > 0 else 0.0
        )
        room_ntl = room_usd * lev
        this_cap = min(copy_cap, room_ntl)
        desired = max(0.0, float(desired_notional))
        final = min(desired, this_cap)
        book_capped = room_ntl + 1e-9 < copy_cap and final + 1e-9 < desired
        capped = final + 1e-9 < desired
        final_pct = (final / lev / equity) * 100.0 if equity > 0 and lev > 0 else 0.0
        return final, {
            "capped": capped,
            "book_capped": book_capped,
            "book_used_usd": book_used,
            "book_room_usd": room_usd,
            "max_book_margin_pct": book_max_pct,
            "final_pct": final_pct,
            "copy_cap_notional": copy_cap,
            "leverage": lev,
        }

    def _size_add_need(
        self,
        *,
        inv: InvestmentUpdate,
        current_notional: float,
        equity: float,
        pending_add: float,
        pending_only: bool,
        exclude_base_id: str | None = None,
    ) -> tuple[float, dict[str, Any]]:
        """USD notional to add: grow our clip by their add-ratio, then cap.

        Drain retries use the already-queued leftover only — do not re-apply
        the same ``simDifference`` onto a clip we already fattened.
        """
        lev = max(1, int(inv.leverage or 1))
        ratio = None if pending_only else self._relative_add_ratio(inv)
        changes = inv.changes or {}
        if pending_only:
            raw_need = max(0.0, pending_add)
            source = "pending"
        elif ratio is not None and ratio > 0:
            raw_need = max(0.0, current_notional * ratio)
            if pending_add > 1e-9:
                raw_need += pending_add
            source = "relative_add"
        else:
            raw_need = max(0.0, pending_add)
            source = "pending" if pending_add > 1e-9 else "none"

        desired = current_notional + raw_need
        cap_ntl, cap_meta = self._cap_position_notional(
            equity=equity,
            leverage=lev,
            desired_notional=desired,
            exclude_base_id=exclude_base_id,
        )
        need = max(0.0, cap_ntl - current_notional)
        trader_pct = self._trader_allocation_pct(inv.entry_size)
        meta = {
            "source": source,
            "add_ratio": ratio,
            "sim_difference": self._change_float(changes.get("simDifference")),
            "old_entry_sim": self._change_float(changes.get("entrySim")),
            "trader_pct": trader_pct,
            "target_pct": trader_pct,
            "target_notional": cap_ntl,
            "uncapped_need": raw_need,
            "uncapped_target": desired,
            "final_pct": cap_meta["final_pct"],
            "capped": cap_meta["capped"],
            "book_capped": cap_meta["book_capped"],
            "book_used_usd": cap_meta["book_used_usd"],
            "book_room_usd": cap_meta["book_room_usd"],
            "max_book_margin_pct": cap_meta["max_book_margin_pct"],
            "hl_min_order_usd": float(self.settings.hl_min_order_usd),
            "leverage": lev,
        }
        return need, meta

    def _resolve_margin(
        self,
        *,
        equity: float,
        leverage: int,
        entry_size: float | None,
        exclude_base_id: str | None = None,
    ) -> tuple[float, dict[str, Any]]:
        """Copy trader % of our equity; bump up if HL $min notional wouldn't clear.

        Then cap per-copy margin % and the book's used+this-order margin %.
        Caps can shrink below the $10 floor — caller skips / queues.
        """
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
        book_capped = False
        max_pct = float(self.settings.max_position_pct)
        if final_pct > max_pct:
            final_pct = max_pct
            capped = True

        book_max_pct = float(self.settings.max_book_margin_pct)
        book_used = self._book_margin_used_usd(exclude_base_id=exclude_base_id)
        room_usd = 0.0
        room_pct = 0.0
        if equity > 0:
            room_usd = max(0.0, equity * book_max_pct / 100.0 - book_used)
            room_pct = (room_usd / equity) * 100.0
            if final_pct > room_pct + 1e-9:
                final_pct = room_pct
                book_capped = True
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
            "book_capped": book_capped,
            "book_used_usd": book_used,
            "book_room_usd": room_usd,
            "max_book_margin_pct": book_max_pct,
            "margin_usd": margin,
            "order_notional_usd": order_notional,
            "hl_min_order_usd": hl_min,
            "leverage": lev,
        }
        return margin, meta

    @staticmethod
    def _sz_for_notional(
        trader: Any,
        coin: str,
        notional: float,
        mid: float,
        min_notional: float,
    ) -> float:
        if hasattr(trader, "size_for_notional"):
            return float(
                trader.size_for_notional(
                    coin, notional, mid=mid, min_notional=min_notional
                )
            )
        if hasattr(trader, "sz_decimals"):
            return size_clearing_min_notional(
                mid=mid,
                notional=notional,
                min_notional=min_notional,
                sz_decimals=int(trader.sz_decimals(coin)),
            )
        return float(trader.round_size(coin, notional / mid))

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
            equity=equity,
            leverage=leverage,
            entry_size=inv.entry_size,
            exclude_base_id=str(inv.base_id) if inv.base_id is not None else None,
        )
        available = trader.available_margin()
        size_meta["available_margin_usd"] = available
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
        hl_min = float(self.settings.hl_min_order_usd)
        sz = self._sz_for_notional(trader, coin, order_notional, mid, hl_min)
        order_notional = float(sz) * mid
        size_meta["order_notional_usd"] = order_notional
        size_meta["margin_usd"] = order_notional / max(1, leverage)
        margin = float(size_meta["margin_usd"])
        if order_notional + 1e-9 < hl_min:
            raise InsufficientMarginError(
                f"Order notional ${order_notional:.2f} < HL_MIN_ORDER_USD="
                f"{hl_min} after lot rounding "
                f"(available=${available:.2f})"
            )
        if margin > available + 1e-9:
            raise InsufficientMarginError(
                f"Rounded {coin} needs ${margin:.2f} margin, available=${available:.2f}"
            )
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
            trader.ensure_leverage(coin, leverage, is_cross=True)
        except BudgetDeniedError:
            raise
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
            if tpsl_placed is not None and tpsl_placed != TPSL_UNCHANGED:
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
            self._clear_ghost_copy(copy, coin=coin, reason="update_flat")
            raise GhostCopyError(
                f"UPDATE {coin}: ghost copy cleared (flat on HL; "
                f"shortId={copy.base_short_id})"
            )

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
        # Queue retry is size-only. A blocked size↑ must not cancel/replace TP/SL
        # or rewrite leverage every poll — that burned the HL request budget.
        if action.reason == "pending_add_drain":
            if self._size_direction(inv) != "up":
                return
            qty_before = sz
            sz, size_sub = self._apply_size_increase(
                trader,
                inv=inv,
                coin=coin,
                is_long=is_long,
                current_sz=sz,
                equity=equity,
                pending_only=True,
            )
            if size_sub is None:
                return
            leverage = int(inv.leverage or 1)
            try:
                recorded = self._mimic_copy(
                    inv=inv,
                    update_type="increase",
                    coin=coin,
                    asset_index=trader.asset_index(coin),
                    client_tx=new_client_tx_id(),
                    submissions=[size_sub],
                    summary={
                        "qtyBefore": str(qty_before),
                        "qtyAfter": str(sz),
                        "intendedLeverage": leverage,
                    },
                    include_update_base_short_id=True,
                )
                logger.info(
                    "UPDATE drain recorded coin=%s sz=%s→%s: %s",
                    coin,
                    qty_before,
                    sz,
                    recorded,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "UPDATE drain mimic/copy failed coin=%s (HL size kept): %s",
                    coin,
                    exc,
                )
            tags = merge_update_tags(update_labels(inv), ["size↑"])
            self.last_update_tags = tags
            return

        leverage = int(inv.leverage or 1)
        try:
            trader.ensure_leverage(coin, leverage, is_cross=True)
        except BudgetDeniedError as exc:
            logger.warning("UPDATE %s leverage skipped (budget): %s", coin, exc)
        except HlTraderError as exc:
            # Size-add / TP/SL must still run — leverage is best-effort on UPDATE.
            console.print(
                f"[yellow]UPDATE {coin}: leverage failed — continuing "
                f"size/TP/SL ({exc})[/yellow]"
            )
            logger.warning("UPDATE %s leverage failed (continuing): %s", coin, exc)
            log_event("update_leverage_skip", coin=coin, error=str(exc))

        qty_before = sz
        size_sub: dict[str, Any] | None = None
        direction = self._size_direction(inv)
        if direction == "up":
            sz, size_sub = self._apply_size_increase(
                trader,
                inv=inv,
                coin=coin,
                is_long=is_long,
                current_sz=sz,
                equity=equity,
            )
        elif direction == "down":
            sz, size_sub = self._apply_size_decrease(
                trader,
                inv=inv,
                coin=coin,
                is_long=is_long,
                current_sz=sz,
                equity=equity,
            )
            if sz <= 1e-12:
                tags = merge_update_tags(update_labels(inv), ["size↓"])
                self.last_update_tags = tags
                return

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
        if size_sub is None and tpsl_placed == TPSL_UNCHANGED:
            return
        if tpsl_placed == TPSL_UNCHANGED:
            tpsl_placed = None

        client_tx = new_client_tx_id()
        submissions: list[dict[str, Any]] = []
        has_tp = inv.price_target is not None and float(inv.price_target) > 0
        # One Copy card → one primary updateType. Prefer size_increase when we added.
        if size_sub is not None and size_sub.get("kind") == "size_increase":
            update_type = "increase"
            submissions.append(size_sub)
        elif size_sub is not None:
            update_type = "entry"
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

        tags = update_labels(inv)
        if size_sub is not None and size_sub.get("kind") == "size_increase":
            tags = merge_update_tags(tags, ["size↑"])
        elif size_sub is not None:
            tags = merge_update_tags(tags, ["size↓"])
        self.last_update_tags = tags

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
        if copy.recovery_pending:
            # Recovery already decided a market exit here locks in a worse loss
            # than the trader took, and parked reduce-only TP/SL instead.
            console.print(
                f"[yellow]CLOSE {coin}: recovery parked "
                f"(tp={copy.recovery_tp} sl={copy.recovery_sl}) — keep park[/yellow]"
            )
            logger.info(
                "CLOSE %s skipped — recovery parked base_id=%s tp=%s sl=%s",
                coin,
                copy.investment_base_id,
                copy.recovery_tp,
                copy.recovery_sl,
            )
            log_event(
                "close_skip_recovery_parked",
                coin=coin,
                base_id=copy.investment_base_id,
                base_short_id=copy.base_short_id,
                recovery_tp=copy.recovery_tp,
                recovery_sl=copy.recovery_sl,
            )
            raise RecoveryParkedError(
                f"CLOSE {coin}: recovery parked tp={copy.recovery_tp} "
                f"sl={copy.recovery_sl} — keep park"
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
                # Net HL side no longer matches this copy. Do not trade and
                # do not drop local tracking (poller skips remember()).
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
                raise SideMismatchError(
                    f"CLOSE {coin}: side mismatch copy={copy.side} "
                    f"hl={'long' if is_long else 'short'}"
                )
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
        """Size to reduce on CLOSE.

        Only copy on the coin → flatten live HL (leftover GTC fills can sit
        above stored size). Shared coin → never more than this copy's slice.
        """
        others = self._other_open_copies_for_coin(coin, copy.investment_base_id)
        if others:
            if copy.size is not None and float(copy.size) > 0:
                return min(float(copy.size), hl_sz)
            raise HlTraderError(
                f"CLOSE {coin}: open_copy has no stored size and other copies "
                f"exist ({[o.base_short_id or o.investment_base_id for o in others]}) "
                f"— refuse full flatten"
            )
        return hl_sz

    def _has_pending_add(self, inv: InvestmentUpdate) -> bool:
        if self.db is None or inv.base_id is None:
            return False
        copy = self.db.get_open_copy(str(inv.base_id))
        return bool(copy and float(copy.pending_add_notional or 0) > 0)

    def _size_direction(self, inv: InvestmentUpdate) -> str | None:
        """``up``, ``down``, or None (TP/SL/lev only)."""
        changes = inv.changes or {}
        if changes.get("simIncrease") is True:
            return "up"
        prev = changes.get("entrySize")
        if prev is not None and inv.entry_size is not None:
            try:
                curr = float(inv.entry_size)
                old = float(prev)
                if curr > old * 1.02:
                    return "up"
                if curr < old * 0.98:
                    return "down"
            except (TypeError, ValueError):
                pass
        if self._has_pending_add(inv):
            return "up"
        return None

    def _should_increase_size(self, inv: InvestmentUpdate) -> bool:
        return self._size_direction(inv) == "up"

    def _clear_ghost_copy(self, copy: OpenCopy, *, coin: str, reason: str) -> None:
        if self.db is not None:
            self.db.close_copy(copy.investment_base_id)
        console.print(
            f"[yellow]GHOST {coin}: cleared {copy.base_short_id or copy.investment_base_id} "
            f"({reason})[/yellow]"
        )
        logger.info(
            "ghost clear coin=%s base_id=%s shortId=%s reason=%s",
            coin,
            copy.investment_base_id,
            copy.base_short_id,
            reason,
        )
        log_event(
            "ghost_clear",
            coin=coin,
            base_id=copy.investment_base_id,
            base_short_id=copy.base_short_id,
            reason=reason,
        )

    def reconcile_ghost_copies(self, trader: HlTrader) -> list[str]:
        """Drop open_copies whose coin is flat on HL. Empty held set is OK."""
        if self.db is None:
            return []
        try:
            held = trader.open_coins()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ghost reconcile skipped (HL read failed): %s", exc)
            return []
        cleared: list[str] = []
        for copy in self.db.list_open_copies():
            coin = (copy.ticker or "").upper()
            if not coin or coin in held:
                continue
            self._clear_ghost_copy(copy, coin=coin, reason="poll_flat")
            cleared.append(coin)
        return cleared

    def _stash_pending_add(
        self,
        inv: InvestmentUpdate,
        notional: float,
        *,
        reason: str,
        entry: float | None,
    ) -> None:
        if self.db is None or inv.base_id is None:
            return
        self.db.set_pending_add_notional(
            str(inv.base_id),
            notional,
            entry=entry,
            reason=reason,
        )

    def _apply_size_increase(
        self,
        trader: HlTrader,
        *,
        inv: InvestmentUpdate,
        coin: str,
        is_long: bool,
        current_sz: float,
        equity: float,
        pending_only: bool = False,
    ) -> tuple[float, dict[str, Any] | None]:
        """Grow our clip by the trader's add-ratio. Returns (new_sz, submission)."""
        leverage = int(inv.leverage or 1)
        mid = trader.mid_price(coin)
        current_notional = float(current_sz) * mid
        hl_min = float(self.settings.hl_min_order_usd)
        pending_add = 0.0
        pending_entry: float | None = None
        if self.db is not None and inv.base_id is not None:
            copy = self.db.get_open_copy(str(inv.base_id))
            if copy is not None:
                pending_add = float(copy.pending_add_notional or 0)
                pending_entry = copy.pending_add_entry
        need, meta = self._size_add_need(
            inv=inv,
            current_notional=current_notional,
            equity=equity,
            pending_add=pending_add,
            pending_only=pending_only,
            exclude_base_id=str(inv.base_id) if inv.base_id is not None else None,
        )
        target_notional = float(meta["target_notional"])

        trader_entry = float(inv.entry_price) if inv.entry_price else None
        if trader_entry is not None and trader_entry <= 0:
            trader_entry = None
        if trader_entry is None and pending_entry is not None and pending_entry > 0:
            trader_entry = float(pending_entry)

        available = trader.available_margin()
        lev = max(1, leverage)
        buyable = available * 0.98 * lev
        if need + 1e-9 < hl_min:
            accumulated = need
            if accumulated <= 1e-9:
                if self.db is not None and inv.base_id is not None:
                    self.db.set_pending_add_notional(str(inv.base_id), 0.0)
                return current_sz, None
            if buyable + 1e-9 >= hl_min:
                need = hl_min
            else:
                self._stash_pending_add(
                    inv, accumulated, reason="margin", entry=trader_entry
                )
                console.print(
                    f"[yellow]UPDATE {coin}: size increase queued "
                    f"(need=${accumulated:.2f} < HL min ${hl_min:g}; "
                    f"available=${available:.2f})[/yellow]"
                )
                log_event_throttled(
                    "size_increase_skip",
                    key=f"{coin}:no_margin",
                    coin=coin,
                    reason="no_margin",
                    current_notional=current_notional,
                    target_notional=target_notional,
                    pending_add_notional=accumulated,
                )
                return current_sz, None

        if buyable + 1e-9 < hl_min:
            self._stash_pending_add(inv, need, reason="margin", entry=trader_entry)
            console.print(
                f"[yellow]UPDATE {coin}: size increase queued "
                f"(no free margin; available=${available:.2f}; +${need:.2f})[/yellow]"
            )
            log_event_throttled(
                "size_increase_skip",
                key=f"{coin}:no_margin",
                coin=coin,
                reason="no_margin",
                current_notional=current_notional,
                target_notional=target_notional,
                pending_add_notional=need,
            )
            return current_sz, None

        remainder = 0.0
        delta_notional = need
        if need / lev > available:
            delta_notional = buyable
            remainder = max(0.0, need - delta_notional)
            console.print(
                f"[yellow]UPDATE {coin}: shrunk size-increase notional to "
                f"${delta_notional:.2f} (available margin); "
                f"queued leftover ${remainder:.2f}[/yellow]"
            )

        if not price_is_at_or_better(mid=mid, trader_entry=trader_entry, is_long=is_long):
            self._stash_pending_add(inv, need, reason="price", entry=trader_entry)
            console.print(
                f"[dim]UPDATE {coin}: size increase waiting for entry-or-better "
                f"(mid={mid} trader={trader_entry}) — queued ${need:.2f}[/dim]"
            )
            log_event_throttled(
                "size_increase_skip",
                key=f"{coin}:price_worse",
                coin=coin,
                reason="price_worse",
                mid=mid,
                trader_entry=trader_entry,
                pending_add_notional=need,
            )
            return current_sz, None

        add_sz = self._sz_for_notional(
            trader, coin, delta_notional, mid, hl_min
        )
        if add_sz <= 0:
            return current_sz, None

        cloid = mimic_size_increase_cloid(inv.base_short_id, copy_update=True)
        console.print(
            f"[cyan]LIVE SIZE INCREASE[/cyan] {coin} "
            f"{'LONG' if is_long else 'SHORT'} +sz={add_sz} "
            f"(~${add_sz * mid:.2f} notional; {current_sz} → ~{current_sz + add_sz})"
        )
        logger.info(
            "SIZE INCREASE %s add_sz=%s current=%s entry_size=%s add_ratio=%s "
            "trader_pct=%s final_pct=%s equity=%.4f target_notional=%.4f "
            "delta_notional=%.4f cloid=%s",
            coin,
            add_sz,
            current_sz,
            inv.entry_size,
            meta.get("add_ratio"),
            meta.get("trader_pct"),
            meta.get("final_pct"),
            equity,
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
            self._stash_pending_add(inv, need, reason="price", entry=trader_entry)
            log_event_throttled(
                "size_increase_skip",
                key=f"{coin}:ioc_unfilled",
                coin=coin,
                reason="ioc_unfilled",
                pending_add_notional=need,
            )
            return current_sz, None
        except BudgetDeniedError as exc:
            # Local request-cap gate — same as an HL "too many" reject: keep the
            # leftover so the next drain can send it. Do not raise (that was
            # ERROR_UPDATE and the add vanished).
            self._stash_pending_add(inv, need, reason="ratelimit", entry=trader_entry)
            console.print(
                f"[yellow]UPDATE {coin}: size increase queued "
                f"(ratelimit; +${need:.2f})[/yellow]"
            )
            log_event_throttled(
                "size_increase_skip",
                key=f"{coin}:rate_limited",
                coin=coin,
                reason="rate_limited",
                error=str(exc),
                pending_add_notional=need,
            )
            return current_sz, None
        except HlTraderError as exc:
            msg = str(exc).lower()
            if "insufficient margin" in msg:
                self._stash_pending_add(inv, need, reason="margin", entry=trader_entry)
                log_event_throttled(
                    "size_increase_skip",
                    key=f"{coin}:no_margin",
                    coin=coin,
                    reason="no_margin",
                    error=str(exc),
                    pending_add_notional=need,
                )
                return current_sz, None
            if "too many" in msg and "requests" in msg:
                # HL request cap: keep the add queued so it is not lost until the
                # trader happens to send another UPDATE.
                self._stash_pending_add(inv, need, reason="ratelimit", entry=trader_entry)
                log_event_throttled(
                    "size_increase_skip",
                    key=f"{coin}:rate_limited",
                    coin=coin,
                    reason="rate_limited",
                    error=str(exc),
                    pending_add_notional=need,
                )
                return current_sz, None
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
            leftover = remainder
            if leftover > 1e-9:
                self._stash_pending_add(
                    inv, leftover, reason="margin", entry=trader_entry
                )
            else:
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
            entry_size=inv.entry_size,
            equity=equity,
            target_notional=target_notional,
            **{
                k: v
                for k, v in meta.items()
                if k not in {"margin_usd", "order_notional_usd", "target_notional"}
            },
        )
        return new_sz, {
            "kind": "size_increase",
            "hlOrder": hl_order,
            "nonceMs": nonce,
            "hlResponse": hl_resp,
        }

    def _hold_size_decrease(
        self,
        coin: str,
        *,
        reason: str,
        current_notional: float,
        target_notional: float,
        keep_notional: float,
        sell_notional: float,
    ) -> None:
        msg = (
            f"UPDATE {coin}: size-down held "
            f"(target=${target_notional:.2f} keep=${keep_notional:.2f} "
            f"sell=${sell_notional:.2f}) — wait for CLOSE"
        )
        console.print(f"[yellow]{msg}[/yellow]")
        logger.info(msg)
        log_event(
            "size_decrease_skip",
            coin=coin,
            reason=reason,
            current_notional=current_notional,
            target_notional=target_notional,
            keep_notional=keep_notional,
            sell_notional=sell_notional,
        )

    def _apply_size_decrease(
        self,
        trader: HlTrader,
        *,
        inv: InvestmentUpdate,
        coin: str,
        is_long: bool,
        current_sz: float,
        equity: float,
    ) -> tuple[float, dict[str, Any] | None]:
        """Cut our clip by the same fraction they cut theirs. Never flatten.

        CLOSE is the only path that dumps the copy. If the cut would leave
        (or sell) less than HL's $10 notional, keep the current clip.
        """
        if self.db is not None and inv.base_id is not None:
            self.db.set_pending_add_notional(str(inv.base_id), 0.0)

        leverage = int(inv.leverage or 1)
        mid = trader.mid_price(coin)
        current_notional = float(current_sz) * mid
        ratio = self._relative_reduce_ratio(inv)
        hl_min = float(self.settings.hl_min_order_usd)
        if ratio is None or ratio <= 0:
            self._hold_size_decrease(
                coin,
                reason="hold_until_close",
                current_notional=current_notional,
                target_notional=current_notional,
                keep_notional=current_notional,
                sell_notional=0.0,
            )
            return current_sz, None
        target_notional = max(0.0, current_notional * (1.0 - ratio))
        keep_notional = max(target_notional, hl_min)
        sell_notional = max(0.0, current_notional - keep_notional)
        if sell_notional + 1e-9 < hl_min:
            self._hold_size_decrease(
                coin,
                reason="hold_until_close",
                current_notional=current_notional,
                target_notional=target_notional,
                keep_notional=keep_notional,
                sell_notional=sell_notional,
            )
            return current_sz, None

        sell_sz = sell_notional / mid if mid > 0 else 0.0
        sell_sz = trader.round_size(coin, sell_sz)
        leftover_sz = current_sz - sell_sz
        leftover_notional = leftover_sz * mid if mid > 0 else 0.0
        if (
            sell_sz <= 0
            or leftover_sz <= 1e-12
            or leftover_notional + 1e-9 < hl_min
        ):
            self._hold_size_decrease(
                coin,
                reason="hold_until_close",
                current_notional=current_notional,
                target_notional=target_notional,
                keep_notional=keep_notional,
                sell_notional=sell_sz * mid if mid > 0 else sell_notional,
            )
            return current_sz, None

        cloid = mimic_size_reduce_cloid(inv.base_short_id, copy_update=True)
        console.print(
            f"[cyan]LIVE SIZE DECREASE[/cyan] {coin} "
            f"{'LONG' if is_long else 'SHORT'} -sz={sell_sz} "
            f"(~${sell_sz * mid:.2f} notional; {current_sz} → ~{leftover_sz})"
        )
        logger.info(
            "SIZE DECREASE %s sell_sz=%s current=%s reduce_ratio=%s "
            "target_notional=%.4f keep_notional=%.4f",
            coin,
            sell_sz,
            current_sz,
            ratio,
            target_notional,
            keep_notional,
        )
        hl_order, hl_resp, nonce = trader.close_position(
            coin, sell_sz, is_long, cloid=cloid
        )
        self._check_hl_ok(hl_resp, "size_decrease")

        new_sz = max(0.0, leftover_sz)
        try:
            detail = trader.position_detail(coin)
            if detail is None:
                new_sz = 0.0
            elif detail.get("sz") is not None:
                new_sz = float(detail["sz"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIZE DECREASE fill lookup failed coin=%s: %s", coin, exc)

        if self.db is not None and inv.base_id is not None:
            if new_sz <= 1e-12:
                self.db.close_copy(str(inv.base_id))
                new_sz = 0.0
            else:
                existing = self.db.get_open_copy(str(inv.base_id))
                entry_px = (
                    float(existing.our_entry_px)
                    if existing is not None and existing.our_entry_px
                    else mid
                )
                self.db.set_open_copy_fill(
                    str(inv.base_id),
                    our_entry_px=entry_px,
                    side="long" if is_long else "short",
                    size=new_sz,
                    leverage=float(leverage),
                )

        log_event(
            "size_decrease_hl",
            coin=coin,
            sell_sz=sell_sz,
            qty_before=current_sz,
            qty_after=new_sz,
            flatten=False,
            target_notional=target_notional,
            keep_notional=keep_notional,
            current_notional=current_notional,
            reduce_ratio=ratio,
            cloid=cloid.to_raw() if cloid else None,
            hl_response=hl_resp,
        )
        return new_sz, {
            "kind": "close",
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
    ) -> tuple[dict[str, Any], dict[str, Any], int] | str | None:
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
            if placed is None or placed == TPSL_UNCHANGED:
                return placed
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
        # Grow this copy as leftover GTC fills land. Never shrink here — a
        # later stale 0.6 snapshot must not overwrite 1.1. Net HL from *other*
        # copies is not passed in (adopt only sends this order's held size).
        fill_sz = float(sz)
        existing = self.db.get_open_copy(str(inv.base_id))
        if existing is not None and existing.size is not None:
            fill_sz = max(fill_sz, float(existing.size))
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
        parked_tp: float | None = None,
        parked_sl: float | None = None,
        our_leverage: float | None = None,
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
                    leverage=max(1, int(our_leverage or 1)),
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
            # Not a close: HL still holds this size, so the copy must survive.
            return RecoveryResult(
                status="skipped",
                coin=coin,
                reason="side_mismatch",
                message=msg,
                changed=False,
            )
        if shared:
            if our_size is not None and float(our_size) > 0:
                sz = min(float(our_size), hl_sz)
            else:
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

        lev = max(
            1,
            int(our_leverage or detail.get("leverage") or 1),
        )
        decision = decide_missed_close(
            our_entry=entry,
            mid=mid,
            is_long=is_long,
            trader_entry=inv.entry_price,
            trader_close=inv.closing_price,
            recovery_sl_pct=self.settings.recovery_sl_pct,
            leverage=lev,
        )
        # Already parked: keep the levels we parked at. The SL is measured from
        # the mid, so recomputing it would trail the price and cancel/replace the
        # same pair of triggers on every pass. Rewrite once if this is still the
        # old price-% stop (3% of mid, not 3% ROE).
        if (
            decision.action == "park"
            and parked_tp is not None
            and parked_sl is not None
        ):
            keep_sl = float(parked_sl)
            if not looks_like_unlevered_park_sl(
                keep_sl,
                mid,
                sl_pct=self.settings.recovery_sl_pct,
                leverage=lev,
            ):
                decision = RecoveryDecision(
                    action="park",
                    reason=decision.reason,
                    tp=float(parked_tp),
                    sl=keep_sl,
                    our_pnl_pct=decision.our_pnl_pct,
                    trader_pnl_pct=decision.trader_pnl_pct,
                )
            else:
                decision = RecoveryDecision(
                    action="park",
                    reason=decision.reason,
                    tp=float(parked_tp),
                    sl=decision.sl,
                    our_pnl_pct=decision.our_pnl_pct,
                    trader_pnl_pct=decision.trader_pnl_pct,
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
        parked = trader.place_position_tpsl(
            coin,
            is_long=is_long,
            sz=sz,
            tp_px=decision.tp,
            sl_px=decision.sl,
        )
        if parked == TPSL_UNCHANGED:
            msg = (
                f"RECOVERY parked TP/SL on {coin} reason={decision.reason} "
                f"tp={decision.tp} sl={decision.sl} (already resting) "
                f"our_pnl={_pct(decision.our_pnl_pct)} "
                f"trader_pnl={_pct(decision.trader_pnl_pct)}"
            )
            console.print(f"[dim]{msg}[/dim]")
            logger.info(msg)
            log_event_throttled(
                "recovery_park",
                key=f"{coin}:already_resting",
                coin=coin,
                reason=decision.reason,
                tp=decision.tp,
                sl=decision.sl,
                already_resting=True,
                our_pnl_pct=decision.our_pnl_pct,
                trader_pnl_pct=decision.trader_pnl_pct,
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
                changed=False,
            )
        hl_order, hl_resp, nonce = parked
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("mimic/seen failed baseShortId=%s: %s", base_short_id, exc)

    def refresh_account(self, *, fills_since_ms: int | None = None) -> None:
        """One snapshot + occasional userFees. Info only — no orders."""
        trader = self._ensure_trader()
        trader.refresh_book(fills_since_ms=fills_since_ms)
        budget = self.budget()
        if budget is None:
            return
        age = budget.fees_age_seconds()
        if age is not None and age < float(self.settings.hl_fees_refresh_seconds):
            return
        try:
            fees = trader.fetch_user_fees()
            budget.record_daily_volume(daily_volume_from_user_fees(fees))
        except Exception as exc:  # noqa: BLE001
            logger.warning("userFees refresh failed: %s", exc)

    def place_resting_open(self, action: TradeAction) -> dict[str, Any]:
        """One GTC at the trader's entry. TP/SL wait until the fill is adopted."""
        inv = action.investment
        if inv is None:
            raise HlTraderError("Cannot rest without enriched investment")
        trader = self._ensure_trader()
        coin = self._coin(action)
        is_long = bool(inv.direction_long)
        self._refuse_same_coin_open(
            coin,
            exclude_base_id=str(inv.base_id) if inv.base_id is not None else None,
        )
        leverage = int(inv.leverage or 1)
        equity = trader.account_value()
        margin, size_meta = self._resolve_margin(
            equity=equity,
            leverage=leverage,
            entry_size=inv.entry_size,
            exclude_base_id=str(inv.base_id) if inv.base_id is not None else None,
        )
        available = trader.available_margin()
        if margin > available:
            margin = available * 0.98
            size_meta["order_notional_usd"] = margin * max(1, leverage)
        order_notional = float(size_meta["order_notional_usd"])
        if order_notional + 1e-9 < float(self.settings.hl_min_order_usd):
            raise InsufficientMarginError(
                f"Order notional ${order_notional:.2f} < HL min"
            )
        trader_entry = float(inv.entry_price) if inv.entry_price else None
        if trader_entry is None or trader_entry <= 0:
            raise PriceNotBetterError(f"{coin}: no trader entry to rest at")
        mid = trader.mid_price(coin)
        hl_min = float(self.settings.hl_min_order_usd)
        sz = self._sz_for_notional(trader, coin, order_notional, mid, hl_min)
        order_notional = float(sz) * mid
        if order_notional + 1e-9 < hl_min:
            raise InsufficientMarginError(
                f"Order notional ${order_notional:.2f} < HL min after lot rounding"
            )
        trader.ensure_leverage(coin, leverage, is_cross=True)
        cloid = mimic_entry_cloid(inv.base_short_id)
        _hl_order, hl_resp, _nonce = trader.limit_order(
            coin,
            is_buy=is_long,
            sz=sz,
            limit_px=trader_entry,
            tif="Gtc",
            cloid=cloid,
            essential=False,
        )
        self._check_hl_ok(hl_resp, "resting_open")
        filled_sz, filled_px = extract_fill_sz_px(hl_resp)
        oid = extract_resting_oid(hl_resp)
        log_event(
            "resting_open",
            coin=coin,
            sz=sz,
            px=trader_entry,
            oid=oid,
            cloid=cloid.to_raw() if cloid else None,
            filled_sz=filled_sz,
        )
        return {
            "oid": oid,
            "cloid": cloid.to_raw() if cloid else None,
            "px": trader.round_price(coin, trader_entry),
            "sz": sz,
            "filled_sz": filled_sz,
            "filled_px": filled_px,
            "hl_response": hl_resp,
        }

    def adopt_open_fill(
        self,
        *,
        inv: InvestmentUpdate,
        coin: str,
        is_long: bool,
        sz: float,
        px: float,
        leverage: int,
    ) -> None:
        """Turn a resting (or external) fill into a tracked copy + TP/SL."""
        trader = self._ensure_trader()
        self._persist_open_fill(
            inv=inv,
            coin=coin,
            mid=px,
            sz=sz,
            is_long=is_long,
            leverage=leverage,
        )
        self._sync_tpsl(
            trader,
            coin=coin,
            is_long=is_long,
            sz=sz,
            tp=inv.price_target,
            sl=inv.stop_loss,
            context="open",
            base_short_id=inv.base_short_id,
            copy_update=False,
        )

    def untracked_hl_coins(self) -> list[str]:
        if self.db is None:
            return []
        trader = self._ensure_trader()
        book = trader._ensure_book()
        tracked = {
            (copy.ticker or "").upper()
            for copy in self.db.list_open_copies()
            if copy.ticker
        }
        return sorted(coin for coin in book.positions if coin not in tracked)

    def cancel_resting_oid(self, coin: str, oid: int) -> None:
        self._ensure_trader().cancel_oids(coin, [oid], essential=True)

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
