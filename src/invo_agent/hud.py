from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from invo_agent import service as launchd_service
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings
from invo_agent.db import GHOST_SEEN_META_PREFIX, AgentDB, OpenCopy
from invo_agent.labels import tags_from_summary, trader_from_summary

# How often to refresh Hyperliquid mark/PnL (network call).
PNL_CACHE_SECONDS = 5.0
# The 24h baseline costs two heavy info reads (portfolio + ledger) and barely
# moves. Refresh it on its own clock and recompute PnL from live equity.
PNL_BASELINE_CACHE_SECONDS = 60.0
# Involio PnlService.getBaselineAtWindowStart — do not pin or 6h-lag-pick.
PNL_RULE = "first_at_or_after"


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
    open_age_s: float | None = None
    # HL holds this coin but no open_copy tracks it, so no signal from the
    # trader will move or close it. It needs closing by hand.
    untracked: bool = False
    untracked_since: str | None = None


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
    # Positions HL holds that no open_copy tracks. Zero is the healthy case.
    untracked: int = 0
    equity: float | None = None
    pnl_24h: float | None = None
    pnl_24h_pct: float | None = None  # percent points, e.g. 11.6
    positions: list[PositionRow] = field(default_factory=list)
    pnl_updated_at: str | None = None
    pending: list[dict] = field(default_factory=list)
    pnl_baseline_ms: int | None = None
    pnl_rule: str | None = None


def hud_path(settings: Settings) -> Path:
    return settings.session_path.expanduser().parent / "hud.json"


def _pnl_cache_path(settings: Settings) -> Path:
    return settings.session_path.expanduser().parent / "pnl-cache.json"


def pending_kind(summary: str | None = None, *, kind: str | None = None) -> str:
    """Queued intents are always OPEN. UPDATE is only set for size-adds on a live copy."""
    if kind:
        return str(kind)
    return "OPEN"


_WAIT_ALIASES = {
    "budget": "ratelimit",
    "size": "margin",
    "dust": "margin",
    "below_hl_min": "margin",
    "under min": "margin",
}


def display_wait(reason: str | None) -> str:
    wait = (reason or "").strip() or "?"
    return _WAIT_ALIASES.get(wait, wait)


def format_pending_reason(
    *,
    kind: str | None = None,
    reason: str | None = None,
    side: str | None = None,
    trader_entry: float | None = None,
    amount: float | None = None,
) -> str:
    """Queue label: wait reason, plus size↑ on leftover adds."""
    _ = (side, trader_entry, amount)
    wait = display_wait(reason)
    if pending_kind(kind=kind) == "UPDATE":
        return f"size↑ · {wait}"
    return wait


def pending_last_tried_age(pending: list[dict] | None) -> float | None:
    """Most recent drain attempt across the queue (they are tried together)."""
    ages = [
        item.get("tried_age_s")
        for item in pending or []
        if isinstance(item.get("tried_age_s"), (int, float))
    ]
    return min(ages) if ages else None


def format_age(seconds: float | None) -> str:
    """Human age like ``4s``, ``1m 30s``, ``1h 30m`` — no decimal units."""
    if seconds is None:
        return "?"
    total = int(round(max(0.0, float(seconds))))
    if total < 60:
        return f"{total}s"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    # Seconds only under an hour so 90s → 1m 30s and 5400s → 1h 30m.
    if not days and not hours and secs:
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"


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


# Involio PnlService.getBaselineAtWindowStart (main.dart.js): first perp
# print at or after T−24h. The 6h lag (216e5 ms) is a staleness gate that
# *hides* PnL when only an older fallback exists — it is not a picker.
# Recompute every fetch (no pin). That matches a refreshed wallet header.
_PNL_BASELINE_MAX_LAG_MS = 21_600_000


@dataclass(frozen=True)
class RollingPnl:
    pnl: float
    pct: float
    baseline_ms: int
    baseline_equity: float
    net_flow: float
    gross_in: float


def _baseline_at_window_start(
    samples: list[tuple[int, float]], window_start_ms: int
) -> tuple[int, float] | None:
    """First Hyperliquid print at or after T−24h; else the last print before it."""
    last_before: tuple[int, float] | None = None
    for row in samples:
        if row[0] >= window_start_ms:
            return row
        last_before = row
    return last_before


def _usable_baseline(
    samples: list[tuple[int, float]], window_start_ms: int
) -> tuple[int, float] | None:
    """Same as getBaselineAtWindowStart, then drop a fallback older than 6h."""
    baseline = _baseline_at_window_start(samples, window_start_ms)
    if baseline is None:
        return None
    if baseline[0] < window_start_ms - _PNL_BASELINE_MAX_LAG_MS:
        return None
    return baseline


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


