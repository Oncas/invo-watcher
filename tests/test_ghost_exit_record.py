"""A position closed by a parked trigger must leave an exit behind it.

A reduce-only TP/SL fills on the exchange, not through the agent, so the first
the agent sees is an empty book. Clearing the row on that alone deletes the only
record of the trade: no exit price, no PnL, nothing to read back. The flat read
is attributed to the fills that caused it before the row goes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from invo_agent.config import Settings
from invo_agent.db import OpenCopy
from invo_agent.dex.executor import _attribute_ghost_exit
from invo_agent.dex.hl_trader import HlFill


def _settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        mac_notify=False,
    )


def _copy(*, side: str, age_seconds: float = 3600.0) -> OpenCopy:
    opened = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return OpenCopy(
        investment_base_id="base-1",
        base_short_id="short-1",
        ticker="AAA",
        portfolio_id=None,
        opened_at=opened.isoformat(),
        last_notification_id=None,
        our_entry_px=100.0,
        side=side,
        size=2.0,
    )


def _ms(seconds_ago: float) -> int:
    return int((datetime.now(UTC) - timedelta(seconds=seconds_ago)).timestamp() * 1000)


def test_stop_out_on_a_long_is_attributed_to_its_sell_fill() -> None:
    copy = _copy(side="long")
    fills = [
        HlFill(coin="AAA", px=96.0, sz=2.0, side="A", time_ms=_ms(60), closed_pnl=-8.0)
    ]

    exit_fill = _attribute_ghost_exit(copy, "AAA", fills)

    assert exit_fill is not None
    assert exit_fill.px == 96.0
    assert exit_fill.sz == 2.0
    assert exit_fill.closed_pnl == -8.0


def test_partial_fills_average_by_size() -> None:
    """Two fills at different prices report one size-weighted exit."""
    copy = _copy(side="long")
    fills = [
        HlFill(coin="AAA", px=96.0, sz=1.0, side="A", time_ms=_ms(90), closed_pnl=-4.0),
        HlFill(coin="AAA", px=94.0, sz=3.0, side="A", time_ms=_ms(60), closed_pnl=-18.0),
    ]

    exit_fill = _attribute_ghost_exit(copy, "AAA", fills)

    assert exit_fill is not None
    assert exit_fill.sz == 4.0
    assert abs(exit_fill.px - 94.5) < 1e-9
    assert exit_fill.closed_pnl == -22.0


def test_the_opening_fill_is_not_mistaken_for_an_exit() -> None:
    """A long is closed by a sell; its own buy must never read as the exit."""
    copy = _copy(side="long")
    fills = [
        HlFill(coin="AAA", px=100.0, sz=2.0, side="B", time_ms=_ms(120), closed_pnl=None)
    ]

    assert _attribute_ghost_exit(copy, "AAA", fills) is None


def test_a_short_is_closed_by_a_buy() -> None:
    copy = _copy(side="short")
    fills = [
        HlFill(coin="AAA", px=104.0, sz=2.0, side="B", time_ms=_ms(60), closed_pnl=-8.0),
        HlFill(coin="AAA", px=100.0, sz=2.0, side="A", time_ms=_ms(300), closed_pnl=None),
    ]

    exit_fill = _attribute_ghost_exit(copy, "AAA", fills)

    assert exit_fill is not None
    assert exit_fill.px == 104.0


def test_fills_from_before_this_copy_are_ignored() -> None:
    """An earlier trade on the same coin must not be claimed as our exit."""
    copy = _copy(side="long", age_seconds=600.0)
    fills = [
        HlFill(coin="AAA", px=96.0, sz=2.0, side="A", time_ms=_ms(900), closed_pnl=-8.0)
    ]

    assert _attribute_ghost_exit(copy, "AAA", fills) is None


def test_another_coin_is_never_borrowed() -> None:
    copy = _copy(side="long")
    fills = [
        HlFill(coin="BBB", px=96.0, sz=2.0, side="A", time_ms=_ms(60), closed_pnl=-8.0)
    ]

    assert _attribute_ghost_exit(copy, "AAA", fills) is None


def test_no_fills_still_clears_the_row_unattributed() -> None:
    """Attribution is best-effort: a missing fill must not block the clear."""
    copy = _copy(side="long")

    assert _attribute_ghost_exit(copy, "AAA", []) is None


class _Book:
    def __init__(self, fills: list[HlFill]) -> None:
        self.fills = list(fills)


class _FakeTrader:
    def __init__(self, fills: list[HlFill]) -> None:
        self._fills = fills
        self.since_ms: int | None = None

    def refresh_book(self, *, fills_since_ms: int | None = None) -> _Book:
        self.since_ms = fills_since_ms
        return _Book(self._fills)

    def open_coins(self) -> set[str]:
        return set()


class _FakeDB:
    def __init__(self, copies: list[OpenCopy]) -> None:
        self._copies = list(copies)
        self.closed: list[str] = []
        self.actions: list[tuple[str, str | None]] = []

    def list_open_copies(self) -> list[OpenCopy]:
        return list(self._copies)

    def close_copy(self, base_id: str) -> None:
        self.closed.append(base_id)
        self._copies = [c for c in self._copies if c.investment_base_id != base_id]

    def log_action(self, *, action, summary, notification_id, dry_run) -> None:
        self.actions.append((action, summary))


def test_reconcile_records_the_exit_before_dropping_the_row(tmp_path):
    from invo_agent.dex.executor import DexExecutor

    copy = _copy(side="long")
    fills = [
        HlFill(coin="AAA", px=96.0, sz=2.0, side="A", time_ms=_ms(60), closed_pnl=-8.0)
    ]
    db = _FakeDB([copy])
    executor = DexExecutor(_settings(tmp_path), db=db)
    trader = _FakeTrader(fills)

    cleared = executor.reconcile_ghost_copies(trader)

    assert cleared == ["AAA"]
    assert db.closed == ["base-1"]
    # The exit is written down, and it reaches the action log the status view reads.
    assert [a for a, _ in db.actions] == ["GHOST_CLOSE"]
    assert "96" in (db.actions[0][1] or "")
    # Fills were requested far enough back to cover the open copy.
    assert trader.since_ms is not None


def test_reconcile_without_fills_still_clears(tmp_path):
    """No attributable fill is not a reason to keep a flat row forever."""
    from invo_agent.dex.executor import DexExecutor

    db = _FakeDB([_copy(side="long")])
    executor = DexExecutor(_settings(tmp_path), db=db)

    cleared = executor.reconcile_ghost_copies(_FakeTrader([]))

    assert cleared == ["AAA"]
    assert db.closed == ["base-1"]
    assert db.actions == []
