from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from invo_agent.events import configure_events, today_stamp

# Live files stay small; dated history is deleted after this many days.
LOG_RETAIN_DAYS = 7
LOG_EVENTS_CAP_BYTES = 8_000_000
LOG_MIN_FREE_MB = 400


class _FlushFileHandler(logging.FileHandler):
    """Append-only file handler that flushes every record (crash-safe overnight)."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


class DatedFileHandler(logging.Handler):
    """Append to ``history/<today>.log``, following the date across midnight.

    ``TimedRotatingFileHandler`` cannot do this: it keeps one fixed filename and
    *renames* it on rollover, so the live file would be ``agent-daily.log`` and
    the archive ``agent-daily.log.2026-09-19``. The sync depends on the opposite
    — a file named for its own day, closed for good once that day ends, which is
    what makes "already present locally" a safe reason to skip it.
    """

    def __init__(
        self,
        history_dir: Path,
        *,
        suffix: str = ".log",
        now: Callable[[], datetime] | None = None,
    ):
        super().__init__()
        self.history_dir = Path(history_dir).expanduser()
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.suffix = suffix
        self._now = now or datetime.now
        self._day: str | None = None
        self._stream: TextIO | None = None

    def _target(self, day: str) -> Path:
        return self.history_dir / f"{day}{self.suffix}"

    def _stream_for(self, day: str) -> TextIO:
        if self._day == day and self._stream is not None:
            return self._stream
        if self._stream is not None:
            self._stream.close()
        # Always append: a restart within the same day must not truncate it.
        self._stream = self._target(day).open("a", encoding="utf-8")
        self._day = day
        return self._stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self._stream_for(self._now().strftime("%Y-%m-%d"))
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
                self._day = None
        finally:
            super().close()


def setup_logging(log_dir: Path, *, level: str = "INFO") -> Path:
    """Configure console + append-only file logging. Returns agent.log path.

    Writes to:
      - log_dir/agent.log              (rotating, long-lived)
      - log_dir/history/YYYY-MM-DD.log (daily archive; pruned by retain_days)
    Events go to events.jsonl + history/YYYY-MM-DD.events.jsonl
    """
    log_dir = log_dir.expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    history_dir = log_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / "agent.log"

    # No dated path is pinned here: both sinks resolve today's file per write,
    # so a process that outlives midnight rolls over on its own.
    configure_events(log_dir)

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
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Dated archive: one file per day, closed for good when the day ends.
    daily = DatedFileHandler(history_dir)
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
        history_dir / f"{today_stamp()}.log",
        log_dir / "events.jsonl",
        level.upper(),
    )
    return log_path


def _tail_file(path: Path, cap_bytes: int) -> bool:
    """Keep the last ``cap_bytes`` of an append-only file. Returns True if trimmed."""
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= cap_bytes:
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    keep = data[-cap_bytes:]
    nl = keep.find(b"\n")
    if nl >= 0:
        keep = keep[nl + 1 :]
    path.write_bytes(keep)
    return True


def prune_logs(
    log_dir: Path,
    *,
    retain_days: int = LOG_RETAIN_DAYS,
    events_cap_bytes: int = LOG_EVENTS_CAP_BYTES,
    min_free_mb: int = LOG_MIN_FREE_MB,
) -> dict[str, int]:
    """Drop old history and cap events.jsonl. Emergency-trim if the disk is low."""
    log_dir = log_dir.expanduser()
    history = log_dir / "history"
    deleted = 0
    trimmed = 0
    cutoff = datetime.now(UTC) - timedelta(days=max(1, int(retain_days)))
    if history.is_dir():
        for path in history.iterdir():
            if not path.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError:
                continue
    for backup in log_dir.glob("agent.log.*"):
        if backup.is_file() and backup.suffix.lstrip(".").isdigit():
            try:
                idx = int(backup.suffix.lstrip("."))
            except ValueError:
                continue
            if idx > 3:
                try:
                    backup.unlink()
                    deleted += 1
                except OSError:
                    continue
    if _tail_file(log_dir / "events.jsonl", events_cap_bytes):
        trimmed += 1

    try:
        free_mb = shutil.disk_usage(log_dir).free / (1024 * 1024)
    except OSError:
        free_mb = float(min_free_mb)
    if free_mb < min_free_mb and history.is_dir():
        aged = sorted(
            (p for p in history.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        for path in aged:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                continue
            try:
                free_mb = shutil.disk_usage(log_dir).free / (1024 * 1024)
            except OSError:
                break
            if free_mb >= min_free_mb:
                break
        if _tail_file(log_dir / "events.jsonl", min(1_000_000, events_cap_bytes)):
            trimmed += 1
        for backup in log_dir.glob("agent.log.*"):
            if backup.is_file():
                try:
                    backup.unlink()
                    deleted += 1
                except OSError:
                    continue
    return {"deleted": deleted, "trimmed": trimmed}
