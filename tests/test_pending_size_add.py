from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from invo_agent.api.models import ActionType, InvestmentUpdate, Notification, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB, PendingIntent
from invo_agent.dex.budget import BudgetDeniedError
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
    db.set_pending_add_notional(str(base_id), 25.0, entry=0.20, reason="margin")
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


def test_size_increase_budget_denied_keeps_add_queued(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="ShortIdH08",
        ticker="BTC",
        portfolio_id=None,
        notification_id="n1",
        side="short",
        size=0.00016,
        leverage=40,
        trader="alpha_trader",
    )
    exe = DexExecutor(settings, db=db)

    def _deny(*args, **kwargs):
        raise BudgetDeniedError("HL budget reserve: remaining=-2323 need=1 reserve=50")

    trader = _FakeTrader(mid=63623.0, available=50.0)
    trader.market_order = _deny  # type: ignore[method-assign]
    sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(
            base_id=base_id, entry_size=1.0, entry_price=63256.0, leverage=40
        ),
        coin="BTC",
        is_long=False,
        current_sz=0.00016,
        equity=100.0,
    )
    assert sub is None
    assert sz == 0.00016
    copy = db.get_open_copy(str(base_id))
    assert copy is not None
    assert copy.pending_add_notional > 0
    assert copy.pending_add_reason == "ratelimit"
    db.close()


def test_queue_update_from_budget_denied(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        hl_min_order_usd=10.0,
    )
    db = AgentDB(settings.db_path)
    base_id = str(uuid4())
    nid = str(uuid4())
    db.upsert_open_copy(
        investment_base_id=base_id,
        base_short_id="ShortIdH08",
        ticker="BTC",
        portfolio_id=None,
        notification_id=nid,
        side="short",
        size=0.00016,
        leverage=40,
        trader="alpha_trader",
    )
    db.set_pending_add_notional(base_id, 22.0, entry=63256.0, reason="ratelimit")
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    notif = Notification.model_validate(
        {
            "id": nid,
            "content": "UPDATE BTC SHORT 40x @alpha_trader ShortIdH08",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": base_id,
            "notifier": {"username": "alpha_trader"},
        }
    )
    action = TradeAction(
        action=ActionType.UPDATE,
        notification=notif,
        investment=_inv(
            base_id=base_id, entry_size=1.0, entry_price=63256.0, leverage=40
        ),
    )
    assert poller._queue_update_from_error(
        nid, action, BudgetDeniedError("HL budget reserve: remaining=-2323")
    )
    assert db.processed_action(nid) == "QUEUED_SIZE"
    copy = db.get_open_copy(base_id)
    assert copy is not None
    assert copy.pending_add_reason == "ratelimit"
    assert copy.pending_add_notional == 22.0
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


def _round_comp(px: float) -> float:
    # COMP: szDecimals=2 → 4 price decimals, 5 significant figures.
    return round(float(f"{px:.5g}"), 4)


def test_flatten_limit_px_clears_comp_dust_short() -> None:
    from invo_agent.dex.pricing import flatten_limit_px

    # Synthetic small short: a narrow IoC limit remains below the order minimum.
    two_pct_mid = 12.0
    assert 0.8 * _round_comp(two_pct_mid * 1.02) + 1e-9 < 10.0
    px = flatten_limit_px(
        mid=two_pct_mid,
        sz=0.8,
        is_buy=True,
        slippage=0.02,
        min_notional=10.0,
        round_price=_round_comp,
    )
    assert 0.8 * px + 1e-9 >= 10.0
    assert px > two_pct_mid * 1.02

    # Fat clip: SDK 5% only, no extra bump.
    fat = flatten_limit_px(
        mid=12.1,
        sz=10.0,
        is_buy=True,
        min_notional=10.0,
        round_price=_round_comp,
    )
    assert fat == _round_comp(12.1 * 1.05)
    assert 10.0 * fat + 1e-9 >= 10.0

    # The synthetic small fill must clear at the default slippage.
    comp = flatten_limit_px(
        mid=12.1,
        sz=0.8,
        is_buy=True,
        min_notional=10.0,
        round_price=_round_comp,
    )
    assert 0.8 * comp + 1e-9 >= 10.0


def test_size_clearing_min_notional_adds_a_lot() -> None:
    from invo_agent.dex.pricing import size_clearing_min_notional

    # ETH: $10 / 1800.0 floors to 0.0052 → $9.91; one lot (0.0001) clears.
    sz = size_clearing_min_notional(
        mid=1800.0, notional=10.0, min_notional=10.0, sz_decimals=4
    )
    assert sz == 0.0056
    assert sz * 1800.0 + 1e-9 >= 10.0
    btc = size_clearing_min_notional(
        mid=60000.0, notional=10.0, min_notional=10.0, sz_decimals=5
    )
    assert btc * 60000.0 + 1e-9 >= 10.0
    exact = size_clearing_min_notional(
        mid=0.20, notional=10.0, min_notional=10.0, sz_decimals=1
    )
    assert exact == 50.0


