"""The BTC adopt path — a stranger on the book must not become our copy.

Replay of the 27 Aug BTC case: HL held a position with no open_copy, the agent
logged ``inverse_ghost`` 16 times (all with ``"pending": false``), and then a
brand-new pending intent for the same coin walked in and the adopt branch
handed it the stranger — plus an unrequested TP on a position we never opened.

The guard: a position already flagged as a ghost *before* the intent existed is
not that intent's fill, and the side has to match either way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from invo_agent.api.models import InvestmentUpdate
from invo_agent.config import Settings
from invo_agent.db import AgentDB, PendingIntent
from invo_agent.dex.executor import DexExecutor
from invo_agent.poller import GHOST_SEEN_META_PREFIX, NotificationPoller


class _FakePos:
    def __init__(
        self, *, sz: float, is_long: bool, entry_px: float, leverage: int
    ) -> None:
        self.sz = sz
        self.is_long = is_long
        self.entry_px = entry_px
        self.leverage = leverage


class _FakeBook:
    def __init__(self, positions: dict[str, _FakePos]) -> None:
        self.positions = positions

    def position(self, coin: str) -> _FakePos | None:
        return self.positions.get(coin.upper())


class _FakeExecutor:
    """Only the surface ``_reconcile_inverse_ghosts`` actually touches."""

    def __init__(self, book: _FakeBook, *, untracked: list[str]) -> None:
        self._trader = SimpleNamespace(_book=book)
        self._untracked = untracked
        self.adopted: list[dict] = []

    def untracked_hl_coins(self) -> list[str]:
        return list(self._untracked)

    def adopt_open_fill(self, **kwargs) -> None:
        self.adopted.append(kwargs)


class _FakeClient:
    def __init__(self, inv: InvestmentUpdate | None) -> None:
        self._inv = inv
        self.session = None
        self.asked: list[str] = []

    def get_current_shadow_post_by_investment_base_id(self, base_id: str):
        self.asked.append(base_id)
        post = None if self._inv is None else SimpleNamespace(update=self._inv)
        return SimpleNamespace(post=post)


def _settings(tmp_path, **extra) -> Settings:
    kwargs = dict(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        mac_notify=False,
    )
    kwargs.update(extra)
    return Settings(**kwargs)


def _inv(base_id: str, *, ticker="BTC", long=True) -> InvestmentUpdate:
    return InvestmentUpdate.model_validate(
        {
            "baseId": str(base_id),
            "baseShortId": "btcShort",
            "ticker": ticker,
            "directionLong": long,
            "leverage": 10,
            "entrySize": 5.0,
            "entryPrice": 60_000.0,
        }
    )


def _pending(
    db: AgentDB,
    *,
    coin="BTC",
    side="long",
    created_at: datetime | None = None,
) -> PendingIntent:
    now = (created_at or datetime.now(UTC)).isoformat()
    intent = PendingIntent(
        investment_base_id=str(uuid4()),
        coin=coin,
        side=side,
        reason="margin",
        created_at=now,
        updated_at=now,
        base_short_id="btcShort",
        notification_id=str(uuid4()),
        leverage=10.0,
        trader_entry=60_000.0,
        entry_size=5.0,
        summary=f"queued OPEN {coin}",
        trader="t",
    )
    db.upsert_pending_intent(intent)
    return intent


def _poller(tmp_path, *, book, untracked, inv, **settings_extra):
    settings = _settings(tmp_path, **settings_extra)
    db = AgentDB(settings.db_path)
    poller = NotificationPoller(
        client=_FakeClient(inv),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    poller.executor = _FakeExecutor(book, untracked=untracked)  # type: ignore[assignment]
    return poller, db


def _btc_book(*, is_long=True) -> _FakeBook:
    return _FakeBook(
        {"BTC": _FakePos(sz=0.01, is_long=is_long, entry_px=60_000.0, leverage=10)}
    )


# --- the executor surface the guard leans on -------------------------------


def test_executor_still_exposes_the_adopt_surface() -> None:
    for name in ("untracked_hl_coins", "adopt_open_fill"):
        assert callable(getattr(DexExecutor, name))


# --- ghost bookkeeping ------------------------------------------------------


def test_first_scan_records_the_ghost_and_adopts_nothing(tmp_path) -> None:
    poller, db = _poller(tmp_path, book=_btc_book(), untracked=["BTC"], inv=None)

    poller._reconcile_inverse_ghosts()

    assert poller.executor.adopted == []
    assert db.get_meta(f"{GHOST_SEEN_META_PREFIX}BTC") is not None
    db.close()


def test_ghost_first_seen_does_not_move_on_later_scans(tmp_path) -> None:
    poller, db = _poller(tmp_path, book=_btc_book(), untracked=["BTC"], inv=None)

    poller._reconcile_inverse_ghosts()
    first = db.get_meta(f"{GHOST_SEEN_META_PREFIX}BTC")
    for _ in range(15):  # the 16 events from the real run
        poller._reconcile_inverse_ghosts()

    assert db.get_meta(f"{GHOST_SEEN_META_PREFIX}BTC") == first
    db.close()


def test_ghost_marker_clears_once_the_coin_leaves_the_book(tmp_path) -> None:
    poller, db = _poller(tmp_path, book=_btc_book(), untracked=["BTC"], inv=None)
    poller._reconcile_inverse_ghosts()
    assert poller._known_ghosts() == {"BTC"}

    poller.executor._untracked = []
    poller._reconcile_inverse_ghosts()

    assert poller._known_ghosts() == set()
    db.close()


# --- the 27 Aug path: an old stranger must not be adopted -------------------


def test_stranger_seen_before_the_intent_is_not_adopted(tmp_path) -> None:
    base_id = str(uuid4())
    poller, db = _poller(
        tmp_path, book=_btc_book(), untracked=["BTC"], inv=_inv(base_id)
    )
    poller._reconcile_inverse_ghosts()  # flag the ghost first — no pending yet
    _pending(db)  # ...then the intent shows up

    poller._reconcile_inverse_ghosts()

    assert poller.executor.adopted == []
    assert poller.client.asked == []  # never even reached for the shadow post
    assert db.list_pending_intents() != []  # intent survives, still queued
    assert db.list_open_copies() == []  # and no copy was invented
    db.close()


def test_refusal_survives_a_restart(tmp_path) -> None:
    """The marker is in the DB, not in memory — a new poller still refuses."""
    base_id = str(uuid4())
    poller, db = _poller(
        tmp_path, book=_btc_book(), untracked=["BTC"], inv=_inv(base_id)
    )
    poller._reconcile_inverse_ghosts()
    _pending(db)
    db.close()

    poller2, db2 = _poller(
        tmp_path, book=_btc_book(), untracked=["BTC"], inv=_inv(base_id)
    )
    poller2._reconcile_inverse_ghosts()

    assert poller2.executor.adopted == []
    db2.close()


@pytest.mark.parametrize("pos_long,intent_side", [(True, "short"), (False, "long")])
def test_side_mismatch_is_never_adopted(tmp_path, pos_long, intent_side) -> None:
    base_id = str(uuid4())
    poller, db = _poller(
        tmp_path,
        book=_btc_book(is_long=pos_long),
        untracked=["BTC"],
        inv=_inv(base_id, long=not pos_long),
    )
    _pending(db, side=intent_side)  # intent exists before any ghost flag

    poller._reconcile_inverse_ghosts()

    assert poller.executor.adopted == []
    assert db.list_pending_intents() != []
    db.close()


def test_side_mismatch_beats_a_clean_timeline(tmp_path) -> None:
    """Even a fresh intent with no prior ghost flag cannot take the wrong side."""
    base_id = str(uuid4())
    poller, db = _poller(
        tmp_path, book=_btc_book(is_long=True), untracked=["BTC"], inv=_inv(base_id)
    )
    _pending(db, side="short", created_at=datetime.now(UTC) + timedelta(minutes=5))

    poller._reconcile_inverse_ghosts()

    assert poller.executor.adopted == []
    db.close()


# --- the legitimate path still works ---------------------------------------


def test_fresh_matching_intent_is_still_adopted(tmp_path) -> None:
    base_id = str(uuid4())
    poller, db = _poller(
        tmp_path, book=_btc_book(), untracked=["BTC"], inv=_inv(base_id)
    )
    intent = _pending(db)  # queued first, position appears after

    poller._reconcile_inverse_ghosts()

    assert len(poller.executor.adopted) == 1
    adopted = poller.executor.adopted[0]
    assert adopted["coin"] == "BTC"
    assert adopted["is_long"] is True
    assert adopted["sz"] == 0.01
    assert db.get_pending_intent(intent.investment_base_id) is None
    db.close()


def test_intent_created_after_the_ghost_flag_clears_is_adopted(tmp_path) -> None:
    """Ghost went away, came back as our own fill — adoption is legal again."""
    base_id = str(uuid4())
    poller, db = _poller(
        tmp_path, book=_btc_book(), untracked=["BTC"], inv=_inv(base_id)
    )
    poller._reconcile_inverse_ghosts()  # flagged
    poller.executor._untracked = []
    poller._reconcile_inverse_ghosts()  # cleared

    _pending(db)
    poller.executor._untracked = ["BTC"]
    poller._reconcile_inverse_ghosts()

    assert len(poller.executor.adopted) == 1
    db.close()


def test_adopt_clears_the_ghost_marker(tmp_path) -> None:
    base_id = str(uuid4())
    poller, db = _poller(
        tmp_path, book=_btc_book(), untracked=["BTC"], inv=_inv(base_id)
    )
    _pending(db)

    poller._reconcile_inverse_ghosts()

    assert poller._known_ghosts() == set()
    db.close()


# --- nothing here may ever flatten -----------------------------------------


def test_a_refused_ghost_is_never_flattened(tmp_path) -> None:
    base_id = str(uuid4())
    poller, db = _poller(
        tmp_path, book=_btc_book(), untracked=["BTC"], inv=_inv(base_id)
    )
    poller._reconcile_inverse_ghosts()
    _pending(db, side="short")

    for _ in range(5):
        poller._reconcile_inverse_ghosts()

    assert poller.executor.adopted == []
    assert db.list_open_copies() == []
    db.close()
