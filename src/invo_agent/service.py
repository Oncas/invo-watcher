from __future__ import annotations

import subprocess
from pathlib import Path

LABEL = "com.invo-agent"
PLIST_NAME = f"{LABEL}.plist"


def project_root() -> Path:
    # src/invo_agent/service.py → repo root
    return Path(__file__).resolve().parents[2]


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return launch_agents_dir() / PLIST_NAME


def render_plist(
    *,
    mode: str,
    working_directory: Path,
    wrapper: Path,
    log_dir: Path,
) -> str:
    """mode: dry-run | run"""
    cmd = "dry-run" if mode == "dry-run" else "run"
    log_dir = log_dir.expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Wrapper appends stdout/stderr itself. Do NOT set StandardOutPath —
    # launchd truncates those files on every restart and wipes history.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>WorkingDirectory</key>
  <string>{working_directory}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{wrapper}</string>
    <string>{cmd}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>INVO_AGENT_LOG_DIR</key>
    <string>{log_dir}</string>
  </dict>
</dict>
</plist>
"""


def install_launchd(*, mode: str, log_dir: Path) -> Path:
    root = project_root()
    wrapper = (root / "scripts" / "launchd-run.sh").absolute()
    if not wrapper.exists():
        raise RuntimeError(f"Missing launchd wrapper: {wrapper}")
    wrapper.chmod(0o755)

    launch_agents_dir().mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.write_text(
        render_plist(
            mode=mode,
            working_directory=root.absolute(),
            wrapper=wrapper,
            log_dir=log_dir,
        )
    )

    # Reload cleanly
    subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
    result = subprocess.run(
        ["launchctl", "load", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "launchctl load failed")
    return path


def uninstall_launchd() -> Path | None:
    path = plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
        path.unlink(missing_ok=True)
        return path
    subprocess.run(["launchctl", "remove", LABEL], check=False, capture_output=True)
    return None


def launchd_running() -> bool:
    result = subprocess.run(
        ["launchctl", "list"],
        check=False,
        capture_output=True,
        text=True,
    )
    return LABEL in (result.stdout or "")
