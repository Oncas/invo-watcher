from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from invo_agent.config import Settings
from invo_agent.db import AgentDB, FollowedPortfolio
from invo_agent.events import log_event

logger = logging.getLogger(__name__)

SYNCED_AT_META = "portfolio_allowlist_synced_at"

# Response containers we accept for the followed-portfolios list.
# Involio returns "savedPortfolios"; the rest are defensive.
_LIST_KEYS = (
    "savedPortfolios",
    "items",
    "portfolios",
    "results",
    "data",
    "followedPortfolios",
)
# Per-item keys that carry the portfolio id itself.
_ID_KEYS = ("id", "portfolioId", "portfolio_id")
# Per-item keys whose value is a nested portfolio object.
_NESTED_KEYS = ("portfolio", "followedPortfolio")
# Where the portfolio's display name lives.
_TITLE_KEYS = ("title", "name", "portfolioName", "portfolioTitle")
# Where the portfolio manager lives (nested object, then flat fallbacks).
_MANAGER_KEYS = ("user", "owner", "creator", "manager", "portfolioOwner")
_USERNAME_KEYS = ("username", "userName", "handle")
_FULL_NAME_KEYS = ("name", "fullName", "displayName")

_MAX_PAGES = 10
_PAGE_SIZE = 50


class FollowedPortfolioSource(Protocol):
    def get_followed_portfolios(
        self, user_id: UUID | str, *, page: int = 1, size: int = 50
    ) -> dict[str, Any]: ...


