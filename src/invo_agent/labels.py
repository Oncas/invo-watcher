from __future__ import annotations

import re

from invo_agent.api.models import TradeAction

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


def trader_from_action(action: TradeAction) -> str | None:
    inv = action.investment
    if inv is not None and inv.owner is not None and inv.owner.username:
        return inv.owner.username
    notifier = action.notification.notifier
    if notifier is not None and notifier.username:
        return notifier.username
    return trader_from_summary(action.summary)
