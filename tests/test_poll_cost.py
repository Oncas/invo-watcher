"""The per-poll cost of a snapshot: how many reads, and how they overlap.

These pin the two things that made the loop slower and heavier than its
configured interval — four book reads taken in sequence, and a HUD that
refetched the whole 24h window (and rebuilt its client) every few seconds.
"""

from __future__ import annotations

import time

from invo_agent.config import Settings
from invo_agent.dex.hl_trader import HlTrader


class _FakeResponse:
    def __init__(self, payload: object):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeHttp:
    """Answers /info by request type, and records concurrency."""

    def __init__(self, payloads: dict[str, object], *, fail: set[str] | None = None):
        self.payloads = payloads
        self.fail = fail or set()
        self.seen: list[str] = []
        self.inflight = 0
        self.max_inflight = 0

    def post(self, url: str, json: dict) -> _FakeResponse:
        typ = str(json.get("type"))
        self.seen.append(typ)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            time.sleep(0.05)  # long enough that sequential calls could not overlap
            if typ in self.fail:
                raise RuntimeError(f"{typ} unavailable")
            return _FakeResponse(self.payloads.get(typ))
        finally:
            self.inflight -= 1


def _trader(http: _FakeHttp) -> HlTrader:
    """An HlTrader with no signer and no Info(), so nothing touches the network."""
    trader = object.__new__(HlTrader)
    trader._http = http
    trader.base_url = "https://example.invalid"
    trader.address = "0xabc"
    trader._book = None
    trader._leverage = {}
    trader.leverage_stale_seconds = 3600.0
    return trader


def _payloads() -> dict[str, object]:
    return {
        "allMids": {"aaa": "2.0"},
        "clearinghouseState": {
            "crossMarginSummary": {"accountValue": "100.0", "totalMarginUsed": "40.0"},
            "assetPositions": [],
        },
        "frontendOpenOrders": [{"coin": "AAA", "oid": 1}],
        "userFillsByTime": [],
    }


def test_book_reads_go_out_together() -> None:
    """All four reads are in flight at once, not one after another."""
    http = _FakeHttp(_payloads())
    book = _trader(http).refresh_book(fills_since_ms=1_000)

    assert sorted(http.seen) == [
        "allMids",
        "clearinghouseState",
        "frontendOpenOrders",
        "userFillsByTime",
    ]
    assert http.max_inflight == 4
    assert book.equity == 100.0
    assert book.available == 60.0
    assert book.mids == {"AAA": 2.0}
    assert book.open_orders == [{"coin": "AAA", "oid": 1}]


def test_fills_are_skipped_when_not_asked_for() -> None:
    http = _FakeHttp(_payloads())
    book = _trader(http).refresh_book()

    assert "userFillsByTime" not in http.seen
    assert book.fills == []


def test_orders_and_fills_degrade_without_losing_the_book() -> None:
    """Their failures were swallowed when sequential; they still are."""
    http = _FakeHttp(_payloads(), fail={"frontendOpenOrders", "userFillsByTime"})
    book = _trader(http).refresh_book(fills_since_ms=1_000)

    assert book.open_orders == []
    assert book.fills == []
    assert book.equity == 100.0


def test_a_failed_mid_read_still_raises() -> None:
    """Mids and state are load-bearing — a silent empty book would size wrong."""
    http = _FakeHttp(_payloads(), fail={"allMids"})
    try:
        _trader(http).refresh_book(fills_since_ms=1_000)
    except RuntimeError as exc:
        assert "allMids" in str(exc)
    else:
        raise AssertionError("expected the allMids failure to propagate")


# --- HUD: the 24h window is the expensive half, and it barely moves ---


class _FakeInfo:
    def __init__(self, equity: float, baseline_ms: int, baseline_equity: float):
        self.equity = equity
        self.baseline_ms = baseline_ms
        self.baseline_equity = baseline_equity
        self.portfolio_calls = 0
        self.ledger_calls = 0

    def user_state(self, address: str) -> dict:
        return {
            "marginSummary": {"accountValue": str(self.equity)},
            "assetPositions": [],
        }

    def portfolio(self, address: str) -> list:
        self.portfolio_calls += 1
        return [
            (
                "perpDay",
                {"accountValueHistory": [[self.baseline_ms, str(self.baseline_equity)]]},
            )
        ]

    def user_non_funding_ledger_updates(self, address: str, startTime: int) -> list:
        self.ledger_calls += 1
        return []


class _FakeSession:
    master_address = "0xabc"


def _hud_settings(tmp_path) -> Settings:
    return Settings(
        session_path=tmp_path / "session.json",
        dex_session_path=tmp_path / "dex-session.json",
        db_path=tmp_path / "agent.db",
        log_dir=tmp_path / "logs",
    )


def _patch_hud(monkeypatch, info: _FakeInfo) -> None:
    from invo_agent.dex import session as dex_session

    monkeypatch.setattr("invo_agent.hud.read_only_info", lambda: info)
    monkeypatch.setattr(
        dex_session.DexSessionStore, "load", lambda self: _FakeSession()
    )


def test_warm_baseline_is_not_refetched(tmp_path, monkeypatch) -> None:
    """The second refresh reuses the window and only re-reads live equity."""
    from invo_agent import hud

    now_ms = int(time.time() * 1000)
    info = _FakeInfo(equity=110.0, baseline_ms=now_ms - 3_600_000, baseline_equity=100.0)
    _patch_hud(monkeypatch, info)
    settings = _hud_settings(tmp_path)

    first = hud._fetch_hl_pnl(settings)
    assert first["error"] is None
    assert first["pnl_24h"] == 10.0
    assert first["pnl_24h_pct"] == 10.0
    assert info.portfolio_calls == 1

    # Expire only the live half of the cache; the window is still warm.
    hud._pnl_cache_path(settings).write_text(
        __import__("json").dumps({**first, "fetched_at": time.time() - 30.0})
    )
    info.equity = 120.0
    second = hud._fetch_hl_pnl(settings)

    assert info.portfolio_calls == 1, "baseline refetched while still warm"
    assert info.ledger_calls == 1
    assert second["equity"] == 120.0
    assert second["pnl_24h"] == 20.0, "PnL must track live equity, not the cache"


def test_cold_baseline_is_refetched(tmp_path, monkeypatch) -> None:
    from invo_agent import hud

    now_ms = int(time.time() * 1000)
    info = _FakeInfo(equity=110.0, baseline_ms=now_ms - 3_600_000, baseline_equity=100.0)
    _patch_hud(monkeypatch, info)
    settings = _hud_settings(tmp_path)

    first = hud._fetch_hl_pnl(settings)
    aged = time.time() - (hud.PNL_BASELINE_CACHE_SECONDS + 5.0)
    hud._pnl_cache_path(settings).write_text(
        __import__("json").dumps(
            {**first, "fetched_at": aged, "baseline_fetched_at": aged}
        )
    )

    hud._fetch_hl_pnl(settings)
    assert info.portfolio_calls == 2