def _as_uuid_str(value: object) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def _items_from(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        # Some endpoints nest one more level: {"data": {"items": [...]}}
        if isinstance(value, dict):
            nested = _items_from(value)
            if nested:
                return nested
    return []


def _first_str(source: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _manager_from(item: dict) -> tuple[str | None, str | None, str | None]:
    """(username, full name, user id) of whoever runs this portfolio."""
    for key in _MANAGER_KEYS:
        nested = item.get(key)
        if isinstance(nested, dict):
            return (
                _first_str(nested, _USERNAME_KEYS),
                _first_str(nested, _FULL_NAME_KEYS),
                _as_uuid_str(nested.get("id")),
            )
    # Flat variants: {"username": ..., "userId": ...} on the item itself.
    return (
        _first_str(item, _USERNAME_KEYS),
        _first_str(item, ("ownerName", "userFullName")),
        _as_uuid_str(item.get("userId") or item.get("ownerId")),
    )


def extract_portfolios(payload: Any) -> list[FollowedPortfolio]:
    """Followed portfolios — id, title, and manager — from an API response.

    Id lookup is deliberately narrow: only id fields on the item (or on a
    nested ``portfolio`` object) count, so a portfolio manager's *user* id
    never leaks into the allowlist.
    """
    found: dict[str, FollowedPortfolio] = {}
    for item in _items_from(payload):
        if isinstance(item, str):
            pid = _as_uuid_str(item)
            if pid:
                found.setdefault(pid, FollowedPortfolio(portfolio_id=pid))
            continue
        if not isinstance(item, dict):
            continue

        source = item
        for key in _NESTED_KEYS:
            nested = item.get(key)
            if isinstance(nested, dict) and any(k in nested for k in _ID_KEYS):
                source = nested
                break

        pid = None
        for key in _ID_KEYS:
            pid = _as_uuid_str(source.get(key))
            if pid:
                break
        if not pid:
            continue

        # Manager can sit on the wrapper (a follow row) or on the portfolio.
        # Merge field by field: the wrapper fills gaps, it never overwrites what
        # the portfolio object already told us.
        manager, manager_name, manager_id = _manager_from(source)
        if source is not item and not (manager and manager_name and manager_id):
            alt_manager, alt_name, alt_id = _manager_from(item)
            manager = manager or alt_manager
            manager_name = manager_name or alt_name
            manager_id = manager_id or alt_id

        found[pid] = FollowedPortfolio(
            portfolio_id=pid,
            title=_first_str(source, _TITLE_KEYS) or _first_str(item, _TITLE_KEYS),
            manager=manager,
            manager_name=manager_name,
            manager_id=manager_id,
        )
    return list(found.values())


def extract_portfolio_ids(payload: Any) -> set[str]:
    """Just the portfolio UUIDs — see :func:`extract_portfolios`."""
    return {p.portfolio_id for p in extract_portfolios(payload)}


class PortfolioAllowlist:
    """Which portfolios we copy: followed portfolios, with ``.env`` as fallback.

    Never narrows to empty on failure — an empty allowlist means "copy every
    trade notification", so a bad sync must keep the last known good list.
    When there is no list to keep, :attr:`usable` goes False so the caller
    stops copying instead of copying everyone.
    """

    def __init__(
        self,
        settings: Settings,
        client: FollowedPortfolioSource | None = None,
        *,
        db: AgentDB | None = None,
        now: Callable[[], float] | None = None,
        user_id: Callable[[], str | None] | None = None,
    ):
        self.settings = settings
        self.client = client
        self.db = db
        self._now = now or time.monotonic
        self._user_id_fn = user_id
        self._configured = frozenset(settings.portfolio_ids)
        self._synced: dict[str, FollowedPortfolio] | None = self._load_cached()
        self._last_attempt: float | None = None
        self.last_error: str | None = None

    # --- state ---

    @property
    def ids(self) -> set[str]:
        if self._synced is not None:
            return set(self._synced)
        return set(self._configured)

    @property
    def source(self) -> str:
        if self._synced is not None:
            return "followed"
        return "env" if self._configured else "all"

    @property
    def usable(self) -> bool:
        """False when we tried to learn the follows and came back empty-handed.

        An empty allowlist means "copy every trade notification", so a sync we
        attempted and could not complete has to stop copying rather than widen
        it to every trader on Involio. Never having attempted a sync is the old
        documented "no allowlist = copy all" case, and stays permissive.
        """
        if self.ids:
            return True
        return self._last_attempt is None or not self.settings.portfolio_sync

    @property
    def portfolios(self) -> list[FollowedPortfolio]:
        """Known follows, newest sync first — empty while running off ``.env``."""
        if self._synced is None:
            return []
        return sorted(
            self._synced.values(),
            key=lambda p: ((p.manager or "~").lower(), (p.title or "").lower()),
        )

    def label(self, portfolio_id: str | None) -> str:
        """`@manager · Title` for a portfolio id, or the id when unknown."""
        if not portfolio_id:
            return "?"
        record = (self._synced or {}).get(str(portfolio_id))
        return record.label if record else str(portfolio_id)

    def describe(self, limit: int | None = None) -> str:
        if not self.ids:
            return "all trade notifs"
        names = [self.label(pid) for pid in sorted(self.ids)]
        shown = names if limit is None else names[:limit]
        more = "" if len(shown) == len(names) else f" (+{len(names) - len(shown)})"
        return f"{', '.join(shown)}{more} [{self.source}]"

    def _load_cached(self) -> dict[str, FollowedPortfolio] | None:
        """Last good sync from a previous run, so a restart mid-outage holds."""
        if self.db is None or not self.settings.portfolio_sync:
            return None
        try:
            rows = self.db.list_followed_portfolios()
        except Exception as exc:  # noqa: BLE001 — cache is best-effort
            logger.warning("allowlist cache read failed: %s", exc)
            return None
        return {row.portfolio_id: row for row in rows} or None

    def _store_cached(self, records: dict[str, FollowedPortfolio]) -> None:
        if self.db is None:
            return
        try:
            self.db.replace_followed_portfolios(list(records.values()))
            self.db.set_meta(SYNCED_AT_META, datetime.now(UTC).isoformat())
        except Exception as exc:  # noqa: BLE001 — cache is best-effort
            logger.warning("allowlist cache write failed: %s", exc)

    # --- sync ---

    def _resolve_user_id(self) -> str | None:
        if self._user_id_fn is not None:
            return self._user_id_fn()
        session = getattr(self.client, "session", None)
        data = session.load() if session is not None else None
        user_id = getattr(data, "user_id", None)
        return str(user_id) if user_id else None

    def _fetch(self, user_id: str) -> dict[str, FollowedPortfolio]:
        found: dict[str, FollowedPortfolio] = {}
        for page in range(1, _MAX_PAGES + 1):
            payload = self.client.get_followed_portfolios(  # type: ignore[union-attr]
                user_id, page=page, size=_PAGE_SIZE
            )
            items = _items_from(payload)
            for record in extract_portfolios(items):
                found[record.portfolio_id] = record
            if len(items) < _PAGE_SIZE:
                break
        return found

    def maybe_refresh(self) -> bool:
        """Refresh if sync is on and the interval has elapsed. True if changed."""
        if not self.settings.portfolio_sync:
            return False
        interval = max(60.0, float(self.settings.portfolio_sync_interval_seconds))
        now = self._now()
        if self._last_attempt is not None and now - self._last_attempt < interval:
            return False
        return self.refresh()

    def _kept(self) -> str:
        """How to describe what we fall back to, for the failure logs."""
        return self.source if self.ids else "no list — copying paused"

    def _warn_if_widened(
        self, previous_ids: set[str], fresh: dict[str, FollowedPortfolio]
    ) -> None:
        """Say so out loud when the sync copies more traders than ``.env`` pinned."""
        if not self._configured or previous_ids != set(self._configured):
            return
        extra = sorted(set(fresh) - set(self._configured))
        if not extra:
            return
        logger.warning(
            "allowlist sync widened PORTFOLIO_ALLOWLIST from %d to %d portfolios "
            "(+%s) — set PORTFOLIO_SYNC=false to pin the .env list",
            len(self._configured),
            len(fresh),
            ", ".join(self.label(pid) for pid in extra),
        )
        log_event(
            "allowlist_widened",
            configured=len(self._configured),
            total=len(fresh),
            added=[self.label(pid) for pid in extra],
        )

    def refresh(self) -> bool:
        """Pull followed portfolios. True when the effective list changed."""
        self._last_attempt = self._now()
        if self.client is None:
            self.last_error = "no API client"
            return False

        user_id = self._resolve_user_id()
        if not user_id:
            self.last_error = "no user_id in session"
            logger.warning("allowlist sync skipped — no user_id in session")
            return False

        try:
            fresh = self._fetch(user_id)
        except Exception as exc:  # noqa: BLE001 — never kill the poll loop
            self.last_error = str(exc)
            logger.warning("allowlist sync failed: %s — keeping %s", exc, self._kept())
            log_event(
                "allowlist_sync_failed",
                error=str(exc),
                source=self.source,
                usable=self.usable,
            )
            return False

        if not fresh:
            self.last_error = "API returned no portfolios"
            logger.warning("allowlist sync returned no portfolios — keeping %s", self._kept())
            log_event("allowlist_sync_empty", source=self.source, kept=len(self.ids))
            return False

        self.last_error = None
        previous = self._synced or {}
        previous_ids = self.ids
        self._synced = fresh
        self._store_cached(fresh)
        self._warn_if_widened(previous_ids, fresh)

        added = sorted(set(fresh) - previous_ids)
        removed = sorted(previous_ids - set(fresh))
        renamed = [
            pid
            for pid, record in fresh.items()
            if pid in previous and previous[pid].label != record.label
        ]
        if not added and not removed and not renamed:
            return False
        logger.info(
            "allowlist synced: +%d -%d (%d total)", len(added), len(removed), len(fresh)
        )
        log_event(
            "allowlist_synced",
            added=[self.label(pid) for pid in added] or None,
            removed=[(previous[pid].label if pid in previous else pid) for pid in removed]
            or None,
            total=len(fresh),
        )
        return True
