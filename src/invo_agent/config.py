from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_home() -> Path:
    return Path.home() / ".invo-agent"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    invo_email: str = ""

    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"

    @field_validator("imap_password", mode="before")
    @classmethod
    def _strip_app_password_spaces(cls, value: object) -> object:
        # Gmail shows app passwords as "xxxx xxxx xxxx xxxx"
        if isinstance(value, str):
            return value.replace(" ", "").strip()
        return value

    dry_run: bool = True
    live: bool = False
    poll_interval_seconds: float = 4.0
    # Vary the polling delay by up to this many seconds in either direction.
    poll_jitter_seconds: float = 1.0
    # Fallback margin % of our equity when the source trade has no entrySize.
    position_pct: float = 5.0
    # Never allocate more than this % of our equity on one open (safety).
    max_position_pct: float = 50.0
    notification_page_size: int = 20
    min_equity_usd: float = 5.0
    min_order_usd: float = 1.0
    # Hyperliquid rejects orders with notional (margin×leverage) below this.
    hl_min_order_usd: float = 10.0

    # Missed-close startup recovery: when Involio closed offline but we still hold HL.
    recovery_enabled: bool = True
    # Adverse % from current mid for parked reduce-only SL while waiting for TP.
    recovery_sl_pct: float = 3.0

    portfolio_allowlist: str = ""

    social_api_base: str = "https://api.involio.com/v1_0"
    dex_api_base: str = "https://api.involio.com"
    origin: str = "https://app.invoapp.com"

    session_path: Path = Field(default_factory=lambda: _default_home() / "session.json")
    dex_session_path: Path = Field(default_factory=lambda: _default_home() / "dex-session.json")
    db_path: Path = Field(default_factory=lambda: _default_home() / "agent.db")
    log_dir: Path = Field(default_factory=lambda: _default_home() / "logs")

    mac_notify: bool = True
    mac_notify_sound: str = "Glass"
    # Stay until dismissed (alerter). False = auto-dismiss osascript banners.
    mac_notify_sticky: bool = True
    log_level: str = "INFO"

    @property
    def portfolio_ids(self) -> set[str]:
        raw = self.portfolio_allowlist.strip()
        if not raw:
            return set()
        return {part.strip() for part in raw.split(",") if part.strip()}

    @property
    def imap_configured(self) -> bool:
        user = self.imap_user or self.invo_email
        return bool(user and self.imap_password)


def get_settings() -> Settings:
    return Settings()
