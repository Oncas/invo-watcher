from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from invo_agent.events import (
    configure_events,
    log_event,
    log_event_throttled,
    reset_event_throttle,
)
from invo_agent.logging_setup import prune_logs


def test_prune_logs_deletes_old_history(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.mkdir()
    old = history / "2026-08-01.events.jsonl"
    old.write_text("{}\n")
    stamp = (datetime.now(UTC) - timedelta(days=10)).timestamp()
    old.touch()
    import os

    os.utime(old, (stamp, stamp))
    keep = history / "today.events.jsonl"
    keep.write_text("{}\n")
    events = tmp_path / "events.jsonl"
    events.write_text("x" * 200 + "\n" + "keep-me\n")

    stats = prune_logs(tmp_path, retain_days=7, events_cap_bytes=50, min_free_mb=0)
    assert not old.exists()
    assert keep.exists()
    assert stats["deleted"] == 1
    assert stats["trimmed"] == 1
    assert events.read_text().endswith("keep-me\n")


def test_log_event_throttled(tmp_path: Path) -> None:
    reset_event_throttle()
    configure_events(tmp_path)
    assert log_event_throttled("pending_still_waiting", key="xrp", every_s=60)
    assert not log_event_throttled("pending_still_waiting", key="xrp", every_s=60)
    assert log_event_throttled("pending_still_waiting", key="btc", every_s=60)
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    log_event("open_hl", coin="ETH")
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 3
    time.sleep(0.05)
    assert log_event_throttled("pending_still_waiting", key="xrp", every_s=0.01)
