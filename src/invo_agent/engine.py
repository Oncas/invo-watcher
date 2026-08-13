from __future__ import annotations

from invo_agent.api.client import InvolioClient
from invo_agent.api.models import ActionType, Notification, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB
from invo_agent.labels import trader_from_action


class CopyEngine:
    """Maps notifications → trade actions and tracks idempotency."""

    def __init__(self, client: InvolioClient, db: AgentDB, settings: Settings):
        self.client = client
        self.db = db
        self.settings = settings

    def should_consider(self, notification: Notification) -> bool:
        if notification.action_type is ActionType.IGNORE:
            return False
        allow = self.settings.portfolio_ids
        if not allow:
            return True
        if notification.portfolio_id is None:
            return False
        return str(notification.portfolio_id) in allow

    def enrich(self, notification: Notification) -> TradeAction:
        investment = None
        reason = None
        if notification.post_id is not None:
            try:
                post_resp = self.client.get_post_by_id(notification.post_id)
                if post_resp.post and post_resp.post.update:
                    investment = post_resp.post.update
                else:
                    reason = "post has no investment update"
            except Exception as exc:  # noqa: BLE001 — keep loop alive in dry-run
                reason = f"enrich failed: {exc}"

        return TradeAction(
            action=notification.action_type,
            notification=notification,
            investment=investment,
            reason=reason,
        )

    def remember(self, action: TradeAction) -> None:
        notif = action.notification
        self.db.mark_processed(str(notif.id), action.action.value, action.summary)

        base_id = None
        if action.investment and action.investment.base_id:
            base_id = str(action.investment.base_id)
        elif notif.investment_base_id:
            base_id = str(notif.investment_base_id)

        if not base_id:
            return

        if action.action is ActionType.CLOSE:
            self.db.close_copy(base_id)
            return

        if action.action is ActionType.OPEN:
            inv = action.investment
            self.db.upsert_open_copy(
                investment_base_id=base_id,
                base_short_id=inv.base_short_id if inv else None,
                ticker=(inv.ticker or inv.name) if inv else None,
                portfolio_id=str(notif.portfolio_id) if notif.portfolio_id else None,
                notification_id=str(notif.id),
                trader=trader_from_action(action),
            )
            return

        if action.action is ActionType.UPDATE:
            # Never create a phantom open_copy from a skipped UPDATE (not ours).
            # Only refresh metadata when we already track this investment.
            existing = self.db.get_open_copy(base_id)
            if existing is None and action.investment and action.investment.base_short_id:
                existing = self.db.get_open_copy_by_short_id(
                    action.investment.base_short_id
                )
            if existing is None:
                return
            inv = action.investment
            self.db.upsert_open_copy(
                investment_base_id=existing.investment_base_id,
                base_short_id=inv.base_short_id if inv else existing.base_short_id,
                ticker=(inv.ticker or inv.name) if inv else existing.ticker,
                portfolio_id=str(notif.portfolio_id)
                if notif.portfolio_id
                else existing.portfolio_id,
                notification_id=str(notif.id),
                trader=trader_from_action(action),
            )
