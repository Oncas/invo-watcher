"""Hyperliquid address-based request budget.

``limit = 10_000 + 1 per USDC of lifetime traded volume``. Actions (orders,
cancels, leverage) cost 1 each; a batched order of N legs costs N. Info reads
are free. The limit never resets on a clock — only fills raise it.

We persist daily volume so a rolling ``userFees`` window cannot forget older
days, and we re-anchor ``sent`` from HL's rejection string whenever we hit it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from invo_agent.db import AgentDB

logger = logging.getLogger(__name__)

INITIAL_BUFFER = 10_000.0
DEFAULT_RESERVE = 50.0
NORMAL_GAP_SECONDS = 0.25
DEFICIT_GAP_SECONDS = 11.0

META_SENT = "hl_sent"
META_SENT_AT = "hl_sent_at"
META_HL_LIMIT = "hl_reported_limit"
META_HL_VOLUME = "hl_reported_volume"
META_FEES_AT = "hl_fees_refreshed_at"

_RATE_LIMIT_RE = re.compile(
    r"too many cumulative requests sent\s*\(\s*(?P<sent>\d+)\s*>\s*(?P<limit>\d+)\s*\)"
    r"(?:\s+for cumulative volume traded\s*\$?(?P<volume>[\d.]+))?",
    re.IGNORECASE,
)


class BudgetDeniedError(RuntimeError):
    """Opportunistic action refused so closes keep their reserve."""

    pass


@dataclass(frozen=True)
class RateLimitHit:
    sent: int
    limit: int
    volume: float | None


def parse_rate_limit_message(msg: str) -> RateLimitHit | None:
    match = _RATE_LIMIT_RE.search(msg or "")
    if match is None:
        return None
    volume = None
    if match.group("volume"):
        try:
            volume = float(match.group("volume"))
        except ValueError:
            volume = None
    return RateLimitHit(
        sent=int(match.group("sent")),
        limit=int(match.group("limit")),
        volume=volume,
    )


def daily_volume_from_user_fees(payload: dict) -> dict[str, float]:
    """``{YYYY-MM-DD: usd}`` from an HL ``userFees`` info response."""
    out: dict[str, float] = {}
    for row in payload.get("dailyUserVlm") or []:
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or "")
        if not date:
            continue
        try:
            usd = float(row.get("userCross") or 0) + float(row.get("userAdd") or 0)
        except (TypeError, ValueError):
            continue
        out[date] = usd
    return out


def action_leg_count(action: dict) -> int:
    typ = action.get("type")
    if typ == "order":
        return max(1, len(action.get("orders") or []))
    if typ == "cancel":
        return max(1, len(action.get("cancels") or []))
    return 1


class RequestBudget:
    def __init__(
        self,
        db: AgentDB,
        *,
        reserve: float = DEFAULT_RESERVE,
        normal_gap: float = NORMAL_GAP_SECONDS,
        deficit_gap: float = DEFICIT_GAP_SECONDS,
    ):
        self.db = db
        self.reserve = float(reserve)
        self.normal_gap = float(normal_gap)
        self.deficit_gap = float(deficit_gap)
        self._last_post = 0.0

    def record_daily_volume(self, days: dict[str, float]) -> None:
        if days:
            self.db.upsert_hl_daily_volume(days)
            self.db.set_meta(META_FEES_AT, str(time.time()))

    def fees_age_seconds(self) -> float | None:
        raw = self.db.get_meta(META_FEES_AT)
        if not raw:
            return None
        try:
            return max(0.0, time.time() - float(raw))
        except ValueError:
            return None

    def lifetime_volume(self) -> float:
        return self.db.hl_lifetime_volume()

    def limit(self) -> float:
        return INITIAL_BUFFER + self.lifetime_volume()

    def sent(self) -> float:
        raw = self.db.get_meta(META_SENT)
        if not raw:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def remaining(self) -> float:
        return self.limit() - self.sent()

    def in_deficit(self) -> bool:
        return self.remaining() <= 0

    def can_spend(self, legs: int, *, essential: bool) -> bool:
        if essential:
            return True
        return self.remaining() - max(1, int(legs)) > self.reserve

    def wait_if_needed(self) -> None:
        gap = self.deficit_gap if self.in_deficit() else self.normal_gap
        if gap <= 0 or self._last_post <= 0:
            return
        elapsed = time.monotonic() - self._last_post
        if elapsed < gap:
            time.sleep(gap - elapsed)

    def note_posted(self, legs: int, *, accepted: bool) -> None:
        self._last_post = time.monotonic()
        # Rejections do not increment HL's counter (measured live).
        if not accepted:
            return
        n = max(1, int(legs))
        self.db.set_meta(META_SENT, str(self.sent() + n))
        self.db.set_meta(META_SENT_AT, str(time.time()))

    def reanchor(self, hit: RateLimitHit) -> None:
        prior = self.sent()
        self.db.set_meta(META_SENT, str(hit.sent))
        self.db.set_meta(META_SENT_AT, str(time.time()))
        self.db.set_meta(META_HL_LIMIT, str(hit.limit))
        if hit.volume is not None:
            self.db.set_meta(META_HL_VOLUME, str(hit.volume))
        drift = prior - hit.sent if prior else 0.0
        logger.warning(
            "HL budget re-anchored sent=%s (was %.0f, drift=%+.0f) limit=%s volume=%s",
            hit.sent,
            prior,
            drift,
            hit.limit,
            hit.volume,
        )

    def snapshot(self) -> dict[str, float | bool]:
        return {
            "limit": self.limit(),
            "sent": self.sent(),
            "remaining": self.remaining(),
            "volume": self.lifetime_volume(),
            "in_deficit": self.in_deficit(),
            "reserve": self.reserve,
        }
