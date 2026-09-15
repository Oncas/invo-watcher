"""The trader entry we copied at must survive on the row.

The recovery comparison is only fair if both sides are measured from the same
starting price. Involio reports the trader's *current* average entry, which
moves every time they scale in, so the entry has to be captured when the copy is
opened and read back from the row at close time.
"""

from __future__ import annotations

from invo_agent.config import Settings
from invo_agent.db import AgentDB


def _db(tmp_path) -> AgentDB:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        mac_notify=False,
    )
    return AgentDB(settings.db_path)


def test_trader_entry_is_persisted_and_read_back(tmp_path):
    db = _db(tmp_path)
    db.upsert_open_copy(
        investment_base_id="base-1",
        base_short_id="short-1",
        ticker="AAA",
        portfolio_id=None,
        notification_id=None,
        our_entry_px=100.0,
        trader_entry_px=101.0,
        side="long",
        size=2.0,
        leverage=10.0,
    )

    copy = db.get_open_copy("base-1")

    assert copy is not None
    assert copy.trader_entry_px == 101.0


def test_a_later_upsert_does_not_wipe_the_copied_entry(tmp_path):
    """Updates that do not carry the entry must leave the stored one alone."""
    db = _db(tmp_path)
    db.upsert_open_copy(
        investment_base_id="base-1",
        base_short_id="short-1",
        ticker="AAA",
        portfolio_id=None,
        notification_id=None,
        our_entry_px=100.0,
        trader_entry_px=101.0,
        side="long",
        size=2.0,
        leverage=10.0,
    )
    db.upsert_open_copy(
        investment_base_id="base-1",
        base_short_id="short-1",
        ticker="AAA",
        portfolio_id=None,
        notification_id="n-2",
        size=3.0,
    )

    copy = db.get_open_copy("base-1")

    assert copy is not None
    assert copy.trader_entry_px == 101.0
    assert copy.size == 3.0


def test_rows_written_before_the_column_existed_read_as_none(tmp_path):
    """Pre-migration copies have no stored entry; the caller falls back."""
    db = _db(tmp_path)
    db.upsert_open_copy(
        investment_base_id="base-old",
        base_short_id="short-old",
        ticker="AAA",
        portfolio_id=None,
        notification_id=None,
        our_entry_px=100.0,
        side="long",
        size=2.0,
        leverage=10.0,
    )

    copy = db.get_open_copy("base-old")

    assert copy is not None
    assert copy.trader_entry_px is None
