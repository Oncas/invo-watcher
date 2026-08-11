from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from invo_agent import service as launchd_service
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings
from invo_agent.db import AgentDB

# How often to refresh Hyperliquid mark/PnL (network call).
PNL_CACHE_SECONDS = 60.0


@dataclass
class PositionRow:
    coin: str
    side: str  # LONG / SHORT
    leverage: int
    size: float
    entry_px: float
    position_value: float
    unrealized_pnl: float
    return_on_equity: float  # fraction, e.g. -0.029


@dataclass
class HudSnapshot:
    mode: str
    launchd_loaded: bool
    last_poll_at: str | None
    poll_age_seconds: float | None
    healthy: bool
    open_copies: int
    last_action: str | None
    last_action_at: str | None
    recent: list[dict]
    logged_in: bool
    position_pct: float
    equity: float | None = None
    positions: list[PositionRow] = field(default_factory=list)
    pnl_updated_at: str | None = None


def hud_path(settings: Settings) -> Path:
    return settings.session_path.expanduser().parent / "hud.json"


def _pnl_cache_path(settings: Settings) -> Path:
    return settings.session_path.expanduser().parent / "pnl-cache.json"


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _load_pnl_cache(settings: Settings) -> dict | None:
    path = _pnl_cache_path(settings)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    fetched = float(data.get("fetched_at") or 0)
    if time.time() - fetched > PNL_CACHE_SECONDS:
        return None
    return data


def _fetch_hl_pnl(settings: Settings) -> dict:
    """Live HL equity + open positions. Cached to disk for PNL_CACHE_SECONDS."""
    cached = _load_pnl_cache(settings)
    if cached is not None:
        return cached

    from invo_agent.dex.session import DexSessionError, DexSessionStore, load_signer
    from invo_agent.dex.hl_trader import HlTrader

    out: dict = {
        "fetched_at": time.time(),
        "equity": None,
        "positions": [],
        "error": None,
    }
    try:
        store = DexSessionStore(settings.dex_session_path)
        data, signer = load_signer(store)
        trader = HlTrader(signer, data.master_address)
        state = trader.info.user_state(data.master_address)
        summary = state.get("marginSummary") or {}
        out["equity"] = float(summary.get("accountValue") or 0)
        rows = []
        for item in state.get("assetPositions") or []:
            pos = item.get("position") or {}
            szi = float(pos.get("szi") or 0)
            if szi == 0:
                continue
            lev = pos.get("leverage") or {}
            rows.append(
                {
                    "coin": pos.get("coin") or "?",
                    "side": "LONG" if szi > 0 else "SHORT",
                    "leverage": int(lev.get("value") or 0),
                    "size": abs(szi),
                    "entry_px": float(pos.get("entryPx") or 0),
                    "position_value": float(pos.get("positionValue") or 0),
                    "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
                    "return_on_equity": float(pos.get("returnOnEquity") or 0),
                }
            )
        out["positions"] = rows
        trader.close()
        signer.close()
    except DexSessionError as exc:
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)

    path = _pnl_cache_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))
    return out


def collect_hud(settings: Settings, *, with_pnl: bool = True) -> HudSnapshot:
    db = AgentDB(settings.db_path)
    session = SessionStore(settings.session_path).load()
    last_poll = db.get_meta("last_poll_at")
    poll_dt = _parse_ts(last_poll)
    now = datetime.now(UTC)
    age = (now - poll_dt).total_seconds() if poll_dt else None

    stale_after = max(30.0, settings.poll_interval_seconds * 4)
    launchd = launchd_service.launchd_running()
    healthy = bool(launchd and age is not None and age <= stale_after)

    if settings.live and not settings.dry_run:
        mode = "LIVE"
    elif settings.dry_run:
        mode = "DRY-RUN"
    else:
        mode = "IDLE"

    recent_rows = db.recent_actions(5)
    recent = [
        {
            "at": r.created_at,
            "action": r.action,
            "summary": r.summary or "",
        }
        for r in recent_rows
    ]
    last = recent[0] if recent else None
    open_copies = len(db.list_open_copies())
    db.close()

    positions: list[PositionRow] = []
    equity: float | None = None
    pnl_updated_at: str | None = None
    if with_pnl:
        pnl = _fetch_hl_pnl(settings)
        equity = pnl.get("equity")
        if pnl.get("fetched_at"):
            pnl_updated_at = datetime.fromtimestamp(
                float(pnl["fetched_at"]), tz=UTC
            ).isoformat()
        for row in pnl.get("positions") or []:
            positions.append(PositionRow(**row))

    return HudSnapshot(
        mode=mode,
        launchd_loaded=launchd,
        last_poll_at=last_poll,
        poll_age_seconds=age,
        healthy=healthy,
        open_copies=open_copies,
        last_action=(last["summary"] if last else None),
        last_action_at=(last["at"] if last else None),
        recent=recent,
        logged_in=session is not None,
        position_pct=settings.position_pct,
        equity=equity,
        positions=positions,
        pnl_updated_at=pnl_updated_at,
    )


def write_hud_file(settings: Settings, snap: HudSnapshot | None = None) -> Path:
    snap = snap or collect_hud(settings)
    path = hud_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(snap)
    payload["updated_at"] = datetime.now(UTC).isoformat()
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def format_pnl(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:.2f}"


def format_roe(roe: float) -> str:
    # roe is fraction of margin
    pct = roe * 100.0
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"
