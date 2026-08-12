from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from rich.console import Console

from invo_agent.api.client import InvolioClient
from invo_agent.auth.inbox import InboxError, wait_for_otp
from invo_agent.auth.session import SessionData, SessionStore
from invo_agent.config import Settings

console = Console()


class LoginError(RuntimeError):
    pass


def start_login(client: InvolioClient, email: str) -> None:
    result = client.login_email_start(email)
    if not result.success:
        raise LoginError(f"login/email/start failed: {result.error}")


def complete_login(
    client: InvolioClient,
    session: SessionStore,
    settings: Settings,
    *,
    email: str,
    code: str,
) -> SessionData:
    del settings  # reserved for future device metadata
    device_id = session.get_or_create_device_id()
    # Match Flutter web: deviceType/deviceInfo are null (not "invo-agent").
    result = client.login_email(
        email=email,
        code=code.strip(),
        device_id=device_id,
        device_type=None,
        device_info=None,
    )
    if not result.access_token or not result.refresh_token:
        raise LoginError(f"login/email failed: {result.error}")

    data = SessionData(
        email=email,
        device_id=device_id,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        user_id=result.user_id,
        username=result.username,
        onboarded=result.onboarded,
    )
    session.save(data)
    return data


def login_with_otp(
    client: InvolioClient,
    session: SessionStore,
    settings: Settings,
    *,
    email: str,
    code: str | None = None,
    code_prompt: Callable[[], str] | None = None,
) -> SessionData:
    """Start OTP login, obtain code via arg / IMAP / prompt, complete login."""
    started_at = datetime.now(UTC)
    console.print(f"[dim]Requesting OTP for {email}...[/dim]")
    start_login(client, email)
    console.print("[green]OTP sent.[/green]")

    if code:
        return complete_login(client, session, settings, email=email, code=code)

    if settings.imap_configured:
        console.print(
            "[dim]Waiting for Involio account OTP "
            "(from no-reply@mail.involio.com)...[/dim]"
        )
        try:
            code = wait_for_otp(settings, kind="account", not_before=started_at)
            console.print(f"[green]Account OTP found:[/green] {code}")
            return complete_login(client, session, settings, email=email, code=code)
        except InboxError as exc:
            console.print(f"[yellow]IMAP failed:[/yellow] {exc}")
            if code_prompt is None:
                raise LoginError(str(exc)) from exc

    if code_prompt is None:
        raise LoginError("No OTP code provided and IMAP unavailable")

    code = code_prompt()
    if not code:
        raise LoginError("Empty OTP code")
    return complete_login(client, session, settings, email=email, code=code)
