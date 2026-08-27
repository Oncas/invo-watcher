from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from invo_agent.config import Settings
from invo_agent.db import AgentDB
from invo_agent.dex.session import (
    DexSessionStore,
    session_ttl_remaining_seconds,
)
from invo_agent.hud import HudSnapshot, collect_hud, write_hud_file

logger = logging.getLogger(__name__)

LOG_CHUNK = 65_536
HUD_REFRESH_SECONDS = 5.0

_lock = threading.Lock()
_snapshot: HudSnapshot | None = None
_settings: Settings | None = None


def current_snapshot() -> HudSnapshot | None:
    with _lock:
        return _snapshot


def _set_snapshot(snap: HudSnapshot) -> None:
    global _snapshot
    with _lock:
        _snapshot = snap


def refresh_snapshot(settings: Settings) -> HudSnapshot:
    snap = collect_hud(settings, with_pnl=True, supervisor_ok=True)
    write_hud_file(settings, snap)
    _set_snapshot(snap)
    return snap


def _hud_loop(settings: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            refresh_snapshot(settings)
        except Exception:  # noqa: BLE001
            logger.exception("hud snapshot refresh failed")
        stop.wait(HUD_REFRESH_SECONDS)


def _dex_key_state(settings: Settings) -> tuple[float | None, bool]:
    """(ttl_seconds, ok). Only gates health on a LIVE run.

    In dry-run an expired key costs nothing, so it must not fail the probe and
    train people to ignore it.
    """
    if not (settings.live and not settings.dry_run):
        return None, True
    try:
        data = DexSessionStore(settings.dex_session_path).load()
    except Exception:  # noqa: BLE001
        logger.exception("health: dex session read failed")
        return None, False
    if data is None:
        return None, False
    remaining = session_ttl_remaining_seconds(data)
    if remaining is None:
        # No expiry recorded — can't prove it's dead, don't cry wolf.
        return None, True
    return round(remaining, 1), remaining > 0


def _authorized(handler: BaseHTTPRequestHandler, token: str) -> bool:
    if not token:
        return False
    got = handler.headers.get("Authorization") or ""
    if got == f"Bearer {token}":
        return True
    qs = parse_qs(urlparse(handler.path).query)
    return (qs.get("token") or [""])[0] == token


def _json(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_log_chunk(path: Path, after: int | None) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"source": path.name, "after": 0, "next": 0, "text": "", "missing": True}
    size = path.stat().st_size
    if after is None:
        start = max(0, size - LOG_CHUNK)
    elif after > size:
        start = max(0, size - LOG_CHUNK)
    else:
        start = max(0, after)
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read(LOG_CHUNK)
    text = raw.decode("utf-8", errors="replace")
    return {
        "source": path.name,
        "after": start,
        "next": start + len(raw),
        "size": size,
        "text": text,
        "missing": False,
        "rotated": after is not None and after > size,
    }


def _journal_tail(after: str | None) -> dict[str, Any]:
    cmd = ["journalctl", "-u", "invo-agent", "-n", "200", "--no-pager", "-o", "short-iso"]
    if after:
        cmd.extend(["--since", after])
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"source": "journal", "text": "", "error": str(exc), "next": after or ""}
    text = proc.stdout or ""
    next_cursor = ""
    for line in reversed(text.splitlines()):
        # short-iso starts with 2026-08-13T...
        if len(line) >= 19 and line[4] == "-":
            next_cursor = line[:19]
            break
    return {
        "source": "journal",
        "text": text[-LOG_CHUNK:],
        "next": next_cursor or after or "",
        "error": None if proc.returncode == 0 else (proc.stderr or "")[:200],
    }


