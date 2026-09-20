from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from invo_agent.logs_cli import plan_transfers
from invo_agent.research import ResearchArchive
from invo_agent.research_cli import research_app
from invo_agent.research_sync import SyncState

runner = CliRunner()


def _seed(path: Path, n: int = 3) -> str:
    with ResearchArchive(path) as archive:
        for i in range(n):
            archive.append("investment", {"id": f"t{i}"}, deduplicate=True)
        return archive.archive_uuid


# --- logs pull planning -----------------------------------------------------


def test_plan_skips_files_already_pulled(tmp_path: Path) -> None:
    (tmp_path / "2026-09-17.log").write_text("x")
    plan = plan_transfers(
        ["2026-09-17.log", "2026-09-18.log"], tmp_path, today="2026-09-19"
    )
    assert plan == ["2026-09-18.log"]


def test_plan_skips_todays_still_growing_file(tmp_path: Path) -> None:
    plan = plan_transfers(
        ["2026-09-18.log", "2026-09-19.log"], tmp_path, today="2026-09-19"
    )
    assert plan == ["2026-09-18.log"]


def test_plan_ignores_the_live_files(tmp_path: Path) -> None:
    plan = plan_transfers(
        ["agent.log", "events.jsonl", "2026-09-18.log"], tmp_path, today="2026-09-19"
    )
    assert plan == ["2026-09-18.log"]


def test_plan_takes_both_dated_kinds(tmp_path: Path) -> None:
    plan = plan_transfers(
        ["2026-09-18.log", "2026-09-18.events.jsonl"], tmp_path, today="2026-09-19"
    )
    assert plan == ["2026-09-18.events.jsonl", "2026-09-18.log"]


def test_plan_ignores_unrelated_names(tmp_path: Path) -> None:
    plan = plan_transfers(
        ["notes.txt", "2026-13-99.log", "2026-09-18.log"], tmp_path, today="2026-09-19"
    )
    assert plan == ["2026-09-18.log"]


# --- research export --------------------------------------------------------


def test_export_writes_ndjson_to_stdout(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    uuid = _seed(src)
    result = runner.invoke(
        research_app, ["export", "--database", str(src), "--after-id", "0"]
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
    assert lines[0]["type"] == "header"
    assert lines[0]["archive_uuid"] == uuid
    assert lines[-1]["type"] == "complete"
    assert sum(1 for x in lines if x["type"] == "row") == 3


def test_export_respects_the_watermark(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _seed(src, 5)
    result = runner.invoke(
        research_app,
        ["export", "--database", str(src), "--after-id", "3",
         "--since", "2030-01-01T00:00:00+00:00"],
    )
    lines = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
    rows = [x for x in lines if x["type"] == "row"]
    assert [r["id"] for r in rows] == [4, 5]


# --- research pull --from-file ----------------------------------------------


def test_pull_from_file_merges_a_backfill(tmp_path: Path) -> None:
    backfill = tmp_path / "bf.db"
    _seed(backfill, 4)
    dest = tmp_path / "dest"
    dest.mkdir()
    local = dest / "research.db"
    result = runner.invoke(
        research_app,
        ["pull", "--from-file", str(backfill), "--dest", str(dest),
         "--database", str(local)],
    )
    assert result.exit_code == 0, result.output
    with ResearchArchive(local) as archive:
        assert archive.conn.execute(
            "SELECT COUNT(*) FROM observations").fetchone()[0] == 4


def test_pull_from_file_leaves_the_vps_watermark_alone(tmp_path: Path) -> None:
    backfill = tmp_path / "bf.db"
    _seed(backfill, 2)
    dest = tmp_path / "dest"
    state = SyncState()
    vps = state.source("vps")
    vps.archive_uuid = "the-real-vps"
    vps.last_id = 51681
    state.save(dest)

    runner.invoke(
        research_app,
        ["pull", "--from-file", str(backfill), "--dest", str(dest),
         "--database", str(dest / "research.db")],
    )
    after = SyncState.load(dest).source("vps")
    assert after.last_id == 51681
    assert after.archive_uuid == "the-real-vps"


# --- research prune ---------------------------------------------------------


def _write_ack(dest: Path, archive: ResearchArchive) -> Path:
    top = archive.conn.execute("SELECT COALESCE(MAX(id),0) FROM observations").fetchone()[0]
    state = SyncState()
    src = state.source("vps")
    src.archive_uuid = archive.archive_uuid
    src.last_id = top
    src.last_max_id = top
    src.last_pull_at = datetime.now(UTC).isoformat()
    src.last_pull_status = "ok"
    state.save(dest)
    return dest / "sync-state.json"


def test_prune_requires_an_ack(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _seed(src)
    result = runner.invoke(research_app, ["prune", "--database", str(src), "--yes"])
    assert result.exit_code != 0


def test_prune_refuses_a_foreign_ack(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _seed(src)
    dest = tmp_path / "dest"
    state = SyncState()
    other = state.source("vps")
    other.archive_uuid = "not-this-archive"
    other.last_max_id = 1
    other.last_pull_at = datetime.now(UTC).isoformat()
    state.save(dest)
    result = runner.invoke(
        research_app,
        ["prune", "--database", str(src), "--ack", str(dest / "sync-state.json"), "--yes"],
    )
    assert result.exit_code != 0
    assert "archive" in result.output.lower()


def test_prune_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        archive.append("old", {"a": 1}, deduplicate=True,
                       observed_at="2020-01-01T00:00:00+00:00")
        ack = _write_ack(tmp_path / "dest", archive)
    result = runner.invoke(
        research_app, ["prune", "--database", str(src), "--ack", str(ack)]
    )
    assert result.exit_code == 0, result.output
    with ResearchArchive(src) as archive:
        assert archive.conn.execute(
            "SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_prune_applies_with_yes(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        archive.append("old", {"a": 1}, deduplicate=True,
                       observed_at="2020-01-01T00:00:00+00:00")
        ack = _write_ack(tmp_path / "dest", archive)
    result = runner.invoke(
        research_app, ["prune", "--database", str(src), "--ack", str(ack), "--yes"]
    )
    assert result.exit_code == 0, result.output
    with ResearchArchive(src) as archive:
        assert archive.conn.execute(
            "SELECT COUNT(*) FROM observations").fetchone()[0] == 0
