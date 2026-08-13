from __future__ import annotations

import json
from pathlib import Path

from invo_agent.config import Settings
from invo_agent.db import AgentDB, OpenCopy
from invo_agent.labels import (
    coin_from_summary,
    merge_update_tags,
    own_copy_from_summary,
    tags_from_action_row,
    tags_from_event,
    tags_from_summary,
    trader_from_summary,
)


def _tail_jsonl(path: Path, max_bytes: int = 1_000_000) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    text = data.decode("utf-8", errors="replace")
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _tags_from_events(
    events: list[dict], *, coin: str | None, short_id: str | None
) -> list[str]:
    if not coin:
        return []
    coin_u = coin.upper()
    short = (short_id or "").lower()
    found: list[str] = []
    for item in reversed(events):
        ev_coin = str(item.get("coin") or "").upper()
        if ev_coin and ev_coin != coin_u:
            continue
        ev_short = str(item.get("base_short_id") or "").lower()
        if short and ev_short and ev_short != short:
            continue
        kind = item.get("kind")
        if kind not in {
            "update_recorded",
            "size_increase_hl",
            "size_increase_skip",
            "size_decrease_hl",
            "size_decrease_skip",
        }:
            continue
        found = merge_update_tags(found, tags_from_event(kind, item.get("update_type")))
        if kind == "update_recorded":
            break
    return found


def backfill_trader_names(db: AgentDB, copies: list[OpenCopy] | None = None) -> int:
    """Fill missing/wrong trader columns from action_log. Returns rows changed."""
    changed = 0
    if copies is None:
        copies = db.list_open_copies()
    for copy in copies:
        name = None
        if copy.base_short_id:
            name = db.lookup_trader_from_logs(short_id=copy.base_short_id)
        if not name and not copy.trader:
            name = db.lookup_trader_from_logs(ticker=copy.ticker)
        if name and name != copy.trader:
            db.set_open_copy_trader(copy.investment_base_id, name)
            changed += 1
    for intent in db.list_pending_intents():
        if intent.trader:
            continue
        name = trader_from_summary(intent.summary)
        if not name and intent.base_short_id:
            name = db.lookup_trader_from_logs(short_id=intent.base_short_id)
        if name:
            intent.trader = name
            db.upsert_pending_intent(intent)
            changed += 1
    return changed


def migrate_state(settings: Settings) -> dict[str, int]:
    """Idempotent: stamp action_log tags, fill traders, drop HUD tag cache."""
    db = AgentDB(settings.db_path)
    events = _tail_jsonl(settings.log_dir.expanduser() / "events.jsonl")
    tagged = 0
    for row in db.list_actions():
        action = row.action or ""
        summary = row.summary or ""
        if "UPDATE" not in action.upper() and action.upper() != "QUEUED_SIZE":
            continue
        if tags_from_summary(summary):
            continue
        own = own_copy_from_summary(summary)
        short_id = own[1] if own else None
        tags = merge_update_tags(
            tags_from_action_row(action, summary),
            _tags_from_events(
                events,
                coin=coin_from_summary(summary),
                short_id=short_id,
            ),
        )
        if not tags:
            continue
        if db.append_summary_tags(row.id, tags):
            tagged += 1
    traders = backfill_trader_names(db)
    dropped = db.delete_meta_prefix("update_tags:")
    db.close()
    return {"tagged": tagged, "traders": traders, "meta_dropped": dropped}
