from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from invo_agent.api.models import ActionType, InvestmentUpdate, Notification, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB, PendingIntent
from invo_agent.dex.executor import DexExecutor, RecoveryResult
from invo_agent.dex.hl_trader import (
    GhostCopyError,
    HlTraderError,
    RecoveryParkedError,
    SideMismatchError,
    tpsl_already_resting,
)
from invo_agent.engine import CopyEngine
from invo_agent.labels import format_recent_line, update_labels
from invo_agent.poller import NotificationPoller


class _FakeTrader:
    def __init__(
        self,
        *,
        mid: float,
        available: float,
        pos: tuple[float, bool] | None = None,
        coins: set[str] | None = None,
    ) -> None:
        self._mid = mid
        self._available = available
        self._pos = pos
        self._coins = coins if coins is not None else (set() if pos is None else {"ADA"})
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

    def close_position(self, coin, sz, is_long, **kwargs):
        return self.market_order(
            coin, is_buy=not is_long, sz=sz, reduce_only=True, **kwargs
        )

    def position_detail(self, coin: str):
        if self._pos is None:
            return None
        sz, is_long = self._pos
        return {"sz": sz, "is_long": is_long, "entry_px": self._mid}

    def position_size(self, coin: str):
        return self._pos

    def open_coins(self) -> set[str]:
        return set(self._coins)


def _settings(tmp_path, **extra) -> Settings:
    return Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        hl_min_order_usd=10.0,
        **extra,
    )


def _cut_inv(*, base_id, entry_size=0.1, prev=0.4, leverage=10):
    return InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "adaShort",
            "ticker": "ADA",
            "directionLong": True,
            "leverage": leverage,
            "entrySize": entry_size,
            "entryPrice": 0.20,
            "changes": {"simIncrease": False, "entrySize": prev},
        }
    )


def test_entry_size_is_percent_not_fraction(tmp_path) -> None:
    settings = _settings(tmp_path)
    exe = DexExecutor(settings)
    assert exe._trader_allocation_pct(0.801) == 0.801
    assert exe._trader_allocation_pct(0.10) == 0.10
    assert exe._trader_allocation_pct(1.0) == 1.0
    assert exe._trader_allocation_pct(12.54) == 12.54
    assert exe._trader_allocation_pct(101.0) is None
    _margin, meta = exe._resolve_margin(equity=100.0, leverage=5, entry_size=0.801)
    assert meta["trader_pct"] == 0.801
    assert meta["bumped_to_hl_min"] is True
    assert meta["order_notional_usd"] == 10.0
    assert meta["capped"] is False
    tiny, tiny_meta = exe._resolve_margin(equity=100.0, leverage=20, entry_size=0.1)
    assert tiny == 0.5
    assert tiny_meta["order_notional_usd"] == 10.0


def test_size_cut_reduces_and_cancels_pending(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        size=200.0,
        leverage=10,
        trader="alpha_trader",
    )
    db.set_pending_add_notional(str(base_id), 25.0, reason="margin")
    exe = DexExecutor(settings, db=db)
    trader = _FakeTrader(mid=0.20, available=80.0, pos=(200.0, True))
    # size 600, mid 0.20 → $120. entrySize 10 = 10% → target $100. sell $20.
    db.set_open_copy_fill(
        str(base_id), our_entry_px=0.20, side="long", size=600.0, leverage=10
    )
    sz, sub = exe._apply_size_decrease(
        trader,  # type: ignore[arg-type]
        inv=_cut_inv(base_id=base_id, entry_size=10.0, prev=40.0),
        coin="ADA",
        is_long=True,
        current_sz=600.0,
        equity=100.0,
    )
    assert sub is not None
    assert trader.orders
    assert trader.orders[0]["reduce_only"] is True
    assert sz < 600.0
    copy = db.get_open_copy(str(base_id))
    assert copy is not None
    assert copy.pending_add_notional == 0.0
    db.close()


def test_size_cut_below_hl_min_flattens(tmp_path) -> None:
    settings = _settings(tmp_path)
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
    trader = _FakeTrader(mid=0.20, available=80.0, pos=None, coins=set())
    # entry 0.5 → 0.5% of 100 * 10 lev = $5 < $10 → flatten
    sz, sub = exe._apply_size_decrease(
        trader,  # type: ignore[arg-type]
        inv=_cut_inv(base_id=base_id, entry_size=0.5, prev=40.0),
        coin="ADA",
        is_long=True,
        current_sz=80.0,
        equity=100.0,
    )
    assert sz == 0.0
    assert sub is not None
    assert db.get_open_copy(str(base_id)) is None
    db.close()


