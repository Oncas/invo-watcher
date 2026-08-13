from pathlib import Path

from invo_agent.config import Settings
from invo_agent.db import AgentDB, OpenCopy, PendingIntent
from invo_agent.hud import (
    PositionRow,
    _traders_by_coin,
    collect_hud,
    format_age,
    load_hud_file,
    pending_last_tried_age,
)


def test_pending_last_tried_age() -> None:
    assert pending_last_tried_age([]) is None
    assert pending_last_tried_age([{"tried_age_s": 8}, {"tried_age_s": 2}]) == 2
    assert pending_last_tried_age([{"tried_age_s": None}]) is None


def test_format_age() -> None:
    assert format_age(None) == "?"
    assert format_age(4) == "4s"
    assert format_age(90) == "1.5m"
    assert format_age(720) == "12m"


def test_traders_by_coin() -> None:
    copies = [
        OpenCopy(
            investment_base_id="1",
            base_short_id="aaa",
            ticker="ETH",
            portfolio_id=None,
            opened_at="t",
            last_notification_id=None,
            trader="beta_trader",
        ),
        OpenCopy(
            investment_base_id="2",
            base_short_id="bbb",
            ticker="stx",
            portfolio_id=None,
            opened_at="t",
            last_notification_id=None,
            trader=None,
        ),
    ]
    assert _traders_by_coin(copies) == {"ETH": "beta_trader"}


def test_lookup_trader_ignores_later_same_ticker_close(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    db.log_action(
        action="OPEN",
        summary="OPEN KAITO LONG 5x @epsilon_trader ShortIdB02",
        notification_id="n1",
        dry_run=False,
    )
    db.log_action(
        action="CLOSE",
        summary="CLOSE KAITO LONG 5x @alpha_trader ShortIdI09",
        notification_id="n2",
        dry_run=False,
    )
    assert db.lookup_trader_from_logs(short_id="ShortIdB02") == "epsilon_trader"
    assert db.lookup_trader_from_logs(ticker="KAITO") == "epsilon_trader"
    assert db.lookup_trader_from_logs(short_id="missing") is None
    db.close()


def test_collect_hud_corrects_trader_stolen_by_later_close(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
    )
    db = AgentDB(settings.db_path)
    db.upsert_open_copy(
        investment_base_id="00000000-0000-4000-8000-000000000014",
        base_short_id="ShortIdB02",
        ticker="KAITO",
        portfolio_id=None,
        notification_id="n1",
        trader="alpha_trader",
    )
    db.log_action(
        action="OPEN",
        summary="OPEN KAITO LONG 5x @epsilon_trader ShortIdB02",
        notification_id="n1",
        dry_run=False,
    )
    db.log_action(
        action="CLOSE",
        summary="CLOSE KAITO LONG 5x @alpha_trader ShortIdI09",
        notification_id="n2",
        dry_run=False,
    )
    db.close()

    collect_hud(settings, with_pnl=False, supervisor_ok=True)
    db = AgentDB(settings.db_path)
    copy = db.get_open_copy("00000000-0000-4000-8000-000000000014")
    db.close()
    assert copy is not None
    assert copy.trader == "epsilon_trader"


def test_collect_hud_pending_includes_trader(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
    )
    db = AgentDB(settings.db_path)
    db.upsert_open_copy(
        investment_base_id="eth",
        base_short_id="ShortIdN14",
        ticker="ETH",
        portfolio_id=None,
        notification_id="n1",
    )
    db.log_action(
        action="OPEN",
        summary="OPEN ETH LONG 20x @beta_trader ShortIdN14",
        notification_id="n1",
        dry_run=False,
    )
    now = "2026-08-13T12:00:00+00:00"
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id="btc",
            coin="BTC",
            side="long",
            reason="same_coin",
            created_at=now,
            updated_at=now,
            leverage=40,
            trader="gamma_trader",
            last_tried_at="2026-08-13T12:00:10+00:00",
            summary="OPEN BTC LONG 40x @gamma_trader ShortIdJ10",
        )
    )
    db.close()

    snap = collect_hud(settings, with_pnl=False, supervisor_ok=True)
    assert snap.pending
    assert snap.pending[0]["trader"] == "gamma_trader"
    assert snap.pending[0]["leverage"] == 40
    assert snap.pending[0]["last_tried_at"] == "2026-08-13T12:00:10+00:00"
    assert snap.pending[0]["tried_age_s"] is not None

    db = AgentDB(settings.db_path)
    copy = db.get_open_copy("eth")
    db.close()
    assert copy is not None
    assert copy.trader == "beta_trader"


def test_load_hud_file_accepts_trader(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
    )
    hud = tmp_path / "hud.json"
    hud.write_text(
        """
        {
          "mode": "LIVE",
          "launchd_loaded": true,
          "last_poll_at": null,
          "poll_age_seconds": 1,
          "healthy": true,
          "open_copies": 1,
          "last_action": null,
          "last_action_at": null,
          "recent": [],
          "logged_in": true,
          "position_pct": 5,
          "positions": [
            {
              "coin": "ETH",
              "side": "LONG",
              "leverage": 20,
              "size": 0.4,
              "entry_px": 1800,
              "position_value": 14400,
              "unrealized_pnl": 1.2,
              "return_on_equity": 0.02,
              "trader": "beta_trader"
            }
          ],
          "pending": []
        }
        """
    )
    monkeypatch.setattr("invo_agent.hud.hud_path", lambda _s: hud)
    snap = load_hud_file(settings)
    assert snap is not None
    assert snap.positions[0].trader == "beta_trader"
    assert isinstance(snap.positions[0], PositionRow)
