from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_path: Path | None = None
_daily_path: Path | None = None


def configure_events(log_dir: Path, *, daily_path: Path | None = None) -> Path:
    """Set JSONL event log paths. Always append — never truncate."""
    global _path, _daily_path
    log_dir = log_dir.expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    _path = log_dir / "events.jsonl"
    if daily_path is not None:
        daily_path = daily_path.expanduser()
        daily_path.parent.mkdir(parents=True, exist_ok=True)
        _daily_path = daily_path
    else:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        hist = log_dir / "history"
        hist.mkdir(parents=True, exist_ok=True)
        _daily_path = hist / f"{day}.events.jsonl"
    return _path


def events_path() -> Path | None:
    return _path


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
        for path in (_path, _daily_path):
            if path is None:
                continue
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
