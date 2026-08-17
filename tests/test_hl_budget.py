from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from invo_agent.api.models import ActionType, Notification
from invo_agent.config import Settings
from invo_agent.db import AgentDB
from invo_agent.dex.budget import (
    INITIAL_BUFFER,
    BudgetDeniedError,
    RequestBudget,
    action_leg_count,
    daily_volume_from_user_fees,
    parse_rate_limit_message,
)
from invo_agent.dex.hl_trader import (
    tpsl_already_resting,
)
from invo_agent.poller import NotificationPoller


def _settings(tmp_path, **extra) -> Settings:
    return Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        **extra,
    )


def test_parse_rate_limit_message() -> None:
    hit = parse_rate_limit_message(
        "Too many cumulative requests sent (39639 > 36699) "
        "for cumulative volume traded $26700.15"
    )
    assert hit is not None
    assert hit.sent == 39639
    assert hit.limit == 36699
    assert hit.volume == 26700.15
    assert parse_rate_limit_message("insufficient margin") is None


def test_daily_volume_and_limit(tmp_path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    budget = RequestBudget(db, reserve=50)
    payload = {
        "dailyUserVlm": [
            {"date": "2026-08-16", "userCross": "100.5", "userAdd": "10"},
            {"date": "2026-08-17", "userCross": "50", "userAdd": "0"},
        ]
    }
    days = daily_volume_from_user_fees(payload)
    budget.record_daily_volume(days)
    assert budget.lifetime_volume() == 160.5
    assert budget.limit() == INITIAL_BUFFER + 160.5
    # Older day kept when the rolling window drops it.
    budget.record_daily_volume({"2026-08-17": 60.0})
    assert budget.lifetime_volume() == 170.5
    db.close()


def test_reanchor_and_rejected_do_not_count(tmp_path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    budget = RequestBudget(db)
    budget.record_daily_volume({"2026-08-17": 100.0})
    hit = parse_rate_limit_message(
        "Too many cumulative requests sent (10150 > 10100) "
        "for cumulative volume traded $100"
    )
    assert hit is not None
    budget.reanchor(hit)
    assert budget.sent() == 10150
    budget.note_posted(2, accepted=False)
    assert budget.sent() == 10150
    budget.note_posted(2, accepted=True)
    assert budget.sent() == 10152
    db.close()


def test_gating_essential_vs_opportunistic(tmp_path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    budget = RequestBudget(db, reserve=50)
    budget.record_daily_volume({"2026-08-17": 10.0})
    # remaining = 10010 - 0
    assert budget.can_spend(1, essential=False)
    budget.db.set_meta("hl_sent", str(budget.limit() - 40))
    assert not budget.can_spend(1, essential=False)
    assert budget.can_spend(1, essential=True)
    drain = RequestBudget(db, reserve=50, allow_deficit=True)
    drain.db.set_meta("hl_sent", str(drain.limit() + 2000))
    assert drain.in_deficit()
    assert drain.can_spend(1, essential=False)
    db.close()


def test_action_leg_count() -> None:
    assert action_leg_count({"type": "updateLeverage"}) == 1
    assert action_leg_count({"type": "order", "orders": [{}, {}]}) == 2
    assert action_leg_count({"type": "cancel", "cancels": [{}, {}, {}]}) == 3


def test_leverage_cache_skips_matching_write() -> None:
    from invo_agent.dex.hl_trader import HlBookSnapshot, HlPosition, HlTrader

    trader = object.__new__(HlTrader)
    trader.leverage_stale_seconds = 3600
    trader._leverage = {"ETH": (10, 10**12)}
    trader._book = HlBookSnapshot(
        mids={"ETH": 1.0},
        available=10.0,
        equity=100.0,
        positions={
            "ETH": HlPosition(
                coin="ETH", sz=1.0, is_long=True, entry_px=1.0, leverage=10.0
            )
        },
        open_orders=[],
        fills=[],
        at=10**12,
    )
    posted: list[int] = []
    trader.update_leverage = lambda coin, lev, is_cross=True: posted.append(lev) or {}
    assert trader.ensure_leverage("ETH", 10) is None
    assert posted == []
    trader.ensure_leverage("ETH", 20)
    assert posted == [20]


def test_tpsl_already_resting_one_leg() -> None:
    assert tpsl_already_resting(
        have_tp=1.0, have_sl=0.9, have_sz=2.0, want_tp=1.0, want_sl=0.9, want_sz=2.0
    )
    assert not tpsl_already_resting(
        have_tp=1.0, have_sl=0.9, have_sz=2.0, want_tp=1.1, want_sl=0.9, want_sz=2.0
    )


def test_supersede_batch_keeps_last_update_and_drops_on_close(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    base = uuid4()
    older = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "u1",
            "notificationType": "user_updated_investment",
            "createdAt": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
            "investmentBaseId": str(base),
        }
    )
    newer = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "u2",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base),
        }
    )
    poller._supersede_batch([older, newer])
    assert db.processed_action(str(older.id)) == "SUPERSEDED_UPDATE"
    assert not db.is_processed(str(newer.id))

    close = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "c",
            "notificationType": "user_sold_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base),
        }
    )
    extra = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "u3",
            "notificationType": "user_updated_investment",
            "createdAt": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            "investmentBaseId": str(base),
        }
    )
    poller._supersede_batch([extra, close])
    assert db.processed_action(str(extra.id)) == "SUPERSEDED_BY_CLOSE"
    assert not db.is_processed(str(close.id))
    db.close()


