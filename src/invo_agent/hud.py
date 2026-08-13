from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

from invo_agent import service as launchd_service
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings
from invo_agent.db import AgentDB, OpenCopy
from invo_agent.labels import trader_from_summary

# How often to refresh Hyperliquid mark/PnL (network call).
PNL_CACHE_SECONDS = 5.0


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
    trader: str | None = None


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
    pnl_24h: float | None = None
    pnl_24h_pct: float | None = None  # percent points, e.g. 11.6
    positions: list[PositionRow] = field(default_factory=list)
    pnl_updated_at: str | None = None
    pending: list[dict] = field(default_factory=list)


def hud_path(settings: Settings) -> Path:
    return settings.session_path.expanduser().parent / "hud.json"


def _pnl_cache_path(settings: Settings) -> Path:
    return settings.session_path.expanduser().parent / "pnl-cache.json"


def pending_last_tried_age(pending: list[dict] | None) -> float | None:
    """Most recent drain attempt across the queue (they are tried together)."""
    ages = [
        item.get("tried_age_s")
        for item in pending or []
        if isinstance(item.get("tried_age_s"), (int, float))
    ]
    return min(ages) if ages else None


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        mins = seconds / 60.0
        return f"{mins:.0f}m" if mins >= 10 else f"{mins:.1f}m"
    hours = seconds / 3600.0
    return f"{hours:.0f}h" if hours >= 10 else f"{hours:.1f}h"


def _age_seconds(raw: str | None, now: datetime) -> float | None:
    dt = _parse_ts(raw)
    if dt is None:
        return None
    return (now - dt).total_seconds()


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


def _portfolio_series(portfolio: object, *keys: str) -> list[dict]:
    """Extract named series payloads from HL portfolio response."""
    out: list[dict] = []
    if isinstance(portfolio, list):
        by_key = {k: p for k, p in portfolio}
        for key in keys:
            payload = by_key.get(key)
            if isinstance(payload, dict):
                out.append(payload)
    elif isinstance(portfolio, dict):
        for key in keys:
            payload = portfolio.get(key)
            if isinstance(payload, dict):
                out.append(payload)
    return out


def _account_value_samples(portfolio: object) -> list[tuple[int, float]]:
    """Merge perpDay+perpWeek accountValueHistory (Involio baseline source)."""
    samples: list[tuple[int, float]] = []
    seen: set[int] = set()
    for series in _portfolio_series(portfolio, "perpDay", "perpWeek"):
        for row in series.get("accountValueHistory") or []:
            try:
                ts = int(row[0])
                value = float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if ts in seen:
                continue
            seen.add(ts)
            samples.append((ts, value))
    samples.sort(key=lambda item: item[0])
    return samples


def _baseline_at_window_start(
    samples: list[tuple[int, float]], window_start_ms: int
) -> tuple[int, float] | None:
    """Account value at T−24h: last perp print at or before the window open.

    Involio's wallet header is rolling 24h (now − 24h, not local midnight).
    perpDay prints are hours apart. ``nearest`` looks right until T−24h slides
    closer to the next print, then we jump and the HUD drifts from the site.
    The site keeps the last print with ``ts <= T−24h`` (else the first after).
    """
    last_at_or_before: tuple[int, float] | None = None
    first_after: tuple[int, float] | None = None
    for row in samples:
        if row[0] <= window_start_ms:
            last_at_or_before = row
        elif first_after is None:
            first_after = row
    return last_at_or_before or first_after


