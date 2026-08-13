from pathlib import Path

from invo_agent.db import AgentDB, PendingIntent


def test_pending_crud_and_one_slot_per_coin(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    now = "2026-08-13T12:00:00+00:00"
    first = PendingIntent(
        investment_base_id="aaa",
        coin="SOL",
        side="long",
        reason="price",
        created_at=now,
        updated_at=now,
        trader_entry=76.12,
        summary="OPEN SOL",
    )
    db.upsert_pending_intent(first)
    second = PendingIntent(
        investment_base_id="bbb",
        coin="SOL",
        side="long",
        reason="margin",
        created_at="2026-08-13T12:01:00+00:00",
        updated_at="2026-08-13T12:01:00+00:00",
    )
    db.upsert_pending_intent(second)
    assert db.get_pending_for_coin("SOL").investment_base_id == "aaa"
    assert db.get_pending_for_coin("SOL", exclude_base_id="aaa").investment_base_id == "bbb"
    removed = db.delete_pending_for_coin("SOL", exclude_base_id="aaa")
    assert removed == ["bbb"]
    assert db.get_pending_intent("aaa") is not None
    assert db.get_pending_intent("bbb") is None
    db.close()


def test_pending_add_notional_survives_upsert(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    db.upsert_open_copy(
        investment_base_id="eth",
        base_short_id="DN14",
        ticker="ETH",
        portfolio_id=None,
        notification_id="n1",
        our_entry_px=1880.2,
        side="long",
        size=0.47,
        leverage=20,
    )
    db.set_pending_add_notional("eth", 8.5)
    db.upsert_open_copy(
        investment_base_id="eth",
        base_short_id="DN14",
        ticker="ETH",
        portfolio_id=None,
        notification_id="n2",
    )
    copy = db.get_open_copy("eth")
    assert copy is not None
    assert copy.pending_add_notional == 8.5
    db.close()


def test_trader_survives_upsert(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    db.upsert_open_copy(
        investment_base_id="eth",
        base_short_id="DN14",
        ticker="ETH",
        portfolio_id=None,
        notification_id="n1",
        trader="beta_trader",
    )
    db.upsert_open_copy(
        investment_base_id="eth",
        base_short_id="DN14",
        ticker="ETH",
        portfolio_id=None,
        notification_id="n2",
    )
    copy = db.get_open_copy("eth")
    assert copy is not None
    assert copy.trader == "beta_trader"

    now = "2026-08-13T12:00:00+00:00"
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id="btc",
            coin="BTC",
            side="long",
            reason="same_coin",
            created_at=now,
            updated_at=now,
            trader="gamma_trader",
            summary="OPEN BTC LONG 40x ShortIdJ10",
        )
    )
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id="btc",
            coin="BTC",
            side="long",
            reason="same_coin",
            created_at=now,
            updated_at=now,
            summary="OPEN BTC LONG 40x ShortIdJ10",
        )
    )
    intent = db.get_pending_intent("btc")
    assert intent is not None
    assert intent.trader == "gamma_trader"
    db.close()


def test_last_tried_survives_upsert(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    now = "2026-08-13T12:00:00+00:00"
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id="btc",
            coin="BTC",
            side="long",
            reason="price",
            created_at=now,
            updated_at=now,
            last_tried_at="2026-08-13T12:01:00+00:00",
        )
    )
    db.mark_pending_tried("btc")
    first = db.get_pending_intent("btc")
    assert first is not None
    assert first.last_tried_at is not None
    tried = first.last_tried_at
    db.upsert_pending_intent(
        PendingIntent(
            investment_base_id="btc",
            coin="BTC",
            side="long",
            reason="price",
            created_at=now,
            updated_at=now,
        )
    )
    again = db.get_pending_intent("btc")
    assert again is not None
    assert again.last_tried_at == tried
    db.close()


def test_lookup_trader_from_logs_prefers_short_id(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    db.log_action(
        action="OPEN",
        summary="OPEN BTC LONG 30x @wrong_trader oldShort",
        notification_id="n0",
        dry_run=False,
    )
    db.log_action(
        action="OPEN",
        summary="OPEN ETH LONG 20x @beta_trader ShortIdN14",
        notification_id="n1",
        dry_run=False,
    )
    assert db.lookup_trader_from_logs(short_id="ShortIdN14") == "beta_trader"
    assert db.lookup_trader_from_logs(ticker="ETH") == "beta_trader"
    db.close()
