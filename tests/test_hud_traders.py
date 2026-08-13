from pathlib import Path

from invo_agent.config import Settings
from invo_agent.db import AgentDB, OpenCopy, PendingIntent
from invo_agent.hud import (
    PositionRow,
    _opened_at_by_coin,
    _traders_by_coin,
    collect_hud,
    format_age,
    load_hud_file,
    pending_kind,
    pending_last_tried_age,
)


def test_pending_kind() -> None:
    assert pending_kind(None) == "OPEN"
    assert pending_kind("UPDATE BTC LONG 33x @beta_trader") == "OPEN"
    assert pending_kind(kind="UPDATE") == "UPDATE"
    assert pending_kind(kind="OPEN") == "OPEN"


def test_pending_last_tried_age() -> None:
    assert pending_last_tried_age([]) is None
    assert pending_last_tried_age([{"tried_age_s": 8}, {"tried_age_s": 2}]) == 2
    assert pending_last_tried_age([{"tried_age_s": None}]) is None


def test_format_age() -> None:
    assert format_age(None) == "?"
    assert format_age(4) == "4s"
    assert format_age(90) == "1m 30s"
    assert format_age(720) == "12m"
    assert format_age(5400) == "1h 30m"
    assert format_age(3600) == "1h"


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
    assert _opened_at_by_coin(copies) == {"ETH": "t", "STX": "t"}


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
    db.log_action(
        action="SKIP_SAME_COIN_OPEN",
        summary=(
            "OPEN KAITO LONG 5x @alpha_trader ShortIdI09: Already mimicking KAITO "
            "via ShortIdB02 (long); skip second same-coin open"
        ),
        notification_id="n3",
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
    db.log_action(
        action="SKIP_SAME_COIN_OPEN",
        summary=(
            "OPEN KAITO LONG 5x @alpha_trader ShortIdI09: Already mimicking KAITO "
            "via ShortIdB02 (long); skip second same-coin open"
        ),
        notification_id="n3",
        dry_run=False,
    )
    db.close()

    collect_hud(settings, with_pnl=False, supervisor_ok=True)
    db = AgentDB(settings.db_path)
    copy = db.get_open_copy("00000000-0000-4000-8000-000000000014")
    db.close()
    assert copy is not None
    assert copy.trader == "epsilon_trader"


def test_collect_hud_corrects_trader_stolen_by_via_skip(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
    )
    db = AgentDB(settings.db_path)
    db.upsert_open_copy(
        investment_base_id="btc-1",
        base_short_id="ShortIdK11",
        ticker="BTC",
        portfolio_id=None,
        notification_id="n1",
        trader="beta_trader",
    )
    db.log_action(
        action="OPEN",
        summary="OPEN BTC LONG 30x @gamma_trader ShortIdK11",
        notification_id="n1",
        dry_run=False,
    )
    db.log_action(
        action="SKIP_SAME_COIN_OPEN",
        summary=(
            "OPEN BTC LONG 32x @beta_trader wPQAxYfNjQ: Already mimicking BTC "
            "via ShortIdK11 (long); skip second same-coin open"
        ),
        notification_id="n2",
        dry_run=False,
    )
    db.close()

    collect_hud(settings, with_pnl=False, supervisor_ok=True)
    db = AgentDB(settings.db_path)
    copy = db.get_open_copy("btc-1")
    db.close()
    assert copy is not None
    assert copy.trader == "gamma_trader"


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
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id="sol",
            coin="SOL",
            side="long",
            reason="same_coin",
            created_at=now,
            updated_at=now,
            leverage=20,
            trader="beta_trader",
            summary="UPDATE SOL LONG 20x @beta_trader ShortIdC03",
        )
    )
    db.upsert_open_copy(
        investment_base_id="dot",
        base_short_id="Rq48",
        ticker="DOT",
        portfolio_id=None,
        notification_id="n2",
        side="long",
        leverage=10,
        trader="gamma_trader",
    )
    db.set_pending_add_notional("dot", 12.5)
    db.close()

    snap = collect_hud(settings, with_pnl=False, supervisor_ok=True)
    by_coin = {row["coin"]: row for row in snap.pending}
    assert by_coin["BTC"]["kind"] == "OPEN"
    assert by_coin["BTC"]["trader"] == "gamma_trader"
    assert by_coin["BTC"]["leverage"] == 40
    assert by_coin["BTC"]["last_tried_at"] == "2026-08-13T12:00:10+00:00"
    assert by_coin["BTC"]["tried_age_s"] is not None
    assert by_coin["SOL"]["kind"] == "OPEN"
    assert by_coin["DOT"]["kind"] == "UPDATE"
    assert by_coin["DOT"]["reason"] == "size"

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
              "trader": "beta_trader",
              "open_age_s": 5400
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
    assert snap.positions[0].open_age_s == 5400
    assert isinstance(snap.positions[0], PositionRow)
