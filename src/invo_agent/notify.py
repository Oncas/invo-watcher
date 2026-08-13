from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Sticky alert-style notifications (stay until dismissed). Prebuilt arm64/universal zip.
ALERTER_RELEASE = "https://github.com/vjeantet/alerter/releases/download/v26.5/alerter-26.5.zip"

_WARNED_FALLBACK = False


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _clean(value: str, limit: int) -> str:
    return re.sub(r"[\r\n\t]+", " ", value).strip()[:limit]


def alerter_local_path() -> Path:
    return Path.home() / ".invo-agent" / "bin" / "alerter"


@lru_cache(maxsize=1)
def find_alerter() -> str | None:
    """Locate alerter binary (PATH or ~/.invo-agent/bin)."""
    found = shutil.which("alerter")
    if found:
        return found
    local = alerter_local_path()
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return None


def ensure_alerter(*, download: bool = True) -> str | None:
    """Return path to alerter, optionally downloading into ~/.invo-agent/bin."""
    existing = find_alerter()
    if existing:
        return existing
    if not download:
        return None

    dest = alerter_local_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "alerter.zip"
            urllib.request.urlretrieve(ALERTER_RELEASE, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extract("alerter", path=tmp)
            binary = Path(tmp) / "alerter"
            shutil.copy2(binary, dest)
            dest.chmod(0o755)
            # Drop Gatekeeper quarantine if present.
            subprocess.run(
                ["xattr", "-cr", str(dest)],
                check=False,
                capture_output=True,
            )
        find_alerter.cache_clear()
        logger.info("Installed alerter → %s", dest)
        return str(dest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not install alerter: %s", exc)
        return None


def _notify_alerter(
    alerter: str,
    title: str,
    body: str,
    *,
    subtitle: str | None,
    sound: str | None,
    timeout: int = 0,
) -> None:
    """Alerter alert. Fire-and-forget — alerter blocks until dismiss or timeout.

    Unique --group per alert so a new event does NOT replace the previous one
    (otherwise Notification Center looks like history is overwritten).
    timeout=0 stays until dismissed; >0 auto-dismisses after that many seconds.
    """
    cmd = [
        alerter,
        "--title",
        title,
        "--message",
        body,
        "--timeout",
        str(max(0, int(timeout))),
        "--close-label",
        "Dismiss",
        "--ignore-dnd",
        "--group",
        f"invo-agent-{uuid.uuid4()}",
    ]
    if subtitle:
        cmd.extend(["--subtitle", subtitle])
    if sound:
        cmd.extend(["--sound", sound])

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _notify_osascript(
    title: str,
    body: str,
    *,
    subtitle: str | None,
    sound: str | None,
) -> None:
    parts = [
        f'display notification "{_escape_applescript(body)}"',
        f'with title "{_escape_applescript(title)}"',
    ]
    if subtitle:
        parts.append(f'subtitle "{_escape_applescript(subtitle)}"')
    if sound:
        parts.append(f'sound name "{_escape_applescript(sound)}"')
    script = " ".join(parts)
    subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def notify_mac(
    title: str,
    body: str,
    *,
    subtitle: str | None = None,
    sound: str | None = "Glass",
    enabled: bool = True,
    sticky: bool = True,
) -> None:
    """Show a macOS notification.

    sticky=True (default): use alerter so the alert stays until dismissed
    (Slack-style). Auto-downloads alerter into ~/.invo-agent/bin on first use.
    Falls back to auto-dismissing osascript banners if alerter is unavailable.
    """
    global _WARNED_FALLBACK

    if not enabled:
        return

    title = _clean(title, 80)
    body = _clean(body, 180)
    subtitle = _clean(subtitle, 80) if subtitle else None

    try:
        alerter = ensure_alerter(download=True)
        if alerter:
            _notify_alerter(
                alerter,
                title,
                body,
                subtitle=subtitle,
                sound=sound,
                timeout=0 if sticky else 10,
            )
            return
        if not _WARNED_FALLBACK:
            _WARNED_FALLBACK = True
            logger.warning(
                "Alerter unavailable — using osascript banners. "
                "Install: brew install vjeantet/tap/alerter"
            )
        _notify_osascript(title, body, subtitle=subtitle, sound=sound)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mac notification failed: %s", exc)