def _ledger_flow_usd(delta: dict, address: str) -> tuple[float, float]:
    """Return (net_flow, gross_in) for one non-funding ledger delta.

    Mirrors Involio AccountHeader: deposits / sends into the wallet raise the
    baseline denominator and are excluded from trading PnL.
    """
    addr = address.lower()
    typ = str(delta.get("type") or "")

    def _f(*keys: str) -> float | None:
        for key in keys:
            raw = delta.get(key)
            if raw is None or raw == "":
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    if typ == "deposit":
        amount = _f("usdc", "amount", "usdcValue") or 0.0
        return amount, max(amount, 0.0)
    if typ == "withdraw":
        amount = abs(_f("usdc", "amount", "usdcValue") or 0.0)
        return -amount, 0.0
    if typ == "accountClassTransfer":
        amount = _f("usdc", "amount") or 0.0
        if delta.get("toPerp"):
            return amount, max(amount, 0.0)
        return -abs(amount), 0.0
    if typ == "send":
        amount = abs(_f("amount", "usdcValue", "usdc") or 0.0)
        token = str(delta.get("token") or "").upper()
        if token and not token.startswith("USDC"):
            return 0.0, 0.0
        dest = str(delta.get("destination") or "").lower()
        sender = str(delta.get("user") or "").lower()
        if dest == addr:
            return amount, amount
        if sender == addr:
            return -amount, 0.0
        return 0.0, 0.0
    if typ == "internalTransfer":
        amount = abs(_f("usdc", "amount", "usdcValue") or 0.0)
        dest = str(delta.get("destination") or delta.get("user") or "").lower()
        source = str(delta.get("user") or delta.get("source") or "").lower()
        if dest == addr and source != addr:
            return amount, amount
        if source == addr and dest != addr:
            return -amount, 0.0
    return 0.0, 0.0


def _involio_rolling_24h_pnl(
    trader: object, address: str, equity: float
) -> tuple[float, float] | None:
    """Match Involio AccountHeader rolling-24h PnL.

    pnl = equity − baseline − net_external_flows
    pct = pnl / (baseline + gross_inflows) × 100
    """
    info = trader.info  # type: ignore[attr-defined]
    try:
        portfolio = info.portfolio(address)
    except Exception:
        return None
    samples = _account_value_samples(portfolio)
    window_start_ms = int(time.time() * 1000) - 86_400_000
    baseline = _baseline_at_window_start(samples, window_start_ms)
    if baseline is None:
        return None
    baseline_ms, baseline_equity = baseline

    net_flow = 0.0
    gross_in = 0.0
    try:
        updates = info.user_non_funding_ledger_updates(
            address, startTime=baseline_ms + 1
        )
    except Exception:
        updates = []
    for item in updates or []:
        if not isinstance(item, dict):
            continue
        delta = item.get("delta")
        if not isinstance(delta, dict):
            continue
        net, gin = _ledger_flow_usd(delta, address)
        net_flow += net
        gross_in += gin

    pnl = equity - baseline_equity - net_flow
    denom = baseline_equity + gross_in
    if abs(denom) <= 1e-9:
        return None
    return pnl, (pnl / denom) * 100.0


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

    from invo_agent.dex.hl_trader import HlTrader
    from invo_agent.dex.session import DexSessionError, DexSessionStore, load_signer

    out: dict = {
        "fetched_at": time.time(),
        "equity": None,
        "pnl_24h": None,
        "pnl_24h_pct": None,
        "positions": [],
        "error": None,
    }
    try:
        store = DexSessionStore(settings.dex_session_path)
        data, signer = load_signer(store)
        trader = HlTrader(signer, data.master_address)
        state = trader.info.user_state(data.master_address)
        summary = state.get("marginSummary") or state.get("crossMarginSummary") or {}
        equity = float(summary.get("accountValue") or 0)
        out["equity"] = equity
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
        rolling = _involio_rolling_24h_pnl(trader, data.master_address, equity)
        if rolling is not None:
            out["pnl_24h"], out["pnl_24h_pct"] = rolling
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


def _traders_by_coin(copies: list[OpenCopy]) -> dict[str, str]:
    out: dict[str, str] = {}
    for copy in copies:
        if copy.ticker and copy.trader:
            out[copy.ticker.upper()] = copy.trader
    return out


def _backfill_trader_names(db: AgentDB, copies: list[OpenCopy]) -> None:
    for copy in copies:
        name = None
        if copy.base_short_id:
            name = db.lookup_trader_from_logs(short_id=copy.base_short_id)
        if not name and not copy.trader:
            name = db.lookup_trader_from_logs(ticker=copy.ticker)
        if name and name != copy.trader:
            db.set_open_copy_trader(copy.investment_base_id, name)
    for intent in db.list_pending_intents():
        if intent.trader:
            continue
        name = trader_from_summary(intent.summary)
        if not name and intent.base_short_id:
            name = db.lookup_trader_from_logs(short_id=intent.base_short_id)
        if name:
            intent.trader = name
            db.upsert_pending_intent(intent)


