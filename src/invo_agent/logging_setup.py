from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from invo_agent.events import configure_events


class _FlushFileHandler(logging.FileHandler):
    """Append-only file handler that flushes every record (crash-safe overnight)."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(log_dir: Path, *, level: str = "INFO") -> Path:
    """Configure console + append-only file logging. Returns agent.log path.

    Writes to:
      - log_dir/agent.log              (rotating, long-lived)
      - log_dir/history/YYYY-MM-DD.log (append-only daily archive; never truncated)
    Events go to events.jsonl + history/YYYY-MM-DD.events.jsonl
    """
    log_dir = log_dir.expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    history_dir = log_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    day = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / "agent.log"
    daily_path = history_dir / f"{day}.log"

    configure_events(log_dir, daily_path=history_dir / f"{day}.events.jsonl")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rolling working copy (append; never truncate on restart).
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10_000_000,
        backupCount=20,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Immutable-ish daily archive — always append, never rotated.
    daily = _FlushFileHandler(daily_path, mode="a", encoding="utf-8")
    daily.setFormatter(fmt)
    root.addHandler(daily)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "logging ready agent=%s daily=%s events=%s level=%s",
        log_path,
        daily_path,
        log_dir / "events.jsonl",
        level.upper(),
    )
    return log_path