def _make_handler(settings: Settings) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            logger.debug("hud-http " + fmt, *args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not _authorized(self, settings.hud_token):
                _json(self, 401, {"error": "unauthorized"})
                return
            qs = parse_qs(parsed.query)
            if parsed.path in ("/v1/health", "/health"):
                self._health()
                return
            if parsed.path == "/v1/hud":
                self._hud()
                return
            if parsed.path == "/v1/events":
                self._events(qs)
                return
            if parsed.path == "/v1/logs":
                self._logs(qs)
                return
            _json(self, 404, {"error": "not found"})

        def _health(self) -> None:
            snap = current_snapshot()
            last_poll = None
            age = None
            if snap is not None:
                last_poll = snap.last_poll_at
                age = snap.poll_age_seconds
            else:
                db = AgentDB(settings.db_path)
                last_poll = db.get_meta("last_poll_at")
                db.close()
                poll_dt = None
                if last_poll:
                    try:
                        poll_dt = datetime.fromisoformat(last_poll)
                    except ValueError:
                        poll_dt = None
                    if poll_dt is not None and poll_dt.tzinfo is None:
                        poll_dt = poll_dt.replace(tzinfo=UTC)
                if poll_dt is not None:
                    age = (datetime.now(UTC) - poll_dt.astimezone(UTC)).total_seconds()
            stale_after = max(30.0, settings.poll_interval_seconds * 4)
            poll_ok = age is not None and age <= stale_after
            # A live agent polling happily with a dead signing key cannot open
            # OR close. That has to fail the probe, not read as healthy.
            dex_ttl, dex_ok = _dex_key_state(settings)
            ok = poll_ok and dex_ok
            reason = None
            if not poll_ok:
                reason = "poll_stale"
            elif not dex_ok:
                reason = "dex_key_expired"
            _json(
                self,
                200 if ok else 503,
                {
                    "ok": ok,
                    "last_poll_at": last_poll,
                    "poll_age_seconds": age,
                    "stale_after": stale_after,
                    "poll_ok": poll_ok,
                    "dex_key_ok": dex_ok,
                    "dex_key_ttl_seconds": dex_ttl,
                    "reason": reason,
                },
            )

        def _hud(self) -> None:
            snap = current_snapshot()
            if snap is None:
                try:
                    snap = refresh_snapshot(settings)
                except Exception as exc:  # noqa: BLE001
                    _json(self, 503, {"error": str(exc)})
                    return
            payload = asdict(snap)
            payload["updated_at"] = datetime.now(UTC).isoformat()
            _json(self, 200, payload)

        def _events(self, qs: dict[str, list[str]]) -> None:
            try:
                after_id = int((qs.get("after_id") or ["0"])[0])
            except ValueError:
                after_id = 0
            db = AgentDB(settings.db_path)
            rows = db.actions_after(after_id, limit=50)
            db.close()
            _json(
                self,
                200,
                {
                    "events": [
                        {
                            "id": r.id,
                            "created_at": r.created_at,
                            "action": r.action,
                            "summary": r.summary or "",
                            "notification_id": r.notification_id,
                            "dry_run": r.dry_run,
                        }
                        for r in rows
                    ]
                },
            )

        def _logs(self, qs: dict[str, list[str]]) -> None:
            source = (qs.get("source") or ["agent"])[0]
            after_raw = (qs.get("after") or [""])[0]
            log_dir = settings.log_dir.expanduser()
            if source == "journal":
                _json(self, 200, _journal_tail(after_raw or None))
                return
            path = log_dir / ("events.jsonl" if source == "events" else "agent.log")
            after: int | None
            if after_raw == "":
                after = None
            else:
                try:
                    after = int(after_raw)
                except ValueError:
                    after = None
            payload = _read_log_chunk(path, after)
            payload["source"] = source
            _json(self, 200, payload)

    return Handler


def start_hud_server(settings: Settings) -> tuple[ThreadingHTTPServer, threading.Event]:
    """Start snapshot refresher + HTTP API. Returns (httpd, stop_event)."""
    global _settings
    _settings = settings
    stop = threading.Event()
    try:
        refresh_snapshot(settings)
    except Exception:  # noqa: BLE001
        logger.exception("initial hud snapshot failed")
    threading.Thread(target=_hud_loop, args=(settings, stop), name="hud-refresh", daemon=True).start()
    handler = _make_handler(settings)
    httpd = ThreadingHTTPServer((settings.hud_bind, settings.hud_port), handler)
    threading.Thread(target=httpd.serve_forever, name="hud-http", daemon=True).start()
    logger.info("hud API listening on %s:%s", settings.hud_bind, settings.hud_port)
    return httpd, stop


def serve_forever(settings: Settings) -> None:
    """Block serving the HUD API (used if run without the poller)."""
    httpd, stop = start_hud_server(settings)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        httpd.shutdown()
