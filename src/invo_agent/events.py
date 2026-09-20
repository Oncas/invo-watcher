from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_path: Path | None = None
_daily_path: Path | None = None
_history_dir: Path | None = None
_throttle: dict[str, float] = {}
_THROTTLE_S = 900.0


def _now() -> datetime:
    """Wall clock, indirected so the midnight rollover is testable."""
    return datetime.now()


def today_stamp() -> str:
    """Local date, matching the dated log filenames the agent already writes."""
    return _now().strftime("%Y-%m-%d")


def configure_events(log_dir: Path, *, daily_path: Path | None = None) -> Path:
    """Set JSONL event log paths. Always append — never truncate.

    Without an explicit ``daily_path`` the dated file is resolved per append,
    so a process that runs past midnight starts a new day's file instead of
    growing yesterday's for ever.
    """
    global _path, _daily_path, _history_dir
    log_dir = log_dir.expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    _path = log_dir / "events.jsonl"
    if daily_path is not None:
        daily_path = daily_path.expanduser()
        daily_path.parent.mkdir(parents=True, exist_ok=True)
        _daily_path = daily_path
        _history_dir = None
    else:
        _daily_path = None
        _history_dir = log_dir / "history"
        _history_dir.mkdir(parents=True, exist_ok=True)
    return _path


def daily_events_path() -> Path | None:
    """Where an event written right now belongs."""
    if _daily_path is not None:
        return _daily_path
    if _history_dir is None:
        return None
    return _history_dir / f"{today_stamp()}.events.jsonl"


def events_path() -> Path | None:
    return _path


def reset_event_throttle() -> None:
    _throttle.clear()


def log_event(kind: str, **fields: Any) -> None:
    """Append one structured event for overnight debugging."""
    if _path is None:
        return
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "kind": kind,
        **{k: v for k, v in fields.items() if v is not None},
    }
    line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
    with _lock:
        for path in (_path, daily_events_path()):
            if path is None:
                continue
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()


def log_event_throttled(
    kind: str,
    *,
    key: str,
    every_s: float = _THROTTLE_S,
    **fields: Any,
) -> bool:
    """Same as log_event, but at most once per key every ``every_s`` seconds."""
    token = f"{kind}:{key}"
    now = time.monotonic()
    prev = _throttle.get(token)
    if prev is not None and now - prev < every_s:
        return False
    _throttle[token] = now
    log_event(kind, **fields)
    return True
