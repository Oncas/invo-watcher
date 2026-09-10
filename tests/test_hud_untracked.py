"""The HUD has to name an untracked position, not leave it to be inferred.

A position HL holds with no ``open_copy`` behind it will not be moved or closed
by anything the trader does — the CLOSE is skipped as "not our copy". Before
this, the only hint in ``hud.json`` was a null ``trader``, which also shows up
for rows the HUD simply has no handle for. The flag says it outright, and the
count makes a single stranger impossible to scroll past.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from invo_agent.config import Settings
from invo_agent.db import GHOST_SEEN_META_PREFIX, AgentDB
from invo_agent.hud import collect_hud, load_hud_file, write_hud_file
from invo_agent.migrate import migrate_state


def _pnl_rows(*coins: str) -> dict:
    return {
        "fetched_at": 1_700_000_000.0,
        "equity": 1000.0,
        "pnl_24h": 0.0,
        "pnl_24h_pct": 0.0,
        "pnl_baseline_ms": None,
        "pnl_rule": "first_at_or_after",
        "error": None,
        "positions": [
            {
                "coin": coin,
                "side": "LONG",
                "leverage": 10,
                "size": 100.0,
                "entry_px": 1.0,
                "position_value": 100.0,
                "unrealized_pnl": 0.0,
                "return_on_equity": 0.0,
            }
            for coin in coins
        ],
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
    )


def _track(db: AgentDB, coin: str, *, base_id: str | None = None) -> None:
    db.upsert_open_copy(
        investment_base_id=base_id or coin.lower(),
        base_short_id=f"{coin.lower()}Short",
        ticker=coin,
        portfolio_id=None,
        notification_id=f"n-{coin.lower()}",
        side="long",
        leverage=10,
        trader="atrader",
    )


def _hud(tmp_path: Path, monkeypatch, *coins: str):
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "invo_agent.hud._fetch_hl_pnl", lambda _settings: _pnl_rows(*coins)
    )
    migrate_state(settings)
    return collect_hud(settings, with_pnl=True, supervisor_ok=True), settings


def _row(snap, coin: str):
    return next(p for p in snap.positions if p.coin == coin)


def test_a_position_with_no_copy_is_flagged(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    db.close()

    snap, _ = _hud(tmp_path, monkeypatch, "AAA")

    assert _row(snap, "AAA").untracked is True
    assert snap.untracked == 1


def test_a_tracked_position_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    _track(db, "AAA")
    db.close()

    snap, _ = _hud(tmp_path, monkeypatch, "AAA")

    assert _row(snap, "AAA").untracked is False
    assert snap.untracked == 0


def test_only_the_stranger_is_flagged(tmp_path: Path, monkeypatch) -> None:
    """A mixed book must not tar the tracked rows with the untracked one."""
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    _track(db, "AAA")
    db.close()

    snap, _ = _hud(tmp_path, monkeypatch, "AAA", "BBB")

    assert _row(snap, "AAA").untracked is False
    assert _row(snap, "BBB").untracked is True
    assert snap.untracked == 1


def test_untracked_since_comes_from_the_first_sighting(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    stamp = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    db.set_meta(f"{GHOST_SEEN_META_PREFIX}AAA", stamp)
    db.close()

    snap, _ = _hud(tmp_path, monkeypatch, "AAA")

    assert _row(snap, "AAA").untracked_since == stamp


def test_untracked_since_is_none_before_the_poller_has_seen_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The flag does not wait on the poller's stamp to be truthful."""
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    db.close()

    snap, _ = _hud(tmp_path, monkeypatch, "AAA")

    row = _row(snap, "AAA")
    assert row.untracked is True
    assert row.untracked_since is None


def test_an_empty_book_reports_no_strangers(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    db.close()

    snap, _ = _hud(tmp_path, monkeypatch)

    assert snap.positions == []
    assert snap.untracked == 0


def test_the_flag_survives_the_json_round_trip(tmp_path: Path, monkeypatch) -> None:
    """hud.json is what the sidecar and the terminal HUD actually read."""
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    _track(db, "AAA")
    db.close()

    snap, settings = _hud(tmp_path, monkeypatch, "AAA", "BBB")
    write_hud_file(settings, snap)
    reloaded = load_hud_file(settings)

    assert reloaded is not None
    assert reloaded.untracked == 1
    assert _row(reloaded, "BBB").untracked is True
    assert _row(reloaded, "AAA").untracked is False


def test_an_older_hud_file_without_the_field_still_loads(
    tmp_path: Path, monkeypatch
) -> None:
    """A file written before this change must not break the reader."""
    import json

    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    db.close()
    snap, settings = _hud(tmp_path, monkeypatch, "AAA")
    write_hud_file(settings, snap)

    path = settings.session_path.expanduser().parent / "hud.json"
    data = json.loads(path.read_text())
    data.pop("untracked", None)
    for row in data["positions"]:
        row.pop("untracked", None)
        row.pop("untracked_since", None)
    path.write_text(json.dumps(data))

    reloaded = load_hud_file(settings)

    assert reloaded is not None
    assert reloaded.untracked == 0
    assert _row(reloaded, "AAA").untracked is False