class RollingBaseline(NamedTuple):
    """The slow half of the 24h window: where it starts and what crossed it."""

    baseline_ms: int
    baseline_equity: float
    net_flow: float
    gross_in: float


def _fetch_rolling_baseline(info: object, address: str) -> RollingBaseline | None:
    """The two heavy info reads behind the 24h window (portfolio + ledger)."""
    try:
        portfolio = info.portfolio(address)  # type: ignore[attr-defined]
    except Exception:
        return None
    samples = _account_value_samples(portfolio)
    window_start_ms = int(time.time() * 1000) - 86_400_000
    baseline = _usable_baseline(samples, window_start_ms)
    if baseline is None:
        return None
    baseline_ms, baseline_equity = baseline

    net_flow = 0.0
    gross_in = 0.0
    try:
        updates = info.user_non_funding_ledger_updates(  # type: ignore[attr-defined]
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
    return RollingBaseline(baseline_ms, baseline_equity, net_flow, gross_in)


def rolling_pnl_from_baseline(
    baseline: RollingBaseline, equity: float
) -> RollingPnl | None:
    """The cheap half: pure math, so live equity can move without a refetch."""
    pnl = equity - baseline.baseline_equity - baseline.net_flow
    denom = baseline.baseline_equity + baseline.gross_in
    if abs(denom) <= 1e-9:
        return None
    return RollingPnl(
        pnl=pnl,
        pct=(pnl / denom) * 100.0,
        baseline_ms=baseline.baseline_ms,
        baseline_equity=baseline.baseline_equity,
        net_flow=baseline.net_flow,
        gross_in=baseline.gross_in,
    )


def _involio_rolling_24h_pnl(
    info: object,
    address: str,
    equity: float,
) -> RollingPnl | None:
    """Match Involio AccountHeader rolling-24h PnL.

    pnl = equity − baseline − net_external_flows
    pct = pnl / (baseline + gross_inflows) × 100
    """
    baseline = _fetch_rolling_baseline(info, address)
    if baseline is None:
        return None
    return rolling_pnl_from_baseline(baseline, equity)


def pnl_served_matches_fresh(
    *,
    served_equity: float | None,
    served_pnl: float | None,
    served_baseline_ms: int | None,
    served_rule: str | None,
    fresh: RollingPnl,
    max_drift: float = 0.05,
) -> str | None:
    """None if the served HUD header uses the same Involio window as ``fresh``.

    Marks can move between fetches. Same baseline + flows means
    Δpnl must equal Δequity.
    """
    if served_rule != PNL_RULE:
        return f"pnl_rule {served_rule!r} != {PNL_RULE!r}"
    if served_baseline_ms != fresh.baseline_ms:
        return f"baseline {served_baseline_ms} != {fresh.baseline_ms}"
    if served_equity is None or served_pnl is None:
        return "served equity/pnl missing"
    expected = served_equity - fresh.baseline_equity - fresh.net_flow
    if abs(served_pnl - expected) > max_drift:
        return f"pnl {served_pnl:.4f} != {expected:.4f} (same window)"
    return None


def _read_pnl_cache(settings: Settings) -> dict | None:
    """The cache file as written, however old. None if unreadable or stale-ruled."""
    path = _pnl_cache_path(settings)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if data.get("pnl_rule") != PNL_RULE:
        return None
    return data


def _load_pnl_cache(settings: Settings) -> dict | None:
    data = _read_pnl_cache(settings)
    if data is None:
        return None
    fetched = float(data.get("fetched_at") or 0)
    if time.time() - fetched > PNL_CACHE_SECONDS:
        return None
    return data


def _cached_rolling_baseline(settings: Settings) -> tuple[RollingBaseline, float] | None:
    """A still-warm baseline plus the time it was fetched, or None to refetch."""
    data = _read_pnl_cache(settings)
    if data is None:
        return None
    at = float(data.get("baseline_fetched_at") or 0)
    if at <= 0 or time.time() - at > PNL_BASELINE_CACHE_SECONDS:
        return None
    try:
        return (
            RollingBaseline(
                baseline_ms=int(data["pnl_baseline_ms"]),
                baseline_equity=float(data["pnl_baseline_equity"]),
                net_flow=float(data["pnl_net_flow"]),
                gross_in=float(data["pnl_gross_in"]),
            ),
            at,
        )
    except (KeyError, TypeError, ValueError):
        return None


_info_lock = threading.Lock()
_info_client: object | None = None


def read_only_info() -> object:
    """One shared, read-only Hyperliquid ``Info`` client.

    Constructing ``Info`` costs a ``spotMeta`` + ``meta`` fetch (40 of the
    1200/min IP weight budget). Rebuilding one per HUD refresh made the display
    the single heaviest consumer of that budget, ahead of the trader itself.
    None of the HUD's reads sign anything, so no Turnkey signer is needed.
    """
    global _info_client
    with _info_lock:
        if _info_client is None:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants

            _info_client = Info(constants.MAINNET_API_URL, skip_ws=True)
        return _info_client


def _fetch_hl_pnl(settings: Settings) -> dict:
    """Live HL equity + open positions. Cached to disk for PNL_CACHE_SECONDS."""
    cached = _load_pnl_cache(settings)
    if cached is not None:
        return cached

    from invo_agent.dex.session import DexSessionError, DexSessionStore

    out: dict = {
        "fetched_at": time.time(),
        "equity": None,
        "pnl_24h": None,
        "pnl_24h_pct": None,
        "pnl_baseline_ms": None,
        "pnl_baseline_equity": None,
        "pnl_net_flow": None,
        "pnl_gross_in": None,
        "baseline_fetched_at": None,
        "pnl_rule": PNL_RULE,
        "positions": [],
        "error": None,
    }
    try:
        data = DexSessionStore(settings.dex_session_path).load()
        if data is None:
            raise DexSessionError("No DEX session. Run: invo-agent dex-login")
        address = data.master_address
        info = read_only_info()
        state = info.user_state(address)  # type: ignore[attr-defined]
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
        warm = _cached_rolling_baseline(settings)
        if warm is not None:
            baseline, baseline_at = warm
        else:
            baseline = _fetch_rolling_baseline(info, address)
            baseline_at = time.time()
        rolling = (
            rolling_pnl_from_baseline(baseline, equity)
            if baseline is not None
            else None
        )
        if rolling is not None:
            out["pnl_24h"] = rolling.pnl
            out["pnl_24h_pct"] = rolling.pct
            out["pnl_baseline_ms"] = rolling.baseline_ms
            out["pnl_baseline_equity"] = rolling.baseline_equity
            out["pnl_net_flow"] = rolling.net_flow
            out["pnl_gross_in"] = rolling.gross_in
            out["baseline_fetched_at"] = baseline_at
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


def _opened_at_by_coin(copies: list[OpenCopy]) -> dict[str, str]:
    out: dict[str, str] = {}
    for copy in copies:
        if copy.ticker and copy.opened_at:
            out[copy.ticker.upper()] = copy.opened_at
    return out


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
    recent = []
    for r in recent_rows:
        summary = r.summary or ""
        recent.append(
            {
                "at": r.created_at,
                "action": r.action,
                "summary": summary,
                "tags": tags_from_summary(summary),
            }
        )
    last = recent[0] if recent else None
    copies = db.list_open_copies()
    open_copies = len(copies)
    trader_by_coin = _traders_by_coin(copies)
    opened_at_by_coin = _opened_at_by_coin(copies)
    tracked_coins = {(c.ticker or "").upper() for c in copies if c.ticker}
    # Read the poller's first-seen stamps while the handle is still open — the
    # positions they annotate are only fetched after ``db`` is closed below.
    untracked_since_by_coin = {
        key[len(GHOST_SEEN_META_PREFIX) :]: db.get_meta(key)
        for key in db.meta_keys_with_prefix(GHOST_SEEN_META_PREFIX)
    }
    now_hud = datetime.now(UTC)
    pending_rows: list[dict] = []
    for intent in db.list_pending_intents():
        wait = display_wait(intent.reason)
        pending_rows.append(
            {
                "coin": intent.coin,
                "side": intent.side,
                "leverage": intent.leverage,
                "trader_entry": intent.trader_entry,
                "trader": intent.trader or trader_from_summary(intent.summary),
                "kind": "OPEN",
                "wait": wait,
                "reason": format_pending_reason(
                    kind="OPEN",
                    reason=wait,
                    side=intent.side,
                    trader_entry=intent.trader_entry,
                ),
                "age_s": _age_seconds(intent.created_at, now_hud),
                "last_tried_at": intent.last_tried_at,
                "tried_age_s": _age_seconds(intent.last_tried_at, now_hud),
                "summary": intent.summary,
            }
        )
    for copy in copies:
        add = float(copy.pending_add_notional or 0)
        if add <= 0 or not copy.ticker:
            continue
        wait = display_wait(copy.pending_add_reason or "margin")
        entry = copy.pending_add_entry or copy.our_entry_px
        pending_rows.append(
            {
                "coin": copy.ticker.upper(),
                "side": copy.side or "",
                "leverage": copy.leverage,
                "trader_entry": entry,
                "trader": copy.trader,
                "kind": "UPDATE",
                "wait": wait,
                "amount": add,
                "reason": format_pending_reason(
                    kind="UPDATE",
                    reason=wait,
                    side=copy.side,
                    trader_entry=entry,
                    amount=add,
                ),
                "age_s": _age_seconds(copy.pending_add_at or copy.opened_at, now_hud),
                "last_tried_at": copy.pending_add_tried_at,
                "tried_age_s": _age_seconds(copy.pending_add_tried_at, now_hud),
                "summary": f"UPDATE {copy.ticker.upper()} size↑ +${add:.2f}",
            }
        )
    db.close()

    positions: list[PositionRow] = []
    equity: float | None = None
    pnl_24h: float | None = None
    pnl_24h_pct: float | None = None
    pnl_updated_at: str | None = None
    pnl_baseline_ms: int | None = None
    pnl_rule: str | None = None
    if with_pnl:
        pnl = _fetch_hl_pnl(settings)
        equity = pnl.get("equity")
        raw_24 = pnl.get("pnl_24h")
        raw_pct = pnl.get("pnl_24h_pct")
        pnl_24h = float(raw_24) if raw_24 is not None else None
        pnl_24h_pct = float(raw_pct) if raw_pct is not None else None
        raw_base = pnl.get("pnl_baseline_ms")
        pnl_baseline_ms = int(raw_base) if raw_base is not None else None
        raw_rule = pnl.get("pnl_rule")
        pnl_rule = str(raw_rule) if raw_rule else None
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
            payload["open_age_s"] = _age_seconds(opened_at_by_coin.get(coin), now_hud)
            # No open_copy for the coin means nothing will act on it again —
            # the trader's own CLOSE is skipped as "not our copy".
            if coin and coin not in tracked_coins:
                payload["untracked"] = True
                payload["untracked_since"] = untracked_since_by_coin.get(coin)
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
        untracked=sum(1 for p in positions if p.untracked),
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
        pnl_baseline_ms=pnl_baseline_ms,
        pnl_rule=pnl_rule,
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


def check_local_hud_pnl(settings: Settings) -> str | None:
    """Compare the running /v1/hud 24h header to a fresh Involio-rule compute."""
    import urllib.error
    import urllib.request

    from invo_agent.dex.session import DexSessionStore

    if not settings.hud_token:
        return "HUD_TOKEN unset"
    req = urllib.request.Request(
        f"http://127.0.0.1:{settings.hud_port}/v1/hud",
        headers={"Authorization": f"Bearer {settings.hud_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            served = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return f"hud fetch failed: {exc}"
    if not isinstance(served, dict):
        return "hud payload is not an object"

    data = DexSessionStore(settings.dex_session_path).load()
    if data is None:
        return "no DEX session"
    info = read_only_info()
    state = info.user_state(data.master_address)  # type: ignore[attr-defined]
    summary = state.get("marginSummary") or state.get("crossMarginSummary") or {}
    equity = float(summary.get("accountValue") or 0)
    fresh = _involio_rolling_24h_pnl(info, data.master_address, equity)
    if fresh is None:
        return "fresh Involio 24h PnL unavailable"
    raw_base = served.get("pnl_baseline_ms")
    try:
        served_baseline_ms = int(raw_base) if raw_base is not None else None
    except (TypeError, ValueError):
        served_baseline_ms = None
    verdict = pnl_served_matches_fresh(
        served_equity=served.get("equity"),
        served_pnl=served.get("pnl_24h"),
        served_baseline_ms=served_baseline_ms,
        served_rule=served.get("pnl_rule"),
        fresh=fresh,
    )
    if (
        verdict is not None
        and served_baseline_ms is not None
        and served_baseline_ms != fresh.baseline_ms
    ):
        # The server holds its baseline for PNL_BASELINE_CACHE_SECONDS, so the
        # 24h window can roll between its fetch and ours. That is the cache
        # working, not a wrong number — say which one it is.
        return (
            f"{verdict} — window rolled; served baseline is up to "
            f"{PNL_BASELINE_CACHE_SECONDS:.0f}s old, rerun to confirm"
        )
    return verdict
