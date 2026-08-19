"""Caps, queue-lock, and expire.

Regressions these lock down, all on a synthetic $100 book:
- a 20x open at 0.1% whose UPDATEs chase the trader's % into a whale clip
- an intent left queued for a day, then opened at the stale fat size
- four names stacking whale notionals onto the same book at once
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from invo_agent.api.models import ActionType, InvestmentUpdate, Notification, TradeAction
from invo_agent.config import Settings
from invo_agent.db import AgentDB, PendingIntent
from invo_agent.dex.executor import DexExecutor
from invo_agent.dex.hl_trader import HlTraderError, InsufficientMarginError
from invo_agent.poller import NotificationPoller, locked_queued_entry_size


class _FakeTrader:
    def __init__(self, *, mid: float, available: float, pos=None, coins=None) -> None:
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

    def position_detail(self, coin: str):
        if self._pos is None:
            return None
        sz, is_long = self._pos
        return {"sz": sz, "is_long": is_long, "entry_px": self._mid}

    def position_size(self, coin: str):
        return self._pos

    def open_coins(self) -> set[str]:
        return set(self._coins)

    def cached_mid(self, coin: str) -> float:
        return self._mid

    def cached_equity(self) -> float:
        return 100.0

    def cached_available(self) -> float:
        return self._available


def _settings(tmp_path, **extra) -> Settings:
    kwargs = dict(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        hl_min_order_usd=10.0,
        max_position_pct=10.0,
        max_book_margin_pct=40.0,
        mac_notify=False,
    )
    kwargs.update(extra)
    return Settings(**kwargs)


def _exe(tmp_path, **extra) -> tuple[Settings, AgentDB, DexExecutor]:
    settings = _settings(tmp_path, **extra)
    db = AgentDB(settings.db_path)
    return settings, db, DexExecutor(settings, db=db)


def _poller(settings, db, client=None) -> NotificationPoller:
    return NotificationPoller(
        client=client or SimpleNamespace(session=None),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]


def _seed_copy(
    db: AgentDB,
    *,
    ticker: str,
    size: float,
    px: float,
    leverage: float,
    side: str = "long",
    base_id: str | None = None,
) -> str:
    bid = base_id or str(uuid4())
    db.upsert_open_copy(
        investment_base_id=bid,
        base_short_id=ticker.lower() + "Short",
        ticker=ticker,
        portfolio_id=None,
        notification_id="n1",
        side=side,
        size=size,
        leverage=leverage,
        trader="t",
    )
    db.set_open_copy_fill(
        bid, our_entry_px=px, side=side, size=size, leverage=leverage
    )
    return bid


def _inv(
    *,
    base_id,
    ticker="ADA",
    entry_size=25.0,
    prev=0.1,
    leverage=20,
    entry_price=0.20,
    long=True,
    increase=True,
):
    return InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "adaShort",
            "ticker": ticker,
            "directionLong": long,
            "leverage": leverage,
            "entrySize": entry_size,
            "entryPrice": entry_price,
            "changes": {"simIncrease": increase, "entrySize": prev},
        }
    )


def _open_action(*, base_id, ticker, entry_size, leverage, entry_price, long=True):
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": f"OPEN {ticker}",
            "notificationType": "user_added_investment",
            "createdAt": datetime.now(UTC).isoformat(),
            "investmentBaseId": str(base_id),
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "openShort",
            "ticker": ticker,
            "directionLong": long,
            "leverage": leverage,
            "entrySize": entry_size,
            "entryPrice": entry_price,
        }
    )
    return TradeAction(action=ActionType.OPEN, notification=notif, investment=inv)


# --- defaults / allocation -------------------------------------------------


def test_config_defaults_are_10_and_40() -> None:
    assert Settings.model_fields["max_position_pct"].default == 10.0
    assert Settings.model_fields["max_book_margin_pct"].default == 40.0


@pytest.mark.parametrize(
    "raw,expect",
    [
        (0.1, 0.1),
        (0.801, 0.801),
        (1.0, 1.0),
        (12.54, 12.54),
        (27.0, 27.0),
        (100.0, 100.0),
        (101.0, None),
        (0.0, None),
        (-1.0, None),
        (None, None),
    ],
)
def test_entry_size_stays_percent(tmp_path, raw, expect) -> None:
    _, db, exe = _exe(tmp_path)
    assert exe._trader_allocation_pct(raw) == expect
    db.close()


@pytest.mark.parametrize(
    "equity,lev,entry,want_ntl,capped,bumped",
    [
        # 0.1% clip on the $111 book — bump to HL $10, never to 10%.
        (100.0, 20, 0.1, 10.0, False, True),
        (100.0, 20, 0.1, 10.0, False, True),
        (80.0, 20, 0.1, 10.0, False, True),
        # 0.8% at 5x also needs the $10 bump (0.8% of $100 * 5 = $4).
        (100.0, 5, 0.801, 10.0, False, True),
        # True 5% fallback-sized clip at 10x is $50, no bump, under cap.
        (100.0, 10, 5.0, 50.0, False, False),
        # Exactly the per-copy cap.
        (100.0, 20, 10.0, 200.0, False, False),
        # A 27% / 20x clip caps at 10% margin ($200 notional), not the raw ask.
        (100.0, 20, 27.0, 200.0, True, False),
        # 25% / 20x caps at $200 notional, not the multi-hundred raw ask.
        (100.0, 20, 25.0, 200.0, True, False),
        # 25% / 30x caps at $300 notional, not a 25%*30x whale.
        (100.0, 30, 25.0, 300.0, True, False),
        # 1% is 1%, not the old ×100 fraction bug (that spent the whole book).
        (100.0, 20, 1.0, 20.0, False, False),
    ],
)
def test_resolve_margin_matrix(
    tmp_path, equity, lev, entry, want_ntl, capped, bumped
) -> None:
    _, db, exe = _exe(tmp_path)
    margin, meta = exe._resolve_margin(
        equity=equity, leverage=lev, entry_size=entry
    )
    assert meta["capped"] is capped
    assert meta["bumped_to_hl_min"] is bumped
    assert meta["final_pct"] <= 10.0 + 1e-9
    assert meta["order_notional_usd"] == pytest.approx(want_ntl, abs=1e-6)
    assert margin == pytest.approx(want_ntl / lev, abs=1e-6)
    db.close()


def test_missing_entry_size_uses_fallback_then_caps(tmp_path) -> None:
    _, db, exe = _exe(tmp_path, position_pct=5.0)
    _margin, meta = exe._resolve_margin(equity=100.0, leverage=10, entry_size=None)
    assert meta["source"] == "fallback_position_pct"
    assert meta["final_pct"] == 5.0
    assert meta["order_notional_usd"] == pytest.approx(50.0)
    _m2, meta2 = exe._resolve_margin(equity=100.0, leverage=20, entry_size=None)
    assert meta2["final_pct"] == 5.0
    assert meta2["order_notional_usd"] == pytest.approx(100.0)
    db.close()


def test_old_env_50_still_allows_27pct_without_book(tmp_path) -> None:
    """Why MAX_POSITION_PCT had to move to 10 — book cap alone is 40%."""
    _, db, exe = _exe(tmp_path, max_position_pct=50.0)
    _m, meta = exe._resolve_margin(equity=100.0, leverage=20, entry_size=27.0)
    assert meta["capped"] is False
    assert meta["final_pct"] == pytest.approx(27.0)
    assert meta["order_notional_usd"] == pytest.approx(100.0 * 0.27 * 20)
    db.close()


# --- book cap --------------------------------------------------------------


def test_copy_margin_is_notional_over_leverage(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    bid = _seed_copy(db, ticker="SOL", size=9.31, px=80.55, leverage=20)
    copy = db.get_open_copy(bid)
    assert copy is not None
    # 9.31 * 80.55 / 20 ≈ $37.5 margin — notional over leverage, not size.
    assert exe._copy_margin_usd(copy) == pytest.approx(9.31 * 80.55 / 20, abs=1e-6)
    db.close()


def test_book_used_excludes_this_copy(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    a = _seed_copy(db, ticker="HYPE", size=40.0, px=1.0, leverage=1)
    _seed_copy(db, ticker="XRP", size=10.0, px=1.0, leverage=1)
    assert exe._book_margin_used_usd() == pytest.approx(50.0)
    assert exe._book_margin_used_usd(exclude_base_id=a) == pytest.approx(10.0)
    db.close()


def test_book_room_shrinks_this_order_below_per_copy_cap(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    _seed_copy(db, ticker="HYPE", size=35.0, px=1.0, leverage=1)  # $35 / 35%
    _m, meta = exe._resolve_margin(equity=100.0, leverage=20, entry_size=10.0)
    assert meta["book_capped"] is True
    assert meta["final_pct"] == pytest.approx(5.0)
    assert meta["order_notional_usd"] == pytest.approx(100.0)
    db.close()


def test_book_room_too_small_for_hl_min_is_unsendable(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    _seed_copy(db, ticker="HYPE", size=39.6, px=1.0, leverage=1)  # room $0.40
    _m, meta = exe._resolve_margin(equity=100.0, leverage=20, entry_size=5.0)
    assert meta["book_capped"] is True
    assert meta["order_notional_usd"] == pytest.approx(8.0)
    assert meta["order_notional_usd"] < 10.0
    db.close()


def test_four_10pct_copies_fill_the_book_fifth_is_refused(tmp_path) -> None:
    settings, db, exe = _exe(tmp_path)
    for coin in ("SOL", "XRP", "BTC", "HYPE"):
        _seed_copy(db, ticker=coin, size=10.0, px=1.0, leverage=1)  # $10 each
    assert exe._book_margin_used_usd() == pytest.approx(40.0)
    margin, meta = exe._resolve_margin(equity=100.0, leverage=20, entry_size=5.0)
    assert meta["book_capped"] is True
    assert margin == 0.0
    assert meta["order_notional_usd"] < settings.hl_min_order_usd
    db.close()


def test_open_27pct_xrp_sizes_at_10pct_not_481(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    trader = _FakeTrader(mid=1.40, available=80.0)
    trader.ensure_leverage = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        HlTraderError("stop after size")
    )
    logged: list[dict] = []
    import invo_agent.dex.executor as exe_mod

    original = exe_mod.log_event
    exe_mod.log_event = lambda name, **kwargs: (  # type: ignore[assignment]
        logged.append(kwargs) if name == "open_size" else original(name, **kwargs)
    )
    action = _open_action(
        base_id=uuid4(),
        ticker="XRP",
        entry_size=27.69,
        leverage=20,
        entry_price=1.40,
    )
    try:
        with pytest.raises(HlTraderError, match="stop after size"):
            exe._open(action, trader, 100.0)  # type: ignore[arg-type]
    finally:
        exe_mod.log_event = original
    assert logged
    assert logged[0]["final_pct"] == pytest.approx(10.0)
    assert logged[0]["order_notional_usd"] == pytest.approx(200.0)
    assert logged[0]["entry_size"] == 27.69
    assert trader.orders == []
    db.close()


def test_open_raises_when_book_already_at_40(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    _seed_copy(db, ticker="HYPE", size=40.0, px=1.0, leverage=1)
    trader = _FakeTrader(mid=2.0, available=80.0)
    action = _open_action(
        base_id=uuid4(),
        ticker="ETH",
        entry_size=5.0,
        leverage=20,
        entry_price=2.0,
    )
    with pytest.raises(InsufficientMarginError, match="HL_MIN_ORDER"):
        exe._open(action, trader, 100.0)  # type: ignore[arg-type]
    assert trader.orders == []
    db.close()


def test_three_10pct_copies_still_allow_a_fourth(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    for coin in ("SOL", "XRP", "BTC"):
        _seed_copy(db, ticker=coin, size=10.0, px=1.0, leverage=1)
    _m, meta = exe._resolve_margin(equity=100.0, leverage=20, entry_size=10.0)
    assert meta["book_capped"] is False
    assert meta["final_pct"] == pytest.approx(10.0)
    assert meta["order_notional_usd"] == pytest.approx(200.0)
    db.close()


# --- size-add: synthetic allocation examples ----------------------------------------


def test_sol_size_add_stops_at_the_per_copy_cap(tmp_path) -> None:
    """Opened $10 / 0.1%. Later 25% must stop at 10% of $111 ($222), not $553+."""
    _, db, exe = _exe(tmp_path)
    equity = 100.0
    mid = 80.0
    current_sz = 10.0 / mid  # $10 clip
    base_id = _seed_copy(
        db, ticker="SOL", size=current_sz, px=mid, leverage=20, side="short"
    )
    trader = _FakeTrader(
        mid=mid, available=80.0, pos=(current_sz, False), coins={"SOL"}
    )
    _sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(
            base_id=base_id,
            ticker="SOL",
            entry_size=25.0,
            prev=0.1,
            leverage=20,
            entry_price=mid,
            long=False,
        ),
        coin="SOL",
        is_long=False,
        current_sz=current_sz,
        equity=equity,
    )
    assert sub is not None
    add_sz = trader.orders[0]["sz"]
    final_ntl = current_sz * mid + add_sz * mid
    assert final_ntl == pytest.approx(200.0, abs=1e-6)
    assert final_ntl < 230.0
    assert final_ntl * 3 < 700.0
    db.close()


def test_size_add_at_cap_does_not_grow_further(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    # Already 10% margin at 20x on $100 = $200 notional, sz=200 at $1.
    base_id = _seed_copy(db, ticker="SOL", size=200.0, px=1.0, leverage=20)
    trader = _FakeTrader(mid=1.0, available=80.0, pos=(200.0, True), coins={"SOL"})
    sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(base_id=base_id, entry_size=27.0, prev=10.0, leverage=20, entry_price=1.0),
        coin="SOL",
        is_long=True,
        current_sz=200.0,
        equity=100.0,
    )
    assert sub is None
    assert sz == 200.0
    assert trader.orders == []
    db.close()


def test_legacy_fat_xrp_does_not_add_or_flatten_on_update(tmp_path) -> None:
    """A legacy 27% clip is already over the new cap — UPDATE must not grow or dump."""
    _, db, exe = _exe(tmp_path)
    equity = 100.0
    mid = 1.40
    sz = 480.0 / mid
    base_id = _seed_copy(
        db, ticker="XRP", size=sz, px=mid, leverage=20, side="long"
    )
    trader = _FakeTrader(mid=mid, available=80.0, pos=(sz, True), coins={"XRP"})
    new_sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(
            base_id=base_id,
            ticker="XRP",
            entry_size=27.69,
            prev=20.0,
            leverage=20,
            entry_price=mid,
        ),
        coin="XRP",
        is_long=True,
        current_sz=sz,
        equity=equity,
    )
    assert sub is None
    assert new_sz == sz
    assert trader.orders == []
    copy = db.get_open_copy(base_id)
    assert copy is not None
    assert copy.size == pytest.approx(sz)
    db.close()


def test_size_add_uses_book_room_after_excluding_self(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    # Others $30. This clip $2 margin (sz=40 at $1, 20x). Room = 10%.
    _seed_copy(db, ticker="HYPE", size=30.0, px=1.0, leverage=1)
    base_id = _seed_copy(db, ticker="ADA", size=40.0, px=1.0, leverage=20)
    trader = _FakeTrader(mid=1.0, available=80.0, pos=(40.0, True), coins={"ADA"})
    _sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(base_id=base_id, entry_size=25.0, prev=1.0, leverage=20, entry_price=1.0),
        coin="ADA",
        is_long=True,
        current_sz=40.0,
        equity=100.0,
    )
    assert sub is not None
    add_sz = trader.orders[0]["sz"]
    # Target 10% of $100 * 20x = $200, current $40 → add $160.
    assert 40.0 + add_sz == pytest.approx(200.0)
    db.close()


def test_size_add_blocked_when_others_already_use_40(tmp_path) -> None:
    _, db, exe = _exe(tmp_path)
    _seed_copy(db, ticker="HYPE", size=40.0, px=1.0, leverage=1)
    base_id = _seed_copy(db, ticker="ADA", size=10.0, px=1.0, leverage=20)
    trader = _FakeTrader(mid=1.0, available=80.0, pos=(10.0, True), coins={"ADA"})
    sz, sub = exe._apply_size_increase(
        trader,  # type: ignore[arg-type]
        inv=_inv(base_id=base_id, entry_size=25.0, prev=0.1, leverage=20, entry_price=1.0),
        coin="ADA",
        is_long=True,
        current_sz=10.0,
        equity=100.0,
    )
    assert sub is None
    assert sz == 10.0
    assert trader.orders == []
    db.close()


def test_size_add_gap_notional_is_capped(tmp_path) -> None:
    settings, db, exe = _exe(tmp_path)
    base_id = _seed_copy(db, ticker="ADA", size=50.0, px=0.20, leverage=20)
    copy = db.get_open_copy(base_id)
    assert copy is not None
    poller = _poller(settings, db)
    poller.executor._trader = SimpleNamespace(
        cached_mid=lambda coin: 0.20,
        cached_equity=lambda: 100.0,
        cached_available=lambda: 80.0,
    )
    gap = poller._size_add_gap_notional(
        copy, _inv(base_id=base_id, entry_size=25.0, leverage=20, entry_price=0.20)
    )
    # Current $10, cap target $200.
    assert gap == pytest.approx(190.0)
    db.close()


def test_need_margin_20pct_is_capped_to_10(tmp_path) -> None:
    settings, db, _exe_ = _exe(tmp_path)
    poller = _poller(settings, db)
    poller.executor._trader = SimpleNamespace(
        cached_equity=lambda: 100.0,
        cached_available=lambda: 80.0,
        cached_mid=lambda coin: 2.0,
    )
    assert poller._need_margin_usd(entry_size=20.0, leverage=10) == pytest.approx(10.0)
    assert poller._need_margin_usd(entry_size=0.1, leverage=20) == pytest.approx(0.5)
    db.close()


# --- queue lock ------------------------------------------------------------


@pytest.mark.parametrize(
    "queued,live,expect",
    [
        (0.1, 25.0, 0.1),
        (0.1, 0.05, 0.05),
        (5.0, 5.0, 5.0),
        (None, 25.0, 25.0),
        (0.1, None, 0.1),
        (None, None, None),
        (12.54, 27.0, 12.54),
    ],
)
def test_locked_queued_entry_size(queued, live, expect) -> None:
    assert locked_queued_entry_size(queued, live) == expect


def test_drain_action_keeps_queued_01_against_live_25(tmp_path) -> None:
    settings, db, exe = _exe(tmp_path)
    poller = _poller(settings, db)
    base_id = uuid4()
    now = datetime.now(UTC).isoformat()
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=str(base_id),
            coin="SOL",
            side="short",
            reason="price",
            created_at=now,
            updated_at=now,
            entry_size=0.1,
            leverage=20,
        )
    )
    intent = db.get_pending_intent(str(base_id))
    assert intent is not None
    live = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "ticker": "SOL",
            "directionLong": False,
            "leverage": 20,
            "entrySize": 25.0,
            "entryPrice": 80.0,
        }
    )
    action = poller._action_from_pending(intent, live)
    assert action.investment is not None
    assert action.investment.entry_size == 0.1
    _m, meta = exe._resolve_margin(
        equity=100.0, leverage=20, entry_size=action.investment.entry_size
    )
    assert meta["order_notional_usd"] == pytest.approx(10.0)
    assert meta["trader_pct"] == 0.1
    db.close()


def test_refresh_chain_cannot_raise_queued_size(tmp_path) -> None:
    settings, db, _ = _exe(tmp_path)
    poller = _poller(settings, db)
    base_id = uuid4()
    now = datetime.now(UTC).isoformat()
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=str(base_id),
            coin="XRP",
            side="short",
            reason="margin",
            created_at=now,
            updated_at=now,
            entry_size=0.1,
            leverage=20,
            trader_entry=0.50,
            tp=0.40,
            sl=0.60,
            summary="OPEN XRP SHORT 20x @alpha_trader",
        )
    )
    for live_pct, px in ((5.0, 0.51), (15.0, 0.52), (25.0, 0.53), (27.0, 0.54)):
        notif = Notification.model_validate(
            {
                "id": str(uuid4()),
                "content": "x",
                "notificationType": "user_updated_investment",
                "createdAt": now,
                "investmentBaseId": str(base_id),
            }
        )
        inv = InvestmentUpdate.model_validate(
            {
                "baseId": str(base_id),
                "ticker": "XRP",
                "directionLong": False,
                "leverage": 20,
                "entrySize": live_pct,
                "entryPrice": px,
                "priceTarget": 0.30,
                "stopLoss": 0.70,
            }
        )
        assert poller._refresh_pending_from_update(
            TradeAction(action=ActionType.UPDATE, notification=notif, investment=inv)
        )
    row = db.get_pending_intent(str(base_id))
    assert row is not None
    assert row.entry_size == 0.1
    assert row.trader_entry == 0.54
    assert row.tp == 0.30
    assert row.sl == 0.70
    db.close()


def test_refresh_may_lower_queued_size_if_live_shrinks(tmp_path) -> None:
    settings, db, _ = _exe(tmp_path)
    poller = _poller(settings, db)
    base_id = uuid4()
    now = datetime.now(UTC).isoformat()
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=str(base_id),
            coin="ETH",
            side="long",
            reason="price",
            created_at=now,
            updated_at=now,
            entry_size=5.0,
            leverage=10,
            trader_entry=2000.0,
            summary="OPEN ETH",
        )
    )
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": now,
            "investmentBaseId": str(base_id),
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "ticker": "ETH",
            "directionLong": True,
            "leverage": 10,
            "entrySize": 1.5,
            "entryPrice": 1990.0,
        }
    )
    assert poller._refresh_pending_from_update(
        TradeAction(action=ActionType.UPDATE, notification=notif, investment=inv)
    )
    row = db.get_pending_intent(str(base_id))
    assert row is not None
    assert row.entry_size == 1.5
    db.close()


def test_drain_failure_cannot_raise_stored_entry_size(tmp_path) -> None:
    settings, db, _ = _exe(tmp_path)
    poller = _poller(settings, db)
    base_id = uuid4()
    now = datetime.now(UTC).isoformat()
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=str(base_id),
            coin="SOL",
            side="short",
            reason="price",
            created_at=now,
            updated_at=now,
            entry_size=0.1,
            leverage=20,
            trader="alpha_trader",
        )
    )
    intent = db.get_pending_intent(str(base_id))
    assert intent is not None
    fat = _open_action(
        base_id=base_id,
        ticker="SOL",
        entry_size=25.0,
        leverage=20,
        entry_price=80.0,
        long=False,
    )
    assert poller._apply_drain_failure(
        intent, fat, InsufficientMarginError("Order notional $0.00 < HL min")
    )
    row = db.get_pending_intent(str(base_id))
    assert row is not None
    assert row.entry_size == 0.1
    db.close()


def test_refresh_skips_once_copy_is_open(tmp_path) -> None:
    settings, db, _ = _exe(tmp_path)
    poller = _poller(settings, db)
    base_id = uuid4()
    now = datetime.now(UTC).isoformat()
    _seed_copy(db, ticker="SOL", size=0.13, px=80.0, leverage=20, base_id=str(base_id))
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=str(base_id),
            coin="SOL",
            side="short",
            reason="price",
            created_at=now,
            updated_at=now,
            entry_size=0.1,
            summary="OPEN SOL",
        )
    )
    notif = Notification.model_validate(
        {
            "id": str(uuid4()),
            "content": "x",
            "notificationType": "user_updated_investment",
            "createdAt": now,
            "investmentBaseId": str(base_id),
        }
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "ticker": "SOL",
            "entrySize": 25.0,
            "directionLong": False,
        }
    )
    assert (
        poller._refresh_pending_from_update(
            TradeAction(action=ActionType.UPDATE, notification=notif, investment=inv)
        )
        is False
    )
    db.close()


# --- expire ----------------------------------------------------------------


def _shadow_client(inv: InvestmentUpdate):
    return SimpleNamespace(
        get_current_shadow_post_by_investment_base_id=lambda _bid: SimpleNamespace(
            post=SimpleNamespace(update=inv)
        )
    )


def test_expire_keeps_23h_still_open(tmp_path) -> None:
    settings, db, _ = _exe(tmp_path)
    created = (datetime.now(UTC) - timedelta(hours=23)).isoformat()
    base_id = str(uuid4())
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=base_id,
            coin="DOT",
            side="long",
            reason="margin",
            created_at=created,
            updated_at=created,
            summary="OPEN DOT",
        )
    )
    inv = InvestmentUpdate.model_validate(
        {"baseId": base_id, "ticker": "DOT", "isOpen": True}
    )
    poller = _poller(settings, db, client=_shadow_client(inv))
    poller._expire_pending()
    assert len(db.list_pending_intents()) == 1
    db.close()


def test_expire_drops_25h_even_when_shadow_fails(tmp_path) -> None:
    settings, db, _ = _exe(tmp_path)
    created = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    base_id = str(uuid4())
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=base_id,
            coin="XRP",
            side="short",
            reason="margin",
            created_at=created,
            updated_at=created,
            summary="OPEN XRP",
            resting_oid=99,
        )
    )

    def _boom(_bid):
        raise RuntimeError("shadow down")

    poller = _poller(
        settings,
        db,
        client=SimpleNamespace(get_current_shadow_post_by_investment_base_id=_boom),
    )
    cancelled: list[int | None] = []
    poller._cancel_pending_resting = (  # type: ignore[method-assign]
        lambda intent: cancelled.append(intent.resting_oid)
    )
    poller._expire_pending()
    assert db.list_pending_intents() == []
    assert cancelled == [99]
    db.close()


def test_expire_only_drops_stale_rows(tmp_path) -> None:
    settings, db, _ = _exe(tmp_path)
    stale = (datetime.now(UTC) - timedelta(hours=29)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    old_id = str(uuid4())
    new_id = str(uuid4())
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=old_id,
            coin="XRP",
            side="short",
            reason="margin",
            created_at=stale,
            updated_at=stale,
            summary="OLD",
        )
    )
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=new_id,
            coin="SOL",
            side="short",
            reason="price",
            created_at=fresh,
            updated_at=fresh,
            summary="NEW",
        )
    )
    inv = InvestmentUpdate.model_validate({"ticker": "XRP", "isOpen": True})
    poller = _poller(settings, db, client=_shadow_client(inv))
    poller._cancel_pending_resting = lambda intent: None  # type: ignore[method-assign]
    poller._expire_pending()
    left = {row.investment_base_id for row in db.list_pending_intents()}
    assert left == {new_id}
    db.close()


def test_xrp_29h_queue_would_expire_before_fat_open(tmp_path) -> None:
    """BRsFFmNyIm sat ~29h because expire used to keep still-open Involio rows."""
    settings, db, _ = _exe(tmp_path)
    created = (datetime.now(UTC) - timedelta(hours=29)).isoformat()
    base_id = str(uuid4())
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id=base_id,
            coin="XRP",
            side="short",
            reason="margin",
            created_at=created,
            updated_at=created,
            entry_size=0.1,
            leverage=20,
            summary="OPEN XRP SHORT 20x @alpha_trader BRsFFmNyIm",
            resting_oid=7,
        )
    )
    inv = InvestmentUpdate.model_validate(
        {
            "baseId": base_id,
            "ticker": "XRP",
            "isOpen": True,
            "entrySize": 27.0,
            "leverage": 20,
        }
    )
    poller = _poller(settings, db, client=_shadow_client(inv))
    cancelled: list[int | None] = []
    poller._cancel_pending_resting = (  # type: ignore[method-assign]
        lambda intent: cancelled.append(intent.resting_oid)
    )
    poller._expire_pending()
    assert db.list_pending_intents() == []
    assert cancelled == [7]
    db.close()


# --- cluster replay --------------------------------------------------------


def test_whale_cluster_cannot_rebuild_on_one_book(tmp_path) -> None:
    """SOL+XRP+BTC+ETH whale notionals cannot sit on one book at once anymore."""
    _, db, exe = _exe(tmp_path)
    equity = 100.0
    # Each name asks for a whale-sized allocation at the same time.
    wanted = [
        ("SOL", 25.0, 20),
        ("XRP", 27.0, 20),
        ("BTC", 8.8, 30),
        ("ETH", 3.1, 25),
    ]
    notionals = []
    for coin, pct, lev in wanted:
        _m, meta = exe._resolve_margin(equity=equity, leverage=lev, entry_size=pct)
        # Seed at the *capped* margin so later names see a filling book.
        margin = float(meta["margin_usd"])
        if meta["order_notional_usd"] + 1e-9 >= 10.0:
            _seed_copy(db, ticker=coin, size=margin, px=1.0, leverage=1)
        notionals.append(meta["order_notional_usd"])
    # Old path: four uncapped whale notionals. New path: each ≤ 10% × lev.
    assert all(n <= equity * 0.10 * lev + 1e-6 for n, (_c, _p, lev) in zip(notionals, wanted))
    used = sum(exe._copy_margin_usd(c) for c in db.list_open_copies())
    assert used <= equity * 0.40 + 1e-6
    assert used > equity * 0.20
    _m, extra = exe._resolve_margin(equity=equity, leverage=20, entry_size=25.0)
    assert extra["capped"] is True
    assert extra["final_pct"] <= 10.0
    assert extra["order_notional_usd"] < 500.0
    db.close()