def test_size_add_dust_leftover_stays(tmp_path) -> None:
    settings = _settings(tmp_path)
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
    # current 16, target 28, need 12; available 1.05 → buyable ~10.29; leftover ~1.71 dust
    trader = _FakeTrader(mid=0.20, available=1.05)
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "adaShort",
            "ticker": "ADA",
            "directionLong": True,
            "leverage": 10,
            "entrySize": 2.8,
            "entryPrice": 0.20,
            "changes": {"simIncrease": True, "entrySize": 0.16},
        }
    )
    sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=inv,
        coin="ADA",
        is_long=True,
        current_sz=80.0,
        equity=100.0,
    )
    assert sub is not None
    copy = db.get_open_copy(str(base_id))
    assert copy is not None
    assert copy.pending_add_reason == "margin"
    assert 0 < copy.pending_add_notional < 10
    db.close()


def test_ghost_update_clears_copy(tmp_path) -> None:
    settings = _settings(tmp_path)
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
        trader="alpha_trader",
    )
    exe = DexExecutor(settings, db=db)
    trader = _FakeTrader(mid=0.20, available=80.0, pos=None, coins=set())
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base_id),
            "notifier": {"username": "alpha_trader"},
        }
    )
    inv = _cut_inv(base_id=base_id)
    action = TradeAction(action=ActionType.UPDATE, notification=notif, investment=inv)
    with pytest.raises(GhostCopyError):
        exe._update(action, trader, 100.0)  # type: ignore[arg-type]
    assert db.get_open_copy(str(base_id)) is None
    db.close()


def test_ghost_reconcile_frees_coin(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    db.upsert_open_copy(
        investment_base_id="ghost-1",
        base_short_id="ghostShort",
        ticker="ETH",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        trader="x",
    )
    exe = DexExecutor(settings, db=db)
    trader = _FakeTrader(mid=1.0, available=10.0, pos=None, coins=set())
    cleared = exe.reconcile_ghost_copies(trader)  # type: ignore[arg-type]
    assert "ETH" in cleared
    assert db.get_open_copy("ghost-1") is None
    db.close()


def test_close_side_mismatch_keeps_copy(tmp_path) -> None:
    settings = _settings(tmp_path)
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
        trader="alpha_trader",
    )
    exe = DexExecutor(settings, db=db)
    trader = _FakeTrader(mid=0.20, available=80.0, pos=(80.0, False), coins={"ADA"})
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_sold_investment",
            "createdAt": datetime.now(UTC).isoformat(),
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
        }
    )
    action = TradeAction(action=ActionType.CLOSE, notification=notif, investment=inv)
    with pytest.raises(SideMismatchError):
        exe._close(action, trader)  # type: ignore[arg-type]
    assert db.get_open_copy(str(base_id)) is not None
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller._record_executor_error(str(notif.id), action, SideMismatchError("x"))
    assert db.get_open_copy(str(base_id)) is not None
    assert db.is_processed(str(notif.id))
    db.close()


def _rate_limit_exc() -> HlTraderError:
    return HlTraderError(
        "HL close not ok: Too many cumulative requests sent (35804 > 34878)"
    )


def _close_action(*, created: datetime) -> tuple[Notification, TradeAction]:
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_sold_investment",
            "createdAt": created.isoformat(),
            "investmentBaseId": str(uuid4()),
            "notifier": {"username": "delta_trader"},
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(notif.investment_base_id),
            "baseShortId": "ShortIdF06",
            "ticker": "UNI",
            "directionLong": False,
        }
    )
    return notif, TradeAction(
        action=ActionType.CLOSE, notification=notif, investment=inv
    )


