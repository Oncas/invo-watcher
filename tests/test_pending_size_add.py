from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from invo_agent.api.models import ActionType, InvestmentUpdate, Notification, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB, PendingIntent
from invo_agent.dex.executor import DexExecutor
from invo_agent.poller import NotificationPoller


class _FakeTrader:
    def __init__(self, *, mid: float, available: float) -> None:
        self._mid = mid
        self._available = available
        self.orders: list[dict] = []

    def mid_price(self, coin: str) -> float:
        return self._mid

    def available_margin(self) -> float:
        return self._available

    def round_size(self, coin: str, sz: float) -> float:
        return sz

    def market_order(self, *args, **kwargs):
        self.orders.append(kwargs)
        return {}, {"status": "ok"}, 1

    def position_detail(self, coin: str):
        return None

    def position_size(self, coin: str):
        return (80.0, True)

    def update_leverage(self, *args, **kwargs):
        self.orders.append({"kind": "leverage", **kwargs})


def _inv(*, base_id, entry_size=40.0, entry_price=0.181, leverage=10):
    return InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "adaShort",
            "ticker": "ADA",
            "directionLong": True,
            "leverage": leverage,
            "entrySize": entry_size,
            "entryPrice": entry_price,
            "changes": {"simIncrease": True, "entrySize": 0.16},
        }
    )


def test_pending_add_drain_does_not_touch_tpsl(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        min_order_usd=1.0,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        size=80.0,
        leverage=10,
        trader="alpha_trader",
    )
    db.set_pending_add_notional(str(base_id), 25.0, entry=0.181, reason="margin")
    exe = DexExecutor(settings, db=db)
    exe._sync_tpsl = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("drain must not sync TP/SL")
    )
    trader = _FakeTrader(mid=0.20, available=0.0)
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "queued UPDATE ADA size +$25.00",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base_id),
            "notifier": {"username": "alpha_trader"},
        }
    )
    action = TradeAction(
        action=ActionType.UPDATE,
        notification=notif,
        investment=_inv(base_id=base_id),
        reason="pending_add_drain",
    )
    exe._update(action, trader, 100.0)  # type: ignore[arg-type]
    assert trader.orders == []
    copy = db.get_open_copy(str(base_id))
    assert copy is not None
    assert copy.pending_add_reason == "margin"
    db.close()


def test_pending_add_drain_records_mimic_after_fill(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=True,
        dry_run=False,
        min_order_usd=1.0,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        size=80.0,
        leverage=10,
        trader="alpha_trader",
    )
    exe = DexExecutor(settings, db=db)
    recorded: list[str] = []
    exe._mimic_copy = lambda **kwargs: recorded.append(kwargs["update_type"]) or {}  # type: ignore[method-assign]
    exe._sync_tpsl = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("drain must not sync TP/SL")
    )
    trader = _FakeTrader(mid=0.20, available=50.0)
    trader.asset_index = lambda coin: 1  # type: ignore[method-assign]
    trader.position_detail = lambda coin: {  # type: ignore[method-assign]
        "sz": 200.0,
        "is_long": True,
        "entry_px": 0.20,
        "leverage": 10,
    }
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "queued UPDATE ADA",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base_id),
            "notifier": {"username": "alpha_trader"},
        }
    )
    action = TradeAction(
        action=ActionType.UPDATE,
        notification=notif,
        investment=_inv(base_id=base_id, entry_size=8.0, entry_price=0.20),
        reason="pending_add_drain",
    )
    exe._update(action, trader, 100.0)  # type: ignore[arg-type]
    assert trader.orders
    assert recorded == ["increase"]
    db.close()


def test_size_increase_rate_limit_keeps_add_queued(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        min_order_usd=1.0,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        size=80.0,
        leverage=10,
        trader="alpha_trader",
    )
    exe = DexExecutor(settings, db=db)
    trader = _FakeTrader(mid=0.20, available=50.0)
    trader.market_order = lambda *args, **kwargs: (  # type: ignore[method-assign]
        {},
        {"status": "Too many cumulative requests sent (35804 > 34878)"},
        1,
    )
    sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(base_id=base_id, entry_size=4.0, entry_price=0.20),
        coin="ADA",
        is_long=True,
        current_sz=80.0,
        equity=100.0,
    )
    assert sub is None
    assert sz == 80.0
    copy = db.get_open_copy(str(base_id))
    assert copy is not None
    assert copy.pending_add_notional > 0
    db.close()


def test_action_from_pending_add_is_drain(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
    )
    db = AgentDB(settings.db_path)
    base_id = str(uuid4())
    db.upsert_open_copy(
        investment_base_id=base_id,
        base_short_id="ShortIdD04",
        ticker="HEMI",
        portfolio_id=None,
        notification_id="n1",
        trader="alpha_trader",
    )
    db.set_pending_add_notional(base_id, 49.0, reason="margin")
    copy = db.get_open_copy(base_id)
    assert copy is not None
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": base_id,
            "baseShortId": "ShortIdD04",
            "ticker": "HEMI",
            "directionLong": False,
            "leverage": 3,
            "entrySize": 0.3,
        }
    )
    action = poller._action_from_pending_add(copy, inv)
    assert action.reason == "pending_add_drain"
    db.close()


