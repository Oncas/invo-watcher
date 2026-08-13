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