def test_rate_limit_close_is_not_marked_processed(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    notif, action = _close_action(created=datetime.now(UTC))
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller._record_executor_error(str(notif.id), action, _rate_limit_exc())
    assert not db.is_processed(str(notif.id))
    db.close()


def test_stale_rate_limit_close_is_marked_processed(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    notif, action = _close_action(created=datetime.now(UTC) - timedelta(hours=1))
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller._record_executor_error(str(notif.id), action, _rate_limit_exc())
    assert db.is_processed(str(notif.id))
    db.close()


def test_rate_limit_update_is_marked_processed(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(uuid4()),
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(notif.investment_base_id),
            "baseShortId": "ShortIdD04",
            "ticker": "HEMI",
            "directionLong": False,
        }
    )
    action = TradeAction(action=ActionType.UPDATE, notification=notif, investment=inv)
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller._record_executor_error(str(notif.id), action, _rate_limit_exc())
    assert db.is_processed(str(notif.id))
    db.close()


def _poller(settings, db) -> NotificationPoller:
    return NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]


def test_transient_close_retries_then_hands_off_to_recovery(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    poller = _poller(settings, db)
    blip = HlTraderError("HL close failed: connect error / read timeout")

    fresh_notif, fresh = _close_action(created=datetime.now(UTC))
    poller._record_executor_error(str(fresh_notif.id), fresh, blip)
    assert not db.is_processed(str(fresh_notif.id))
    assert poller._force_missed_close is False

    old_notif, old = _close_action(created=datetime.now(UTC) - timedelta(minutes=30))
    poller._record_executor_error(str(old_notif.id), old, blip)
    assert db.is_processed(str(old_notif.id))
    assert poller._force_missed_close is True
    db.close()


def test_close_is_never_filed_as_no_margin(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    poller = _poller(settings, db)
    notif, action = _close_action(created=datetime.now(UTC))
    poller._record_executor_error(
        str(notif.id), action, HlTraderError("HL close not ok: Insufficient margin")
    )
    assert db.processed_action(str(notif.id)) == "ERROR_CLOSE"
    assert poller._force_missed_close is True
    db.close()


def test_parked_copy_close_keeps_park(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="ShortIdF06",
        ticker="UNI",
        portfolio_id=None,
        notification_id="n1",
        side="short",
        size=22.0,
        trader="delta_trader",
    )
    db.set_recovery_state(
        str(base_id), pending=True, recovery_tp=3.0, recovery_sl=3.2
    )
    exe = DexExecutor(settings, db=db)
    trader = _FakeTrader(mid=3.30, available=10.0, pos=(22.0, False), coins={"UNI"})
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_sold_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base_id),
            "notifier": {"username": "delta_trader"},
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "ShortIdF06",
            "ticker": "UNI",
            "directionLong": False,
        }
    )
    action = TradeAction(action=ActionType.CLOSE, notification=notif, investment=inv)
    with pytest.raises(RecoveryParkedError):
        exe._close(action, trader)  # type: ignore[arg-type]
    assert trader.orders == []
    poller = _poller(settings, db)
    poller._record_executor_error(str(notif.id), action, RecoveryParkedError("x"))
    assert db.processed_action(str(notif.id)) == "SKIP_PARKED_CLOSE"
    copy = db.get_open_copy(str(base_id))
    assert copy is not None and copy.recovery_pending
    db.close()


def test_reparking_keeps_original_levels_and_stays_quiet(tmp_path) -> None:
    """A parked copy must not re-place a trailing SL on every reconcile."""
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=True,
        dry_run=False,
        hl_min_order_usd=10.0,
        recovery_sl_pct=3.0,
    )
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="ShortIdF06",
        ticker="UNI",
        portfolio_id=None,
        notification_id="n1",
        side="short",
        size=22.0,
        trader="delta_trader",
    )
    db.set_open_copy_fill(
        str(base_id), our_entry_px=3.0, side="short", size=22.0, leverage=10
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "ShortIdF06",
            "ticker": "UNI",
            "directionLong": False,
            "isOpen": False,
            "closingPrice": 3.1,
            "entryPrice": 3.15,
        }
    )
    exe = DexExecutor(settings, client=SimpleNamespace(), db=db)  # type: ignore[arg-type]
    placed: list[tuple[float, float]] = []

    class _ParkTrader(_FakeTrader):
        def place_position_tpsl(self, coin, *, is_long, sz, tp_px, sl_px):
            placed.append((tp_px, sl_px))
            return {}, {"status": "ok"}, 1

    trader = _ParkTrader(mid=3.27, available=10.0, pos=(22.0, False), coins={"UNI"})
    exe._ensure_trader = lambda: trader  # type: ignore[method-assign,assignment]
    exe._record_recovery_close = lambda **kwargs: None  # type: ignore[method-assign]

    first = exe.recover_missed_close(
        coin="UNI",
        inv=inv,
        our_entry_px=3.0,
        our_side="short",
        our_size=22.0,
        our_leverage=10,
    )
    assert first.status == "parked"
    assert first.changed is True
    assert len(placed) == 1

    # Price drifted; a second pass must reuse the stored levels, not trail the SL.
    trader._mid = 3.25
    second = exe.recover_missed_close(
        coin="UNI",
        inv=inv,
        our_entry_px=3.0,
        our_side="short",
        our_size=22.0,
        parked_tp=first.tp,
        parked_sl=first.sl,
        our_leverage=10,
    )
    assert second.tp == first.tp
    assert second.sl == first.sl
    assert placed[1] == placed[0]
    db.close()