def test_size_increase_queues_when_no_margin(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        min_order_usd=1.0,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        size=80.0,
        leverage=10,
        trader="alpha_trader",
    )
    exe = DexExecutor(settings, db=db)
    trader = _FakeTrader(mid=0.20, available=0.4)
    sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(base_id=base_id),
        coin="ADA",
        is_long=True,
        current_sz=80.0,
        equity=100.0,
    )
    assert sz == 80.0
    assert sub is None
    assert trader.orders == []
    copy = db.get_open_copy(str(base_id))
    assert copy is not None
    assert copy.pending_add_notional > 10
    assert copy.pending_add_reason == "margin"
    assert copy.pending_add_entry == 0.181
    db.close()


def test_size_increase_queues_when_price_worse(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        min_order_usd=1.0,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        size=80.0,
        leverage=10,
        trader="alpha_trader",
    )
    exe = DexExecutor(settings, db=db)
    # Plenty of cash, but mid 0.20 is worse than update entry 0.181 on a long.
    trader = _FakeTrader(mid=0.20, available=80.0)
    sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(base_id=base_id),
        coin="ADA",
        is_long=True,
        current_sz=80.0,
        equity=100.0,
    )
    assert sz == 80.0
    assert sub is None
    assert trader.orders == []
    copy = db.get_open_copy(str(base_id))
    assert copy is not None
    assert copy.pending_add_reason == "price"
    assert copy.pending_add_entry == 0.181
    db.close()


def test_expire_pending_adds(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        pending_max_age_seconds=60,
    )
    db = AgentDB(settings.db_path)
    db.upsert_open_copy(
        investment_base_id="ada",
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        trader="alpha_trader",
    )
    db.set_pending_add_notional("ada", 25.0, entry=0.181, reason="margin")
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    db._conn.execute(
        "UPDATE open_copies SET pending_add_at = ? WHERE investment_base_id = ?",
        (stale, "ada"),
    )
    db._conn.commit()
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller._expire_pending_adds()
    copy = db.get_open_copy("ada")
    assert copy is not None
    assert copy.pending_add_notional == 25.0
    db.close()


def test_drain_clears_add_when_trader_closed(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=True,
        dry_run=False,
    )
    db = AgentDB(settings.db_path)
    base_id = str(uuid4())
    db.upsert_open_copy(
        investment_base_id=base_id,
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        trader="alpha_trader",
    )
    db.set_pending_add_notional(base_id, 25.0, entry=0.181, reason="margin")
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": base_id,
            "ticker": "ADA",
            "isOpen": False,
            "closedAt": datetime.now(UTC).isoformat(),
        }
    )
    client = SimpleNamespace(
        get_current_shadow_post_by_investment_base_id=lambda _bid: SimpleNamespace(
            post=SimpleNamespace(update=inv)
        )
    )
    poller = NotificationPoller(
        client=client,
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller._drain_pending_adds()
    copy = db.get_open_copy(base_id)
    assert copy is not None
    assert copy.pending_add_notional == 0.0
    db.close()


def test_refresh_pending_skips_when_already_open(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    now = datetime.now(UTC).isoformat()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        trader="alpha_trader",
    )
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=str(base_id),
            coin="ADA",
            side="long",
            reason="margin",
            created_at=now,
            updated_at=now,
            base_short_id="adaShort",
            summary="OPEN ADA LONG 10x @alpha_trader adaShort",
        )
    )
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": now,
            "investmentBaseId": str(base_id),
            "notifier": {"username": "alpha_trader"},
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "adaShort",
            "ticker": "ADA",
            "directionLong": True,
            "leverage": 10,
            "entryPrice": 0.181,
            "owner": {"username": "alpha_trader"},
        }
    )
    action = TradeAction(action=ActionType.UPDATE, notification=notif, investment=inv)
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    assert poller._refresh_pending_from_update(action) is False
    db.close()


def test_queued_ready_locally_price_and_margin() -> None:
    from invo_agent.dex.pricing import queued_ready_locally

    assert queued_ready_locally(
        reason="price", mid=0.18, trader_entry=0.20, is_long=True
    )
    assert not queued_ready_locally(
        reason="price", mid=0.22, trader_entry=0.20, is_long=True
    )
    assert not queued_ready_locally(
        reason="margin", available=0.0, leverage=10, hl_min_order_usd=10, min_order_usd=1
    )
    assert queued_ready_locally(
        reason="margin", available=5.0, leverage=10, hl_min_order_usd=10, min_order_usd=1
    )
    assert not queued_ready_locally(
        reason="size", leftover_notional=4.0, hl_min_order_usd=10
    )
    assert queued_ready_locally(
        reason="size", leftover_notional=12.0, hl_min_order_usd=10
    )
    assert queued_ready_locally(reason="same_coin", coin_free=True)
    assert not queued_ready_locally(reason="same_coin", coin_free=False)


def test_should_attempt_queued_local_or_fallback(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
    )
    db = AgentDB(settings.db_path)
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    recent = datetime.now(UTC).isoformat()
    # Fillable per the cached book, but not on every 4s poll.
    assert not poller._should_attempt_queued(
        reason="margin", last_tried_at=recent, local_ok=True
    )
    assert poller._should_attempt_queued(
        reason="margin", last_tried_at=None, local_ok=True
    )
    gap_passed = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    assert poller._should_attempt_queued(
        reason="margin", last_tried_at=gap_passed, local_ok=True
    )
    assert not poller._should_attempt_queued(
        reason="margin", last_tried_at=recent, local_ok=False
    )
    assert not poller._should_attempt_queued(
        reason="margin", last_tried_at=gap_passed, local_ok=False
    )
    stale = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    assert poller._should_attempt_queued(
        reason="margin", last_tried_at=stale, local_ok=False
    )
    db.close()