def collect_hud(
    settings: Settings,
    *,
    with_pnl: bool = True,
    supervisor_ok: bool | None = None,
) -> HudSnapshot:
    db = AgentDB(settings.db_path)
    session = SessionStore(settings.session_path).load()
    last_poll = db.get_meta("last_poll_at")
    poll_dt = _parse_ts(last_poll)
    now = datetime.now(UTC)
    age = (now - poll_dt).total_seconds() if poll_dt else None

    stale_after = max(30.0, settings.poll_interval_seconds * 4)
    if supervisor_ok is None:
        launchd = launchd_service.launchd_running()
    else:
        launchd = supervisor_ok
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
    copies = db.list_open_copies()
    _backfill_trader_names(db, copies)
    copies = db.list_open_copies()
    open_copies = len(copies)
    trader_by_coin = _traders_by_coin(copies)
    now_hud = datetime.now(UTC)
    pending_rows: list[dict] = []
    for intent in db.list_pending_intents():
        pending_rows.append(
            {
                "coin": intent.coin,
                "side": intent.side,
                "leverage": intent.leverage,
                "trader_entry": intent.trader_entry,
                "trader": intent.trader or trader_from_summary(intent.summary),
                "reason": intent.reason,
                "age_s": _age_seconds(intent.created_at, now_hud),
                "last_tried_at": intent.last_tried_at,
                "tried_age_s": _age_seconds(intent.last_tried_at, now_hud),
                "summary": intent.summary,
            }
        )
    db.close()

    positions: list[PositionRow] = []
    equity: float | None = None
    pnl_24h: float | None = None
    pnl_24h_pct: float | None = None
    pnl_updated_at: str | None = None
    if with_pnl:
        pnl = _fetch_hl_pnl(settings)
        equity = pnl.get("equity")
        raw_24 = pnl.get("pnl_24h")
        raw_pct = pnl.get("pnl_24h_pct")
        pnl_24h = float(raw_24) if raw_24 is not None else None
        pnl_24h_pct = float(raw_pct) if raw_pct is not None else None
        if pnl.get("fetched_at"):
            pnl_updated_at = datetime.fromtimestamp(
                float(pnl["fetched_at"]), tz=UTC
            ).isoformat()
        pos_fields = {f.name for f in fields(PositionRow)}
        for row in pnl.get("positions") or []:
            if not isinstance(row, dict):
                continue
            payload = {k: v for k, v in row.items() if k in pos_fields}
            coin = str(payload.get("coin") or "").upper()
            payload["trader"] = trader_by_coin.get(coin)
            try:
                positions.append(PositionRow(**payload))
            except TypeError:
                continue

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
        pnl_24h=pnl_24h,
        pnl_24h_pct=pnl_24h_pct,
        positions=positions,
        pnl_updated_at=pnl_updated_at,
        pending=pending_rows,
    )


def load_hud_file(settings: Settings) -> HudSnapshot | None:
    path = hud_path(settings)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    allowed = {f.name for f in fields(HudSnapshot)}
    payload = {k: v for k, v in data.items() if k in allowed}
    rows = []
    pos_fields = {f.name for f in fields(PositionRow)}
    for row in payload.get("positions") or []:
        if not isinstance(row, dict):
            continue
        try:
            rows.append(PositionRow(**{k: v for k, v in row.items() if k in pos_fields}))
        except TypeError:
            continue
    payload["positions"] = rows
    if not isinstance(payload.get("pending"), list):
        payload["pending"] = []
    try:
        return HudSnapshot(**payload)
    except TypeError:
        return None


def hud_file_age_seconds(settings: Settings) -> float | None:
    path = hud_path(settings)
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


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


def format_pnl_24h(pnl: float, pct: float | None = None) -> str:
    """e.g. '+11.60% · +$5.59 24h' (Involio-style)."""
    bits: list[str] = []
    if pct is not None:
        sign = "+" if pct >= 0 else ""
        bits.append(f"{sign}{pct:.2f}%")
    bits.append(format_pnl(pnl))
    return f"{' · '.join(bits)} 24h"