def test_rate_limit_pauses_queue_drains(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=str(uuid4()),
            coin="KBONK",
            side="long",
            reason="margin",
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
    )
    poller = _poller(settings, db)
    poller._ensure_book_snapshot = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("must not touch HL during cooldown")
    )
    assert not poller._hl_cooling_down()
    poller._note_hl_rate_limit(_rate_limit_exc())
    assert poller._hl_cooling_down()
    poller._drain_pending()
    poller._drain_pending_adds()
    db.close()


def test_late_close_routes_to_recovery(tmp_path) -> None:
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
        base_short_id="ShortIdF06",
        ticker="UNI",
        portfolio_id=None,
        notification_id="n1",
        side="short",
        size=22.0,
        trader="delta_trader",
    )
    poller = _poller(settings, db)
    seen: list[str] = []

    def _fake_reconcile(copy):
        seen.append(copy.investment_base_id)
        return RecoveryResult(
            status="parked",
            coin="UNI",
            reason="losing_trader_green_or_unknown",
            message="RECOVERY parked TP/SL on UNI",
            tp=3.0,
            sl=3.2,
        )

    poller._reconcile_one_copy = _fake_reconcile  # type: ignore[method-assign]

    fresh_notif, fresh = _close_action(created=datetime.now(UTC))
    assert poller._route_late_close(str(fresh_notif.id), fresh) is False
    assert seen == []

    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_sold_investment",
            "createdAt": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "investmentBaseId": str(base_id),
            "notifier": {"username": "delta_trader"},
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "ShortIdF06",
            "ticker": "UNI",
            "directionLong": False,
        }
    )
    action = TradeAction(action=ActionType.CLOSE, notification=notif, investment=inv)
    assert poller._route_late_close(str(notif.id), action) is True
    assert seen == [str(base_id)]
    assert db.processed_action(str(notif.id)) == "CLOSE_LATE_RECOVERY"
    db.close()


def test_migrate_unmarks_only_fresh_rate_limited_close(tmp_path) -> None:
    from invo_agent.migrate import migrate_state

    settings = _settings(tmp_path, log_dir=tmp_path / "logs")
    db = AgentDB(settings.db_path)
    fresh = str(uuid4())
    stale = str(uuid4())
    other = str(uuid4())
    db.mark_processed(
        fresh,
        "ERROR_CLOSE",
        "HL close not ok: Too many cumulative requests sent (35804 > 34878)",
    )
    db.mark_processed(
        stale,
        "ERROR_CLOSE",
        "HL close not ok: Too many cumulative requests sent (35804 > 34878)",
    )
    db._conn.execute(
        "UPDATE processed_notifications SET processed_at = ? WHERE notification_id = ?",
        ((datetime.now(UTC) - timedelta(hours=6)).isoformat(), stale),
    )
    db._conn.commit()
    db.mark_processed(other, "ERROR_CLOSE", "HL close not ok: Insufficient margin")
    db.close()
    stats = migrate_state(settings)
    assert stats["rate_limited_retried"] == 1
    db = AgentDB(settings.db_path)
    assert not db.is_processed(fresh)
    assert db.is_processed(stale)
    assert db.is_processed(other)
    db.close()


def test_tpsl_already_resting_matches_same_levels() -> None:
    assert tpsl_already_resting(
        have_tp=0.01,
        have_sl=0.02,
        have_sz=100.0,
        want_tp=0.01,
        want_sl=0.02,
        want_sz=100.0,
    )
    assert not tpsl_already_resting(
        have_tp=0.01,
        have_sl=0.02,
        have_sz=100.0,
        want_tp=0.011,
        want_sl=0.02,
        want_sz=100.0,
    )
    assert not tpsl_already_resting(
        have_tp=0.01,
        have_sl=0.02,
        have_sz=100.0,
        want_tp=0.01,
        want_sl=0.02,
        want_sz=150.0,
    )