def test_queued_ready_locally_price_and_margin() -> None:
    from invo_agent.dex.pricing import queued_ready_locally

    assert queued_ready_locally(
        reason="price", mid=0.18, trader_entry=0.20, is_long=True
    )
    assert not queued_ready_locally(
        reason="price", mid=0.22, trader_entry=0.20, is_long=True
    )
    assert not queued_ready_locally(
        reason="margin", available=0.0, leverage=10, hl_min_order_usd=10
    )
    assert queued_ready_locally(
        reason="margin", available=5.0, leverage=10, hl_min_order_usd=10
    )
    # High lev: $0.50 free cash can still clear HL's $10 notional.
    assert queued_ready_locally(
        reason="margin", available=0.50, leverage=40, hl_min_order_usd=10
    )
    assert not queued_ready_locally(
        reason="margin", available=0.20, leverage=20, hl_min_order_usd=10
    )
    # Per-clip need: $18 free funds a $10 bump, not a 20% / $20 open.
    assert queued_ready_locally(
        reason="margin", available=18.0, need_margin=0.50
    )
    assert not queued_ready_locally(
        reason="margin", available=18.0, need_margin=20.0
    )
    assert not queued_ready_locally(
        reason="size", available=0.0, leverage=10, hl_min_order_usd=10
    )
    assert queued_ready_locally(
        reason="size", available=5.0, leverage=10, hl_min_order_usd=10
    )
    assert queued_ready_locally(reason="same_coin", coin_free=True)
    assert not queued_ready_locally(reason="same_coin", coin_free=False)


def test_classify_pending_wait_priority() -> None:
    from invo_agent.dex.pricing import classify_pending_wait

    assert (
        classify_pending_wait(coin_free=False, prior="margin") == "same_coin"
    )
    assert (
        classify_pending_wait(
            coin_free=True,
            mid=0.31,
            trader_entry=0.30,
            is_long=True,
            prior="margin",
        )
        == "price"
    )
    assert (
        classify_pending_wait(
            coin_free=True,
            mid=1906.0,
            trader_entry=1892.0,
            is_long=False,
            available=18.0,
            leverage=25,
            can_spend=False,
            prior="margin",
        )
        == "ratelimit"
    )
    assert classify_pending_wait(coin_free=True, prior="budget") == "ratelimit"
    assert classify_pending_wait(coin_free=True, prior="size") == "margin"
    assert classify_pending_wait(coin_free=True, prior="dust") == "margin"
    # Fat clip needs $20; $18 free → margin even while the request cap is hit.
    assert (
        classify_pending_wait(
            coin_free=True,
            mid=2.0,
            trader_entry=2.1,
            is_long=True,
            available=18.0,
            need_margin=20.0,
            can_spend=False,
            prior="ratelimit",
        )
        == "margin"
    )
    # Same book, 1% bumped to $10 (~$0.50 cash) → ratelimit, not margin.
    assert (
        classify_pending_wait(
            coin_free=True,
            mid=2.0,
            trader_entry=2.1,
            is_long=True,
            available=18.0,
            need_margin=0.50,
            can_spend=False,
            prior="margin",
        )
        == "ratelimit"
    )
    # Tiny clip has cash, but mid is worse → price, not ratelimit.
    assert (
        classify_pending_wait(
            coin_free=True,
            mid=2.2,
            trader_entry=2.0,
            is_long=True,
            available=18.0,
            need_margin=0.50,
            can_spend=False,
            prior="ratelimit",
        )
        == "price"
    )


def test_live_pending_reason_fat_vs_tiny(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        hl_min_order_usd=10.0,
        max_position_pct=10.0,
        max_book_margin_pct=40.0,
    )
    db = AgentDB(settings.db_path)
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller.executor._trader = SimpleNamespace(
        cached_equity=lambda: 100.0,
        cached_available=lambda: 8.0,
        cached_mid=lambda coin: 2.0,
    )
    poller.executor.budget = lambda: SimpleNamespace(  # type: ignore[method-assign]
        can_spend=lambda n, essential=False: False
    )
    # 20% of $100 is $20; per-copy cap is 10% → $10 cash.
    assert poller._need_margin_usd(entry_size=20.0, leverage=10) == 10.0
    # 0.5% of $100 is $5 notional at 10x → bump to $10 → $1 cash.
    assert poller._need_margin_usd(entry_size=0.5, leverage=10) == 1.0
    assert poller._need_margin_usd(
        entry_size=None, leverage=10, leftover_notional=3.0
    ) == 1.0

    now = datetime.now(UTC).isoformat()
    fat = PendingIntent(
        investment_base_id="fat",
        coin="XRP",
        side="long",
        reason="ratelimit",
        created_at=now,
        updated_at=now,
        leverage=10,
        trader_entry=2.1,
        entry_size=20.0,
    )
    tiny = PendingIntent(
        investment_base_id="tiny",
        coin="ETH",
        side="long",
        reason="margin",
        created_at=now,
        updated_at=now,
        leverage=10,
        trader_entry=2.1,
        entry_size=0.5,
    )
    worse = PendingIntent(
        investment_base_id="worse",
        coin="LDO",
        side="long",
        reason="ratelimit",
        created_at=now,
        updated_at=now,
        leverage=10,
        trader_entry=1.8,
        entry_size=0.5,
    )
    assert poller._live_pending_reason(fat) == "margin"
    assert poller._live_pending_reason(tiny) == "ratelimit"
    assert poller._live_pending_reason(worse) == "price"
    db.close()


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
