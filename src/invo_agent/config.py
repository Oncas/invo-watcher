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
    # Never allocate more than this % of our equity as margin on one copy.
    max_position_pct: float = 10.0
    # Sum of open-copy margins + this order cannot exceed this % of equity.
    max_book_margin_pct: float = 40.0
    notification_page_size: int = 20
    min_equity_usd: float = 5.0
    # Hyperliquid rejects orders with notional (margin×leverage) below this.
    hl_min_order_usd: float = 10.0

    # Missed-close startup recovery: when Involio closed offline but we still hold HL.
    recovery_enabled: bool = True
    # Parked reduce-only SL: ROE / margin % (divided by leverage → price).
    recovery_sl_pct: float = 3.0
    # Background Turnkey renew when remaining TTL is below this (capped to 25% of issued TTL).
    dex_renew_before_seconds: float = 6 * 3600
    # Drop queued opens the trader never closed after this many seconds.
    pending_max_age_seconds: float = 24 * 3600
    # Keep this many request slots for closes / protective TP/SL.
    hl_budget_reserve: float = 50.0
    # Temporary: allow queued opens/adds while over the address cap so fills
    # can earn the limit back. Still waits ~11s between posts. Off by default.
    hl_budget_allow_deficit: bool = False
    # How often to refresh HL userFees (info, not an order).
    hl_fees_refresh_seconds: float = 60.0
    # Re-assert coin leverage if our cache is older than this.
    hl_leverage_stale_seconds: float = 3 * 3600

    # Fallback / seed list. Live runs refresh this from followed portfolios.
    portfolio_allowlist: str = ""
    # Pull the allowlist from the Involio account's followed portfolios.
    portfolio_sync: bool = True
    portfolio_sync_interval_seconds: float = 900.0

    social_api_base: str = "https://api.involio.com/v1_0"
    dex_api_base: str = "https://api.involio.com"
    origin: str = "https://app.invoapp.com"

    session_path: Path = Field(default_factory=lambda: _default_home() / "session.json")
    dex_session_path: Path = Field(default_factory=lambda: _default_home() / "dex-session.json")
    db_path: Path = Field(default_factory=lambda: _default_home() / "agent.db")
    log_dir: Path = Field(default_factory=lambda: _default_home() / "logs")
    research_enabled: bool = True
    research_db_path: Path = Field(default_factory=lambda: _default_home() / "research.db")

    mac_notify: bool = True
    # Empty = silent banners (still shown). Set Glass/Basso to restore a sound.
    mac_notify_sound: str = ""
    # Stay until dismissed (alerter). False = auto-dismiss osascript banners.
    mac_notify_sticky: bool = True
    log_level: str = "INFO"

    # Snapshot HTTP API (VPS). Empty token = server disabled.
    hud_token: str = ""
    hud_bind: str = "127.0.0.1"
    hud_port: int = 8787

    # Mac sidecar: poll this base URL (e.g. http://invo-vps:8787).
    hud_sync_url: str = ""
    hud_sync_interval_seconds: float = 1.0

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
