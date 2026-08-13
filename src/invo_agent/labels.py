from __future__ import annotations

import re

from invo_agent.api.models import ActionType, InvestmentUpdate, TradeAction

_UPDATE_TAG_ORDER = ("size", "tp", "sl", "lev")
_UPDATE_TAG_SET = frozenset(_UPDATE_TAG_ORDER)

_AT_USER = re.compile(r"@([A-Za-z0-9_.]+)")
# The investment this line is about — not a later "via OTHERID" mention.
_OWN_COPY = re.compile(r"@([A-Za-z0-9_.]+)\s+([A-Za-z0-9]+)(?:\s|:|$)")


def trader_from_summary(summary: str | None) -> str | None:
    if not summary or "@" not in summary:
        return None
    match = _AT_USER.search(summary)
    return match.group(1) if match else None


def own_copy_from_summary(summary: str | None) -> tuple[str, str] | None:
    """Return (trader, short_id) for this action's investment, if present."""
    if not summary:
        return None
    match = _OWN_COPY.search(summary)
    if not match:
        return None
    return match.group(1), match.group(2)


def _change_present(changes: dict, *keys: str) -> bool:
    return any(key in changes for key in keys)


def _numeric_changed(current: float | None, previous: object) -> bool:
    if previous is None or current is None:
        return previous is not None or current is not None
    try:
        prev = float(previous)
    except (TypeError, ValueError):
        return True
    return abs(float(current) - prev) > max(1e-9, abs(prev) * 0.01)


def update_labels(inv: InvestmentUpdate | None) -> list[str]:
    """Short tags for what an UPDATE changed: size, tp, sl, lev."""
    if inv is None:
        return []
    changes = inv.changes or {}
    tags: list[str] = []

    size = changes.get("simIncrease") is True
    if not size and "entrySize" in changes:
        size = _numeric_changed(inv.entry_size, changes.get("entrySize"))
    if not size:
        try:
            size = abs(float(changes.get("simDifference") or 0)) > 1e-9
        except (TypeError, ValueError):
            size = False
    if size:
        tags.append("size")
    if _change_present(changes, "priceTarget", "price_target"):
        tags.append("tp")
    if _change_present(changes, "stopLoss", "stop_loss"):
        tags.append("sl")
    if _change_present(changes, "leverage"):
        tags.append("lev")
    return tags


def format_action_summary(action: TradeAction) -> str:
    """Trade line plus UPDATE tags, e.g. ``UPDATE ETH LONG 20x @x · size · tp``."""
    base = action.summary
    if action.action is not ActionType.UPDATE:
        return base
    tags = update_labels(action.investment)
    if not tags:
        return base
    return f"{base} · {' · '.join(tags)}"


def format_recent_line(action: str, summary: str, tags: list[str] | None = None) -> str:
    """``UPDATE · size · tp: ETH LONG 20x @beta_trader`` — tags stay visible."""
    tags = list(tags) if tags is not None else tags_from_summary(summary)
    core = summary or ""
    if core.startswith(f"{action} "):
        core = core[len(action) + 1 :]
    for tag in tags:
        core = core.replace(f" · {tag}", "")
    core = core.strip()
    tag_s = f" · {' · '.join(tags)}" if tags else ""
    if core:
        return f"{action}{tag_s}: {core}"
    return f"{action}{tag_s}"


def tags_from_summary(summary: str | None) -> list[str]:
    """Read `` · size · tp`` suffixes written by ``format_action_summary``."""
    if not summary:
        return []
    found: list[str] = []
    for part in summary.split(" · "):
        token = part.strip().lower()
        if token in _UPDATE_TAG_SET and token not in found:
            found.append(token)
    return [tag for tag in _UPDATE_TAG_ORDER if tag in found]


def trader_from_action(action: TradeAction) -> str | None:
    inv = action.investment
    if inv is not None and inv.owner is not None and inv.owner.username:
        return inv.owner.username
    notifier = action.notification.notifier
    if notifier is not None and notifier.username:
        return notifier.username
    return trader_from_summary(action.summary)