def test_enrich_retry_then_timeout(tmp_path) -> None:
    settings = _settings(tmp_path, pending_max_age_seconds=60)
    db = AgentDB(settings.db_path)
    fresh = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(uuid4()),
        }
    )
    engine = CopyEngine(client=SimpleNamespace(), db=db, settings=settings)
    engine.enrich = lambda _n: TradeAction(  # type: ignore[method-assign]
        action=ActionType.UPDATE, notification=fresh, investment=None, reason="enrich failed"
    )
    poller = NotificationPoller(
        client=SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller.engine = engine
    assert poller._prepare_action(fresh) is None
    assert not db.is_processed(str(fresh.id))

    old = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
            "investmentBaseId": str(uuid4()),
        }
    )
    engine.enrich = lambda _n: TradeAction(  # type: ignore[method-assign]
        action=ActionType.UPDATE, notification=old, investment=None, reason="enrich failed"
    )
    assert poller._prepare_action(old) is None
    assert db.is_processed(str(old.id))
    db.close()


def test_close_stub_from_copy(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    base_id = uuid4()
    db.upsert_open_copy(
        investment_base_id=str(base_id),
        base_short_id="adaShort",
        ticker="ADA",
        portfolio_id=None,
        notification_id="n1",
        side="long",
        trader="alpha_trader",
    )
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_sold_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base_id),
        }
    )
    engine = CopyEngine(client=SimpleNamespace(), db=db, settings=settings)
    stub = engine.stub_close_from_copy(notif)
    assert stub is not None
    assert stub.investment is not None
    assert stub.investment.ticker == "ADA"
    db.close()


def test_queued_open_kept_when_shadow_still_open(tmp_path) -> None:
    settings = _settings(tmp_path, pending_max_age_seconds=60)
    db = AgentDB(settings.db_path)
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    from invo_agent.db import PendingIntent

    base_id = str(uuid4())
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=base_id,
            coin="DOT",
            side="long",
            reason="margin",
            created_at=stale,
            updated_at=stale,
            summary="OPEN DOT LONG 10x @gamma_trader Rq48",
        )
    )
    inv = InvestmentUpdate.model_validate(
        {"baseId": base_id, "ticker": "DOT", "isOpen": True}
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
    poller._expire_pending()
    assert db.list_pending_intents()
    db.close()


def test_recent_line_size_up_vs_down() -> None:
    up = InvestmentUpdate.model_validate(
        {"ticker": "ETH", "entrySize": 2.0, "changes": {"simIncrease": True, "entrySize": 1.0}}
    )
    down = InvestmentUpdate.model_validate(
        {
            "ticker": "ETH",
            "entrySize": 1.0,
            "changes": {"simIncrease": False, "entrySize": 2.0},
        }
    )
    assert update_labels(up) == ["size↑"]
    assert update_labels(down) == ["size↓"]
    assert format_recent_line(
        "UPDATE", "UPDATE ETH LONG 20x @beta_trader DG07 · size↑", ["size↑"]
    ).startswith("UPDATE · size↑:")
    assert format_recent_line(
        "UPDATE", "UPDATE ETH LONG 20x @beta_trader DG07 · size↓", ["size↓"]
    ).startswith("UPDATE · size↓:")


def test_catchup_leaves_update_on_open_copy(tmp_path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    settings = Settings()
    base_id = str(uuid4())
    nid = str(uuid4())
    db.upsert_open_copy(
        investment_base_id=base_id,
        base_short_id="ShortIdL12",
        ticker="2Z",
        portfolio_id=None,
        notification_id=nid,
        trader="zeta_trader",
    )
    db.mark_processed(nid, "CATCHUP_UPDATE", "zeta_trader updated 2Z")
    notif = Notification.model_validate(
        {
            "id": nid,
            "content": "zeta_trader updated their investment in 2Z",
            "notificationType": "user_updated_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": base_id,
        }
    )
    client = SimpleNamespace(
        get_notifications=lambda page=1, size=None: SimpleNamespace(items=[notif])
    )
    poller = NotificationPoller(
        client=client,
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    assert poller._tracked_followup(notif) is True
    poller.catch_up(pages=1)
    assert db.is_processed(nid) is False
    db.close()
