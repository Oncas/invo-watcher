from datetime import UTC, datetime
from uuid import uuid4

from invo_agent.api.models import ActionType, InvestmentUpdate, Notification, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB
from invo_agent.engine import CopyEngine


def _action(
    *,
    action: ActionType,
    ticker: str,
    short_id: str,
    base_id,
    trader: str,
    notification_id=None,
) -> TradeAction:
    notif = Notification.model_validate(
        {
            "id": str(notification_id or uuid4()),
            "content": "x",
            "notificationType": (
                "user_updated_investment"
                if action is ActionType.UPDATE
                else "user_sold_investment"
            ),
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base_id),
            "notifier": {"username": trader},
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": short_id,
            "ticker": ticker,
            "directionLong": True,
            "leverage": 5,
            "owner": {"username": trader},
        }
    )
    return TradeAction(action=action, notification=notif, investment=inv)


def test_remember_update_keeps_opener_trader(tmp_path) -> None:
    settings = Settings(db_path=tmp_path / "agent.db")
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="ShortIdB02",
        ticker="KAITO",
        portfolio_id=None,
        notification_id="n1",
        trader="epsilon_trader",
    )
    engine = CopyEngine(client=None, db=db, settings=settings)  # type: ignore[arg-type]
    engine.remember(
        _action(
            action=ActionType.UPDATE,
            ticker="KAITO",
            short_id="ShortIdB02",
            base_id=base_id,
            trader="alpha_trader",
        )
    )
    copy = db.get_open_copy(str(base_id))
    db.close()
    assert copy is not None
    assert copy.trader == "epsilon_trader"
    assert copy.base_short_id == "ShortIdB02"
