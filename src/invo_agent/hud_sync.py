from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from invo_agent.config import Settings
from invo_agent.hud import hud_path
from invo_agent.notify import notify_mac

logger = logging.getLogger(__name__)

NOTIFY_EXACT = frozenset({"OPEN", "UPDATE", "CLOSE"})
NOTIFY_PREFIXES = ("ERROR_", "SKIP_", "RECOVERY_")


def cursor_path(settings: Settings) -> Path:
    return settings.session_path.expanduser().parent / "hud-cursor"


def _load_cursor(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"after_id": None, "logs": {}}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"after_id": None, "logs": {}}
    if not isinstance(data, dict):
        return {"after_id": None, "logs": {}}
    logs = data.get("logs")
    if not isinstance(logs, dict):
        data["logs"] = {}
    return data


def _save_cursor(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _should_notify(action: str) -> bool:
    if action in NOTIFY_EXACT:
        return True
    return any(action.startswith(prefix) for prefix in NOTIFY_PREFIXES)


def _notify_event(settings: Settings, event: dict[str, Any]) -> None:
    action = str(event.get("action") or "")
    if not _should_notify(action):
        return
    summary = str(event.get("summary") or action)
    dry_run = bool(event.get("dry_run"))
    mode = "DRY-RUN" if dry_run else "LIVE"
    if action.startswith("ERROR_"):
        notify_mac(
            "invo-agent · ERROR",
            summary,
            subtitle=action,
            sound="Basso",
            enabled=settings.mac_notify,
            sticky=settings.mac_notify_sticky,
        )
        return
    if action.startswith("SKIP_"):
        notify_mac(
            "invo-agent · skipped",
            summary,
            subtitle=action,
            sound=None,
            enabled=settings.mac_notify,
            sticky=False,
        )
        return
    notify_mac(
        f"invo-agent · {action}",
        summary,
        subtitle=mode,
        sound=settings.mac_notify_sound or None,
        enabled=settings.mac_notify,
        sticky=settings.mac_notify_sticky,
    )


class HudClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.hud_sync_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {settings.hud_token}"}
        self._client = httpx.Client(timeout=2.5, headers=self.headers)

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.get(f"{self.base}{path}", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"unexpected payload from {path}")
        return data


def _sync_hud(client: HudClient, settings: Settings) -> None:
    payload = client.get_json("/v1/hud")
    path = hud_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


def _sync_events(
    client: HudClient,
    settings: Settings,
    cursor: dict[str, Any],
) -> None:
    after_id = cursor.get("after_id")
    seeding = after_id is None
    current = 0 if seeding else int(after_id)
    while True:
        payload = client.get_json("/v1/events", {"after_id": current})
        events = payload.get("events") or []
        if not events:
            break
        for event in events:
            if not seeding:
                _notify_event(settings, event)
            current = int(event["id"])
        if len(events) < 50:
            break
    cursor["after_id"] = current


def _append_text(path: Path, text: str) -> None:
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _sync_logs(
    client: HudClient,
    settings: Settings,
    cursor: dict[str, Any],
) -> None:
    log_dir = settings.log_dir.expanduser()
    logs = cursor.setdefault("logs", {})
    if not isinstance(logs, dict):
        logs = {}
        cursor["logs"] = logs
    mapping = (
        ("agent", log_dir / "agent.log"),
        ("events", log_dir / "events.jsonl"),
    )
    for source, dest in mapping:
        params: dict[str, Any] = {"source": source}
        after = logs.get(source)
        if after is not None:
            params["after"] = after
        payload = client.get_json("/v1/logs", params)
        _append_text(dest, str(payload.get("text") or ""))
        nxt = payload.get("next")
        if nxt is not None:
            logs[source] = nxt


def sync_once(client: HudClient, settings: Settings, cursor: dict[str, Any]) -> None:
    _sync_hud(client, settings)
    _sync_events(client, settings, cursor)
    try:
        _sync_logs(client, settings, cursor)
    except Exception:  # noqa: BLE001
        logger.exception("hud-sync log tail failed")


def run_forever(settings: Settings, *, stop_fn: Any | None = None) -> None:
    if not settings.hud_sync_url:
        raise RuntimeError("HUD_SYNC_URL is required for hud-sync")
    if not settings.hud_token:
        raise RuntimeError("HUD_TOKEN is required for hud-sync")

    path = cursor_path(settings)
    cursor = _load_cursor(path)
    client = HudClient(settings)
    interval = max(0.2, settings.hud_sync_interval_seconds)
    logger.info("hud-sync polling %s every %ss", settings.hud_sync_url, interval)
    try:
        while True:
            if stop_fn and stop_fn():
                break
            try:
                sync_once(client, settings, cursor)
                _save_cursor(path, cursor)
            except Exception:  # noqa: BLE001
                logger.exception("hud-sync poll failed")
            time.sleep(interval)
    finally:
        client.close()
