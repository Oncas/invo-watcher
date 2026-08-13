from datetime import UTC, datetime
from uuid import uuid4

from invo_agent.api.models import ActionType, InvestmentUpdate, Notification, TradeAction
from invo_agent.labels import trader_from_action, trader_from_summary


def test_trader_from_summary() -> None:
    assert trader_from_summary("OPEN ETH LONG 20x @beta_trader ShortIdN14") == (
        "beta_trader"
    )
    assert trader_from_summary("OPEN BTC LONG 40x ShortIdJ10") is None
    assert trader_from_summary(None) is None


def test_trader_from_action_prefers_owner() -> None:
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "open",
            "notificationType": "user_added_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "notifier": {"username": "notifier_user"},
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "ticker": "ETH",
            "directionLong": True,
            "leverage": 20,
            "owner": {"username": "beta_trader"},
        }
    )
    action = TradeAction(action=ActionType.OPEN, notification=notif, investment=inv)
    assert trader_from_action(action) == "beta_trader"
    assert "@beta_trader" in action.summary


def test_trader_from_action_falls_back_to_notifier() -> None:
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "open",
            "notificationType": "user_added_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "notifier": {"username": "gamma_trader"},
        }
    )
    action = TradeAction(action=ActionType.OPEN, notification=notif)
    assert trader_from_action(action) == "gamma_trader"