def test_budget_denied_is_queueable() -> None:
    settings = Settings(
        db_path="/tmp/unused.db",
        session_path="/tmp/unused.json",
        live=False,
        dry_run=True,
    )
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=SimpleNamespace(),
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    action = SimpleNamespace(action=ActionType.OPEN)
    assert (
        poller._queueable_open_reason(action, BudgetDeniedError("reserve"))
        == "ratelimit"
    )
    update = SimpleNamespace(action=ActionType.UPDATE)
    assert (
        poller._queueable_open_reason(update, BudgetDeniedError("reserve"))
        is None
    )


def test_open_skips_leverage_and_counts_order_plus_tpsl(tmp_path) -> None:
    from invo_agent.api.models import InvestmentUpdate, TradeAction
    from invo_agent.dex.executor import DexExecutor

    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=True,
        dry_run=False,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    exe = DexExecutor(settings, db=db)
    posted: list[str] = []

    class _T:
        def account_value(self):
            return 100.0

        def available_margin(self):
            return 80.0

        def mid_price(self, coin):
            return 2.0

        def round_size(self, coin, sz):
            return sz

        def asset_index(self, coin):
            return 1

        def ensure_leverage(self, coin, lev, is_cross=True):
            posted.append("leverage")
            return None

        def market_order(self, *args, **kwargs):
            posted.append("order")
            return (
                {},
                {
                    "status": "ok",
                    "response": {
                        "data": {"statuses": [{"filled": {"avgPx": "2", "totalSz": "5"}}]}
                    },
                },
                1,
            )

        def position_detail(self, coin):
            return {"sz": 5.0, "is_long": True, "entry_px": 2.0, "leverage": 10}

        def sync_position_tpsl(self, *args, **kwargs):
            posted.append("tpsl")
            return {}, {"status": "ok"}, 1

        def sign_action(self, *args, **kwargs):
            return {}

    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_added_investment",
            "createdAt": datetime.now(UTC).isoformat(),
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(uuid4()),
            "baseShortId": "abcdefghij",
            "ticker": "ETH",
            "directionLong": True,
            "leverage": 10,
            "entrySize": 1.0,
            "entryPrice": 2.0,
            "priceTarget": 3.0,
            "stopLoss": 1.0,
        }
    )
    action = TradeAction(action=ActionType.OPEN, notification=notif, investment=inv)
    exe._open(action, _T(), 100.0)  # type: ignore[arg-type]
    assert posted == ["leverage", "order", "tpsl"]
    db.close()


def test_unchanged_update_posts_nothing(tmp_path) -> None:
    from invo_agent.api.models import InvestmentUpdate, TradeAction
    from invo_agent.dex.executor import DexExecutor
    from invo_agent.dex.hl_trader import TPSL_UNCHANGED

    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=True,
        dry_run=False,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="abcdefghij",
        ticker="ETH",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        size=5.0,
        leverage=10,
        trader="x",
    )
    exe = DexExecutor(settings, db=db)
    posted: list[str] = []

    class _T:
        def position_size(self, coin):
            return (5.0, True)

        def ensure_leverage(self, *args, **kwargs):
            posted.append("leverage")
            return None

        def sync_position_tpsl(self, *args, **kwargs):
            posted.append("tpsl")
            return TPSL_UNCHANGED

        def mid_price(self, coin):
            return 2.0

        def asset_index(self, coin):
            return 1

    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base_id),
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "abcdefghij",
            "ticker": "ETH",
            "directionLong": True,
            "leverage": 10,
            "entrySize": 1.0,
            "priceTarget": 3.0,
            "stopLoss": 1.0,
        }
    )
    action = TradeAction(action=ActionType.UPDATE, notification=notif, investment=inv)
    exe._update(action, _T(), 100.0)  # type: ignore[arg-type]
    # Leverage is consulted (cache skip returns None) and TP/SL is checked,
    # but neither writes when they already match.
    assert posted == ["leverage", "tpsl"]
    db.close()


def test_queued_ready_skips_when_no_budget(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    budget = RequestBudget(db, reserve=50)
    budget.record_daily_volume({"2026-08-17": 1.0})
    budget.db.set_meta("hl_sent", str(budget.limit()))
    poller.executor._budget = budget
    assert not poller._should_attempt_queued(
        reason="price", last_tried_at=None, local_ok=True
    )
    db.close()
