from datetime import UTC, datetime
from uuid import uuid4

from invo_agent.api.models import ActionType, InvestmentUpdate, Notification, TradeAction
from invo_agent.labels import (
    coin_from_summary,
    format_action_summary,
    format_recent_line,
    own_copy_from_summary,
    tags_from_action_row,
    tags_from_event,
    tags_from_summary,
    trader_from_action,
    trader_from_summary,
    update_labels,
)


def test_trader_from_summary() -> None:
    assert trader_from_summary("OPEN ETH LONG 20x @beta_trader ShortIdN14") == (
        "beta_trader"
    )
    assert trader_from_summary("OPEN BTC LONG 40x ShortIdJ10") is None
    assert trader_from_summary(None) is None


def test_own_copy_from_summary_ignores_via_mention() -> None:
    assert own_copy_from_summary("OPEN KAITO LONG 5x @epsilon_trader ShortIdB02") == (
        "epsilon_trader",
        "ShortIdB02",
    )
    assert own_copy_from_summary(
        "OPEN KAITO LONG 5x @alpha_trader ShortIdI09: Already mimicking KAITO "
        "via ShortIdB02 (long); skip second same-coin open"
    ) == ("alpha_trader", "ShortIdI09")
    assert own_copy_from_summary("OPEN BTC LONG 40x ShortIdJ10") is None


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


def test_update_labels_from_changes() -> None:
    size_down = InvestmentUpdate.model_validate(
        {
            "ticker": "ETH",
            "entrySize": 1.0,
            "changes": {
                "entrySize": 2.0,
                "simIncrease": False,
                "simDifference": 10.2,
            },
        }
    )
    assert update_labels(size_down) == ["size"]
    tp_sl = InvestmentUpdate.model_validate(
        {
            "ticker": "ADA",
            "priceTarget": 0.2,
            "stopLoss": 0.15,
            "changes": {"priceTarget": 0.18, "stopLoss": None},
        }
    )
    assert update_labels(tp_sl) == ["tp", "sl"]
    lev = InvestmentUpdate.model_validate(
        {"ticker": "BTC", "leverage": 40, "changes": {"leverage": 30}}
    )
    assert update_labels(lev) == ["lev"]
    assert update_labels(None) == []


def test_format_recent_line_keeps_tags_visible() -> None:
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "notifier": {"username": "beta_trader"},
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "ticker": "ETH",
            "directionLong": True,
            "leverage": 20,
            "baseShortId": "ShortIdG07",
            "owner": {"username": "beta_trader"},
            "entrySize": 1.0,
            "changes": {"simIncrease": True, "entrySize": 0.4, "priceTarget": 2000},
        }
    )
    action = TradeAction(action=ActionType.UPDATE, notification=notif, investment=inv)
    summary = format_action_summary(action)
    assert summary.endswith(" · size · tp")
    assert tags_from_summary(summary) == ["size", "tp"]
    line = format_recent_line(action.action.value, summary)
    assert line.startswith("UPDATE · size · tp:")
    assert "ETH LONG 20x @beta_trader" in line


def test_tags_from_action_row_and_events() -> None:
    assert coin_from_summary("UPDATE ETH LONG 20x @beta_trader ShortIdG07") == "ETH"
    assert tags_from_action_row(
        "UPDATE", "UPDATE ETH LONG 20x @beta_trader ShortIdG07"
    ) == []
    assert tags_from_action_row(
        "SKIP_NO_MARGIN_UPDATE",
        "UPDATE BTC LONG 30x @gamma_trader: UPDATE BTC size increase: no free margin",
    ) == ["size"]
    assert tags_from_action_row("QUEUED_SIZE", "UPDATE ADA LONG 10x @x") == ["size"]
    assert tags_from_event("update_recorded", "tp") == ["tp"]
    assert tags_from_event("size_increase_skip", None) == ["size"]
    assert tags_from_event("update_recorded", "increase") == ["size"]


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
