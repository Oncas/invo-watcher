"""Ghost reconcile must not delete a copy it has only just written.

Two guards are pinned here:

* the held set is read from a *fresh* HL book, never the cached snapshot, so a
  resting fill that landed moments ago is visible;
* a copy younger than the grace window is left alone even when HL still reads
  flat, because "not visible yet" is not "never opened".

Without them a freshly adopted copy is cleared as a ghost, and the trader's
later CLOSE finds no row to match and is skipped as "not our copy" — the
position stays open with nothing tracking it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from invo_agent.config import Settings
from invo_agent.db import OpenCopy
from invo_agent.dex.executor import (
    GHOST_CLEAR_GRACE_SECONDS,
    DexExecutor,
    _copy_age_seconds,
)


class _FakeTrader:
    """Records whether the book was refreshed before the held set was read."""

    def __init__(self, held: set[str], *, held_after_refresh: set[str] | None = None):
        self._held = held
        self._after = held_after_refresh
        self.refreshed = 0

    def refresh_book(self, *, fills_since_ms: int | None = None) -> None:
        self.refreshed += 1
        if self._after is not None:
            self._held = self._after

    def open_coins(self) -> set[str]:
        return set(self._held)


class _FakeDB:
    def __init__(self, copies: list[OpenCopy]) -> None:
        self._copies = list(copies)
        self.closed: list[str] = []

    def list_open_copies(self) -> list[OpenCopy]:
        return list(self._copies)

    def close_copy(self, base_id: str) -> None:
        self.closed.append(base_id)
        self._copies = [c for c in self._copies if c.investment_base_id != base_id]


def _settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        mac_notify=False,
    )


def _copy(base_id: str, *, ticker: str, age_seconds: float) -> OpenCopy:
    opened = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return OpenCopy(
        investment_base_id=base_id,
        base_short_id=f"short-{base_id}",
        ticker=ticker,
        portfolio_id=None,
        opened_at=opened.isoformat(),
        last_notification_id=None,
    )


def test_fresh_copy_survives_a_flat_snapshot(tmp_path):
    """A copy opened seconds ago is not a ghost, even when HL reads flat."""
    db = _FakeDB([_copy("base-fresh", ticker="AAA", age_seconds=2.0)])
    executor = DexExecutor(_settings(tmp_path), db=db)

    cleared = executor.reconcile_ghost_copies(_FakeTrader(held=set()))

    assert cleared == []
    assert db.closed == []


def test_old_copy_flat_on_hl_is_cleared(tmp_path):
    """Past the grace window a flat read is trusted and the row is dropped."""
    age = GHOST_CLEAR_GRACE_SECONDS + 30.0
    db = _FakeDB([_copy("base-old", ticker="AAA", age_seconds=age)])
    executor = DexExecutor(_settings(tmp_path), db=db)

    cleared = executor.reconcile_ghost_copies(_FakeTrader(held=set()))

    assert cleared == ["AAA"]
    assert db.closed == ["base-old"]


def test_held_coin_is_never_cleared(tmp_path):
    """A coin HL still holds is left alone regardless of age."""
    age = GHOST_CLEAR_GRACE_SECONDS + 30.0
    db = _FakeDB([_copy("base-held", ticker="AAA", age_seconds=age)])
    executor = DexExecutor(_settings(tmp_path), db=db)

    cleared = executor.reconcile_ghost_copies(_FakeTrader(held={"AAA"}))

    assert cleared == []
    assert db.closed == []


def test_book_is_refreshed_before_judging_flat(tmp_path):
    """The stale-cache race: refreshing reveals the fill, so nothing is cleared.

    The trader reads flat until ``refresh_book`` runs, mirroring a snapshot
    taken before a resting order filled.
    """
    age = GHOST_CLEAR_GRACE_SECONDS + 30.0
    db = _FakeDB([_copy("base-filled", ticker="AAA", age_seconds=age)])
    executor = DexExecutor(_settings(tmp_path), db=db)
    trader = _FakeTrader(held=set(), held_after_refresh={"AAA"})

    cleared = executor.reconcile_ghost_copies(trader)

    assert trader.refreshed == 1
    assert cleared == []
    assert db.closed == []


def test_hl_read_failure_clears_nothing(tmp_path):
    """A failed HL read must never be mistaken for a flat book."""

    class _Broken(_FakeTrader):
        def refresh_book(self, *, fills_since_ms: int | None = None) -> None:
            raise RuntimeError("HL unreachable")

    age = GHOST_CLEAR_GRACE_SECONDS + 30.0
    db = _FakeDB([_copy("base-any", ticker="AAA", age_seconds=age)])
    executor = DexExecutor(_settings(tmp_path), db=db)

    cleared = executor.reconcile_ghost_copies(_Broken(held=set()))

    assert cleared == []
    assert db.closed == []


def test_unparseable_opened_at_does_not_block_clearing(tmp_path):
    """A bad timestamp must not make a row permanently unclearable."""
    copy = OpenCopy(
        investment_base_id="base-bad-ts",
        base_short_id="short",
        ticker="AAA",
        portfolio_id=None,
        opened_at="not-a-timestamp",
        last_notification_id=None,
    )
    assert _copy_age_seconds(copy) is None

    db = _FakeDB([copy])
    executor = DexExecutor(_settings(tmp_path), db=db)

    cleared = executor.reconcile_ghost_copies(_FakeTrader(held=set()))

    assert cleared == ["AAA"]
