from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from invo_agent.config import Settings
from invo_agent.events import configure_events, log_event
from invo_agent.logging_setup import DatedFileHandler


class _Clock:
    """A movable stand-in for the wall clock, so midnight is testable."""

    def __init__(self, stamp: str):
        self.stamp = stamp

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.stamp)

    def set(self, stamp: str) -> None:
        self.stamp = stamp


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)


# --- the daily log handler --------------------------------------------------


def test_handler_writes_to_todays_dated_file(tmp_path: Path) -> None:
    clock = _Clock("2026-09-19T23:59:00")
    handler = DatedFileHandler(tmp_path, now=clock)
    handler.emit(_record("before midnight"))
    handler.close()
    assert "before midnight" in (tmp_path / "2026-09-19.log").read_text()


def test_handler_moves_to_the_new_file_after_midnight(tmp_path: Path) -> None:
    clock = _Clock("2026-09-19T23:59:00")
    handler = DatedFileHandler(tmp_path, now=clock)
    handler.emit(_record("before midnight"))
    clock.set("2026-09-20T00:00:30")
    handler.emit(_record("after midnight"))
    handler.close()

    yesterday = (tmp_path / "2026-09-19.log").read_text()
    today = (tmp_path / "2026-09-20.log").read_text()
    assert "before midnight" in yesterday
    assert "after midnight" not in yesterday
    assert "after midnight" in today


def test_handler_does_not_reopen_a_finished_day(tmp_path: Path) -> None:
    """Once the day rolls over, yesterday's file must never grow again."""
    clock = _Clock("2026-09-19T23:59:00")
    handler = DatedFileHandler(tmp_path, now=clock)
    handler.emit(_record("one"))
    clock.set("2026-09-20T00:01:00")
    handler.emit(_record("two"))
    size = (tmp_path / "2026-09-19.log").stat().st_size
    handler.emit(_record("three"))
    handler.close()
    assert (tmp_path / "2026-09-19.log").stat().st_size == size


def test_handler_appends_within_the_same_day(tmp_path: Path) -> None:
    clock = _Clock("2026-09-19T08:00:00")
    handler = DatedFileHandler(tmp_path, now=clock)
    handler.emit(_record("one"))
    clock.set("2026-09-19T09:00:00")
    handler.emit(_record("two"))
    handler.close()
    body = (tmp_path / "2026-09-19.log").read_text()
    assert "one" in body and "two" in body


def test_handler_reopens_without_truncating_an_existing_day(tmp_path: Path) -> None:
    clock = _Clock("2026-09-19T08:00:00")
    first = DatedFileHandler(tmp_path, now=clock)
    first.emit(_record("from the old process"))
    first.close()
    second = DatedFileHandler(tmp_path, now=clock)
    second.emit(_record("from the new process"))
    second.close()
    body = (tmp_path / "2026-09-19.log").read_text()
    assert "from the old process" in body
    assert "from the new process" in body


# --- the events sink --------------------------------------------------------


def test_events_follow_the_date_without_reconfiguring(
    tmp_path: Path, monkeypatch
) -> None:
    clock = _Clock("2026-09-19T23:59:00")
    monkeypatch.setattr("invo_agent.events._now", clock)
    configure_events(tmp_path)
    log_event("before_midnight", coin="ETH")
    clock.set("2026-09-20T00:00:30")
    log_event("after_midnight", coin="BTC")

    history = tmp_path / "history"
    assert "before_midnight" in (history / "2026-09-19.events.jsonl").read_text()
    assert "after_midnight" in (history / "2026-09-20.events.jsonl").read_text()
    assert "after_midnight" not in (history / "2026-09-19.events.jsonl").read_text()


def test_events_still_write_the_live_file_across_the_rollover(
    tmp_path: Path, monkeypatch
) -> None:
    clock = _Clock("2026-09-19T23:59:00")
    monkeypatch.setattr("invo_agent.events._now", clock)
    configure_events(tmp_path)
    log_event("before_midnight")
    clock.set("2026-09-20T00:00:30")
    log_event("after_midnight")
    body = (tmp_path / "events.jsonl").read_text()
    assert "before_midnight" in body and "after_midnight" in body


# --- retention is configurable ---------------------------------------------


def test_log_retain_days_defaults_to_seven() -> None:
    assert Settings().log_retain_days == 7


def test_log_retain_days_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("LOG_RETAIN_DAYS", "14")
    assert Settings().log_retain_days == 14
