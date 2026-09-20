"""Copy the VPS's dated log history to the laptop, which keeps it for good."""
from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Iterable
from pathlib import Path

import typer

from invo_agent.config import get_settings
from invo_agent.events import today_stamp

logs_app = typer.Typer(no_args_is_help=True, help="Pull operational logs off the VPS.")

# Only dated history is ever copied. `agent.log` and `events.jsonl` are live.
_DATED = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.(log|events\.jsonl)$")


def plan_transfers(names: Iterable[str], dest: Path, *, today: str) -> list[str]:
    """Which remote files are worth fetching.

    Today's file is skipped because it is still being appended to; every older
    dated file is closed for good once its day ended, so "already present
    locally" is a sufficient reason to skip it.
    """
    dest = Path(dest).expanduser()
    wanted = []
    for name in names:
        match = _DATED.match(name)
        if not match:
            continue
        day = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if day >= today:
            continue
        if (dest / name).exists():
            continue
        wanted.append(name)
    return sorted(wanted)


def _run(args: list[str]) -> str:
    done = subprocess.run(args, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip() or f"{args[0]} failed")
    return done.stdout


@logs_app.command("pull")
def pull(
    remote: str = typer.Option(..., "--remote", help="user@host of the VPS"),
    dest: Path | None = typer.Option(None, "--dest", help="Local directory for the history"),
    remote_dir: str = typer.Option(
        # The deployed LOG_DIR, not the code's ~/.invo-agent default: the VPS
        # sets LOG_DIR=/var/lib/invo-agent/logs and the home path is empty there.
        "/var/lib/invo-agent/logs/history", "--remote-dir",
        help="History directory on the VPS",
    ),
) -> None:
    """Copy dated history files that are not already held locally."""
    target = (dest or (get_settings().log_dir / "pulled")).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    try:
        quoted = shlex.quote(remote_dir)
        listing = _run(["ssh", remote, f"ls -1 {quoted} 2>/dev/null || true"])
        wanted = plan_transfers(listing.split(), target, today=today_stamp())
        for name in wanted:
            _run(["scp", "-q", f"{remote}:{remote_dir}/{name}", str(target / name)])
    except (RuntimeError, OSError) as exc:
        typer.secho(f"logs pull failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"pulled {len(wanted)} file(s) into {target}")
