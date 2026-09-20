from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from invo_agent.research import ResearchArchive, SchemaTooNewError
from invo_agent.research_sync import (
    PROTOCOL,
    ArchiveMismatch,
    PruneRefused,
    PullBusyError,
    SourceState,
    SyncProtocolError,
    SyncState,
    export_stream,
    merge_archive_file,
    merge_stream,
    prune_archive,
    pull_lock,
    pull_source,
)


def _rows(archive: ResearchArchive, sql: str, *args: object) -> list[tuple]:
    return archive.conn.execute(sql, args).fetchall()


# --- archive identity -------------------------------------------------------


def test_archive_uuid_is_stable_across_opens(tmp_path: Path) -> None:
    path = tmp_path / "research.db"
    with ResearchArchive(path) as first:
        original = first.archive_uuid
    assert original
    with ResearchArchive(path) as second:
        assert second.archive_uuid == original


def test_separate_archives_get_separate_uuids(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "a.db") as a, ResearchArchive(tmp_path / "b.db") as b:
        assert a.archive_uuid != b.archive_uuid


# --- dedupe keys ------------------------------------------------------------


def test_append_without_deduplicate_still_writes_a_key(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        archive.append("capture_gap", {"reason": "restart"})
        keys = _rows(archive, "SELECT dedupe_key FROM observations")
        assert len(keys) == 1
        assert keys[0][0] is not None


def test_distinct_rows_with_identical_content_get_distinct_keys(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        archive.append("capture_gap", {"reason": "restart"})
        archive.append("capture_gap", {"reason": "restart"})
        keys = {row[0] for row in _rows(archive, "SELECT dedupe_key FROM observations")}
        assert len(keys) == 2


def test_content_dedupe_still_collapses_repeats(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        assert archive.append("investment", {"id": "t1"}, deduplicate=True) is True
        assert archive.append("investment", {"id": "t1"}, deduplicate=True) is False
        rows = _rows(archive, "SELECT seen_count FROM observations")
        assert len(rows) == 1
        assert rows[0][0] == 2


def test_content_key_is_namespace_free_so_archives_agree(tmp_path: Path) -> None:
    """A content hash must match across archives, or merge would duplicate."""
    with ResearchArchive(tmp_path / "a.db") as a, ResearchArchive(tmp_path / "b.db") as b:
        a.append("investment", {"id": "t1"}, deduplicate=True)
        b.append("investment", {"id": "t1"}, deduplicate=True)
        key_a = _rows(a, "SELECT dedupe_key FROM observations")[0][0]
        key_b = _rows(b, "SELECT dedupe_key FROM observations")[0][0]
        assert key_a == key_b


def test_synthesized_key_is_namespaced_by_archive(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        archive.append("capture_gap", {"reason": "restart"})
        key = _rows(archive, "SELECT dedupe_key FROM observations")[0][0]
        assert key.startswith(f"{archive.archive_uuid}:")


# --- legacy NULL-key migration ---------------------------------------------


def _legacy_archive(path: Path, count: int = 3) -> None:
    """An archive as the pre-migration code left it: NULL dedupe_keys, no meta."""
    with ResearchArchive(path) as archive:
        for i in range(count):
            archive.append("capture_gap", {"i": i})
        archive.conn.execute("UPDATE observations SET dedupe_key = NULL")
        archive.conn.execute("DROP TABLE IF EXISTS archive_meta")
        archive.conn.commit()


def test_migration_backfills_null_keys(tmp_path: Path) -> None:
    path = tmp_path / "r.db"
    _legacy_archive(path)
    with ResearchArchive(path) as archive:
        nulls = _rows(archive, "SELECT COUNT(*) FROM observations WHERE dedupe_key IS NULL")
        assert nulls[0][0] == 0


def test_migration_keys_are_archive_uuid_and_row_id(tmp_path: Path) -> None:
    path = tmp_path / "r.db"
    _legacy_archive(path)
    with ResearchArchive(path) as archive:
        for row_id, key in _rows(archive, "SELECT id, dedupe_key FROM observations"):
            assert key == f"{archive.archive_uuid}:{row_id}"


def test_migration_is_deterministic_across_reruns(tmp_path: Path) -> None:
    path = tmp_path / "r.db"
    _legacy_archive(path)
    with ResearchArchive(path) as archive:
        first = dict(_rows(archive, "SELECT id, dedupe_key FROM observations"))
    with ResearchArchive(path) as archive:
        second = dict(_rows(archive, "SELECT id, dedupe_key FROM observations"))
    assert first == second


def test_migration_preserves_existing_content_keys(tmp_path: Path) -> None:
    path = tmp_path / "r.db"
    with ResearchArchive(path) as archive:
        archive.append("investment", {"id": "t1"}, deduplicate=True)
        before = _rows(archive, "SELECT dedupe_key FROM observations")[0][0]
        archive.conn.execute("DROP TABLE IF EXISTS archive_meta")
        archive.conn.commit()
    with ResearchArchive(path) as archive:
        after = _rows(archive, "SELECT dedupe_key FROM observations")[0][0]
    assert after == before


# --- schema version guard ---------------------------------------------------


def test_open_refuses_archive_from_newer_code(tmp_path: Path) -> None:
    path = tmp_path / "r.db"
    with ResearchArchive(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaTooNewError):
        ResearchArchive(path)


def test_open_accepts_archive_from_older_code(tmp_path: Path) -> None:
    path = tmp_path / "r.db"
    with ResearchArchive(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with ResearchArchive(path) as archive:
        assert archive.archive_uuid


# --- export protocol --------------------------------------------------------


def _seed(archive: ResearchArchive, n: int, kind: str = "investment") -> None:
    for i in range(n):
        archive.append(kind, {"id": f"t{i}"}, deduplicate=True)


def test_export_emits_header_rows_then_complete(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _seed(archive, 3)
        out = list(export_stream(archive, after_id=0, since="1970-01-01T00:00:00+00:00"))
    assert out[0]["type"] == "header"
    assert out[-1]["type"] == "complete"
    assert [line["type"] for line in out[1:-1]] == ["row"] * 3


def test_export_header_carries_protocol_and_identity(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        header = next(export_stream(archive, after_id=0, since="1970-01-01T00:00:00+00:00"))
        assert header["protocol"] == PROTOCOL
        assert header["archive_uuid"] == archive.archive_uuid
        assert header["remote_now"]


def test_export_complete_boundary_equals_header_remote_now(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _seed(archive, 2)
        out = list(export_stream(archive, after_id=0, since="1970-01-01T00:00:00+00:00"))
    assert out[-1]["update_boundary"] == out[0]["remote_now"]


def test_export_complete_carries_max_id(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _seed(archive, 4)
        out = list(export_stream(archive, after_id=0, since="1970-01-01T00:00:00+00:00"))
    assert out[-1]["max_id"] == 4


def test_export_new_rows_are_ascending_by_id(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _seed(archive, 5)
        rows = [x for x in export_stream(archive, after_id=0, since="1970-01-01T00:00:00+00:00")
                if x["type"] == "row"]
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids)
    assert all(r["phase"] == "new" for r in rows)


def test_export_splits_updates_from_new(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _seed(archive, 3)
        archive.conn.execute(
            "UPDATE observations SET last_seen_at = '2030-01-01T00:00:00+00:00' WHERE id = 1")
        archive.conn.commit()
        _seed(archive, 2, kind="later")
        rows = [x for x in export_stream(archive, after_id=3, since="2029-01-01T00:00:00+00:00")
                if x["type"] == "row"]
    updates = [r for r in rows if r["phase"] == "update"]
    new = [r for r in rows if r["phase"] == "new"]
    assert [r["id"] for r in updates] == [1]
    assert [r["id"] for r in new] == [4, 5]


def test_export_excludes_old_rows_not_recently_seen(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _seed(archive, 3)
        archive.conn.execute(
            "UPDATE observations SET last_seen_at = '2000-01-01T00:00:00+00:00'")
        archive.conn.commit()
        rows = [x for x in export_stream(archive, after_id=3, since="2029-01-01T00:00:00+00:00")
                if x["type"] == "row"]
    assert rows == []


def test_row_committed_during_export_is_included_or_beyond_the_boundary(tmp_path: Path) -> None:
    """The boundary must never claim a row it did not carry."""
    with ResearchArchive(tmp_path / "r.db") as archive:
        _seed(archive, 2)
        stream = export_stream(archive, after_id=0, since="1970-01-01T00:00:00+00:00")
        header = next(stream)
        archive.append("investment", {"id": "late"}, deduplicate=True)
        rest = list(stream)
    payloads = [r.get("payload", "") for r in rest if r["type"] == "row"]
    carried = any("late" in p for p in payloads)
    if not carried:
        late = archive_row_last_seen(tmp_path / "r.db", "late")
        assert late > header["remote_now"]


def archive_row_last_seen(path: Path, needle: str) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT last_seen_at FROM observations WHERE payload LIKE ?", (f"%{needle}%",)
        ).fetchone()
        return str(row[0])
    finally:
        conn.close()


# --- merge ------------------------------------------------------------------


def _export(path: Path, after_id: int = 0, since: str = "1970-01-01T00:00:00+00:00") -> list:
    with ResearchArchive(path) as archive:
        return list(export_stream(archive, after_id=after_id, since=since))


def _snapshot(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT dedupe_key, payload, seen_count, last_seen_at"
            "  FROM observations ORDER BY dedupe_key"
        ).fetchall()
    finally:
        conn.close()


def test_merge_inserts_rows_into_an_empty_archive(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 3)
    with ResearchArchive(tmp_path / "dst.db") as dst:
        result = merge_stream(dst, _export(src))
        assert result.complete is True
        assert _rows(dst, "SELECT COUNT(*) FROM observations")[0][0] == 3


def test_merging_the_same_export_twice_is_a_no_op(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 4)
    dst_path = tmp_path / "dst.db"
    with ResearchArchive(dst_path) as dst:
        merge_stream(dst, _export(src))
    first = _snapshot(dst_path)
    with ResearchArchive(dst_path) as dst:
        merge_stream(dst, _export(src))
    assert _snapshot(dst_path) == first


def test_replaying_non_deduplicated_rows_does_not_duplicate_them(tmp_path: Path) -> None:
    """Backfill markers and capture gaps have no content hash; they still must not clone."""
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        archive.append("backfill_started", {"run": 1})
        archive.append("capture_gap", {"reason": "restart"})
    dst_path = tmp_path / "dst.db"
    with ResearchArchive(dst_path) as dst:
        merge_stream(dst, _export(src))
        merge_stream(dst, _export(src))
        assert _rows(dst, "SELECT COUNT(*) FROM observations")[0][0] == 2


def test_merge_takes_max_seen_count_and_never_increments(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        archive.append("investment", {"id": "t1"}, deduplicate=True)
        archive.conn.execute("UPDATE observations SET seen_count = 7")
        archive.conn.commit()
    with ResearchArchive(tmp_path / "dst.db") as dst:
        merge_stream(dst, _export(src))
        merge_stream(dst, _export(src))
        assert _rows(dst, "SELECT seen_count FROM observations")[0][0] == 7


def test_merge_keeps_the_higher_local_seen_count(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        archive.append("investment", {"id": "t1"}, deduplicate=True)
    with ResearchArchive(tmp_path / "dst.db") as dst:
        merge_stream(dst, _export(src))
        dst.conn.execute("UPDATE observations SET seen_count = 40")
        dst.conn.commit()
        merge_stream(dst, _export(src))
        assert _rows(dst, "SELECT seen_count FROM observations")[0][0] == 40


def test_merge_is_null_safe_on_incoming_last_seen_at(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        archive.append("investment", {"id": "t1"}, deduplicate=True)
        # last_seen_at is the one genuinely nullable column of the pair;
        # seen_count is NOT NULL in the schema and cannot arrive empty.
        archive.conn.execute("UPDATE observations SET last_seen_at = NULL")
        archive.conn.commit()
    with ResearchArchive(tmp_path / "dst.db") as dst:
        merge_stream(dst, _export(src))
        merge_stream(dst, _export(src))
        count, seen = _rows(dst, "SELECT seen_count, last_seen_at FROM observations")[0]
        assert count == 1
        assert seen is None


def test_merge_is_null_safe_when_local_last_seen_is_missing(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        archive.append("investment", {"id": "t1"}, deduplicate=True)
    with ResearchArchive(tmp_path / "dst.db") as dst:
        merge_stream(dst, _export(src))
        dst.conn.execute("UPDATE observations SET last_seen_at = NULL")
        dst.conn.commit()
        merge_stream(dst, _export(src))
        seen = _rows(dst, "SELECT last_seen_at FROM observations")[0][0]
        assert seen is not None


def test_merge_assigns_local_ids_not_remote_ones(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 2)
        archive.conn.execute("UPDATE observations SET id = id + 5000")
        archive.conn.commit()
    with ResearchArchive(tmp_path / "dst.db") as dst:
        merge_stream(dst, _export(src))
        ids = [r[0] for r in _rows(dst, "SELECT id FROM observations ORDER BY id")]
        assert ids == [1, 2]


def test_merge_reports_the_remote_watermark(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 3)
        uuid = archive.archive_uuid
    with ResearchArchive(tmp_path / "dst.db") as dst:
        result = merge_stream(dst, _export(src))
    assert result.archive_uuid == uuid
    assert result.max_id == 3
    assert result.last_id == 3
    assert result.update_boundary


def test_merge_rejects_an_unknown_line_type(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "dst.db") as dst:
        stream = [{"type": "header", "protocol": PROTOCOL, "archive_uuid": "x",
                   "remote_now": "2026-01-01T00:00:00+00:00"},
                  {"type": "wat"}]
        with pytest.raises(SyncProtocolError):
            merge_stream(dst, stream)


def test_merge_rejects_an_unknown_protocol(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "dst.db") as dst:
        stream = [{"type": "header", "protocol": 99, "archive_uuid": "x",
                   "remote_now": "2026-01-01T00:00:00+00:00"}]
        with pytest.raises(SyncProtocolError):
            merge_stream(dst, stream)


def test_merge_without_a_complete_marker_is_partial(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 3)
    truncated = _export(src)[:-1]
    with ResearchArchive(tmp_path / "dst.db") as dst:
        result = merge_stream(dst, truncated)
        assert result.complete is False
        assert result.update_boundary is None
        assert _rows(dst, "SELECT COUNT(*) FROM observations")[0][0] == 3


def test_only_new_rows_advance_the_watermark(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 3)
        archive.conn.execute(
            "UPDATE observations SET last_seen_at = '2030-01-01T00:00:00+00:00' WHERE id = 2")
        archive.conn.commit()
    stream = _export(src, after_id=3, since="2029-01-01T00:00:00+00:00")
    with ResearchArchive(tmp_path / "dst.db") as dst:
        result = merge_stream(dst, stream, last_id=3)
    assert result.last_id == 3


# --- sync state -------------------------------------------------------------


def test_sync_state_round_trips(tmp_path: Path) -> None:
    state = SyncState()
    src = state.source("vps")
    src.archive_uuid = "abc"
    src.last_id = 12
    src.last_max_id = 12
    src.last_pull_at = "2026-09-19T14:03:11+00:00"
    src.last_pull_status = "ok"
    state.save(tmp_path)

    again = SyncState.load(tmp_path)
    assert again.source("vps").archive_uuid == "abc"
    assert again.source("vps").last_id == 12
    assert again.source("vps").last_pull_at == "2026-09-19T14:03:11+00:00"


def test_sync_state_load_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert SyncState.load(tmp_path).source("vps").last_id == 0


def test_sync_state_save_leaves_no_temp_file(tmp_path: Path) -> None:
    state = SyncState()
    state.source("vps").last_id = 3
    state.save(tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "sync-state.json").is_file()


def test_sync_state_save_does_not_corrupt_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = SyncState()
    state.source("vps").last_id = 1
    state.save(tmp_path)
    original = (tmp_path / "sync-state.json").read_text()

    state.source("vps").last_id = 999
    monkeypatch.setattr(
        "invo_agent.research_sync.os.replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        state.save(tmp_path)
    assert (tmp_path / "sync-state.json").read_text() == original


# --- pull lock --------------------------------------------------------------


def test_second_pull_is_refused_while_one_holds_the_lock(tmp_path: Path) -> None:
    with pull_lock(tmp_path):
        with pytest.raises(PullBusyError):
            with pull_lock(tmp_path):
                pass


def test_lock_is_released_after_the_pull(tmp_path: Path) -> None:
    with pull_lock(tmp_path):
        pass
    with pull_lock(tmp_path):
        pass


# --- prune ------------------------------------------------------------------


def _aged(archive: ResearchArchive, kind: str, days_old: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    archive.append(kind, {"k": kind, "age": days_old}, deduplicate=True, observed_at=stamp)


def _ack_for(archive: ResearchArchive, **over: object) -> SourceState:
    top = archive.conn.execute("SELECT COALESCE(MAX(id), 0) FROM observations").fetchone()[0]
    ack = SourceState(
        archive_uuid=archive.archive_uuid,
        last_id=top,
        last_max_id=top,
        rescan_days=14,
        last_pull_at=datetime.now(UTC).isoformat(),
        last_pull_status="ok",
    )
    for key, value in over.items():
        setattr(ack, key, value)
    return ack


def test_prune_refuses_a_foreign_archive_uuid(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "old", 40)
        ack = _ack_for(archive, archive_uuid="someone-elses-archive")
        with pytest.raises(PruneRefused):
            prune_archive(archive, ack, keep_days=14, apply=True)


def test_prune_refuses_a_watermark_above_the_archive(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "old", 40)
        ack = _ack_for(archive, last_max_id=999999)
        with pytest.raises(PruneRefused):
            prune_archive(archive, ack, keep_days=14, apply=True)


def test_prune_refuses_an_ack_from_the_future(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "old", 40)
        ahead = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        with pytest.raises(PruneRefused):
            prune_archive(archive, _ack_for(archive, last_pull_at=ahead), keep_days=14, apply=True)


def test_prune_refuses_without_a_pull_timestamp(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "old", 40)
        with pytest.raises(PruneRefused):
            prune_archive(archive, _ack_for(archive, last_pull_at=None), keep_days=14, apply=True)


def test_prune_deletes_only_rows_older_than_keep_days(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "ancient", 40)
        _aged(archive, "recent", 3)
        report = prune_archive(archive, _ack_for(archive), keep_days=14, apply=True)
        kinds = {r[0] for r in _rows(archive, "SELECT kind FROM observations")}
    assert kinds == {"recent"}
    assert report["deleted"] == 1


def test_prune_keeps_recent_rows_even_below_the_watermark(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "old_but_reseen", 40)
        archive.conn.execute(
            "UPDATE observations SET last_seen_at = ? WHERE kind = 'old_but_reseen'",
            (datetime.now(UTC).isoformat(),),
        )
        archive.conn.commit()
        prune_archive(archive, _ack_for(archive), keep_days=14, apply=True)
        assert _rows(archive, "SELECT COUNT(*) FROM observations")[0][0] == 1


def test_prune_never_touches_rows_above_the_watermark(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "pulled", 40)
        ack = _ack_for(archive)
        _aged(archive, "arrived_after_the_pull", 40)
        prune_archive(archive, ack, keep_days=14, apply=True)
        kinds = {r[0] for r in _rows(archive, "SELECT kind FROM observations")}
    assert kinds == {"arrived_after_the_pull"}


def test_prune_dry_run_deletes_nothing(tmp_path: Path) -> None:
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "ancient", 40)
        report = prune_archive(archive, _ack_for(archive), keep_days=14, apply=False)
        assert report["deleted"] == 1
        assert _rows(archive, "SELECT COUNT(*) FROM observations")[0][0] == 1


def test_prune_with_a_stale_ack_is_allowed_and_prunes_less(tmp_path: Path) -> None:
    """The predicate is anchored to the ack, so an old ack is safe, not fatal."""
    with ResearchArchive(tmp_path / "r.db") as archive:
        _aged(archive, "ancient", 40)
        stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        report = prune_archive(
            archive, _ack_for(archive, last_pull_at=stale), keep_days=14, apply=True
        )
        assert report["stale_days"] >= 29
        assert _rows(archive, "SELECT COUNT(*) FROM observations")[0][0] == 1


# --- pull orchestration -----------------------------------------------------


class _Recorder:
    """Stands in for the ssh pipe; records what window the pull asked for."""

    def __init__(self, lines: list):
        self.lines = lines
        self.calls: list[tuple[int, str]] = []

    def __call__(self, after_id: int, since: str):
        self.calls.append((after_id, since))
        return iter(self.lines)


def test_first_pull_records_the_remote_identity(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 2)
        uuid = archive.archive_uuid
    dest = tmp_path / "dest"
    with ResearchArchive(tmp_path / "dst.db") as dst:
        pull_source(dst, dest, open_stream=_Recorder(_export(src)))
    assert SyncState.load(dest).source("vps").archive_uuid == uuid


def test_pull_refuses_a_different_remote_archive(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 1)
    dest = tmp_path / "dest"
    state = SyncState()
    state.source("vps").archive_uuid = "a-different-archive"
    state.save(dest)
    with ResearchArchive(tmp_path / "dst.db") as dst:
        with pytest.raises(ArchiveMismatch):
            pull_source(dst, dest, open_stream=_Recorder(_export(src)))


NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


def _window_for(tmp_path: Path, last_pull_at: str | None, rescan_days: int = 14) -> str:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 1)
    dest = tmp_path / "dest"
    state = SyncState()
    state.source("vps").last_pull_at = last_pull_at
    state.save(dest)
    recorder = _Recorder(_export(src))
    with ResearchArchive(tmp_path / "dst.db") as dst:
        pull_source(dst, dest, open_stream=recorder, rescan_days=rescan_days, now=NOW)
    return recorder.calls[0][1]


def test_pull_window_always_reaches_back_at_least_the_rescan_days(tmp_path: Path) -> None:
    """A pull an hour ago still re-scans the full rescan window, not just the hour."""
    recent = (NOW - timedelta(hours=1)).isoformat()
    assert _window_for(tmp_path, recent) == (NOW - timedelta(days=14)).isoformat()


def test_pull_window_reaches_back_further_after_a_long_gap(tmp_path: Path) -> None:
    """A 90-day absence re-scans all 90 days, so no update in the gap is missed."""
    ancient = (NOW - timedelta(days=90)).isoformat()
    assert _window_for(tmp_path, ancient) == ancient


def test_pull_window_for_a_first_ever_pull_is_the_rescan_floor(tmp_path: Path) -> None:
    assert _window_for(tmp_path, None) == (NOW - timedelta(days=14)).isoformat()


def test_pull_stores_the_remote_clock_not_the_local_one(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 2)
    stream = _export(src)
    stream[-1]["update_boundary"] = "2001-02-03T04:05:06+00:00"
    dest = tmp_path / "dest"
    with ResearchArchive(tmp_path / "dst.db") as dst:
        pull_source(dst, dest, open_stream=_Recorder(stream))
    assert SyncState.load(dest).source("vps").last_pull_at == "2001-02-03T04:05:06+00:00"


def test_truncated_pull_does_not_advance_the_boundary(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 3)
    dest = tmp_path / "dest"
    with ResearchArchive(tmp_path / "dst.db") as dst:
        result = pull_source(dst, dest, open_stream=_Recorder(_export(src)[:-1]))
    saved = SyncState.load(dest).source("vps")
    assert result.complete is False
    assert saved.last_pull_at is None
    assert saved.last_pull_status == "partial"
    assert saved.last_id == 3


def test_truncated_pull_keeps_rows_it_committed(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 3)
    dst_path = tmp_path / "dst.db"
    with ResearchArchive(dst_path) as dst:
        pull_source(dst, tmp_path / "dest", open_stream=_Recorder(_export(src)[:-1]))
        assert _rows(dst, "SELECT COUNT(*) FROM observations")[0][0] == 3


def test_resuming_after_a_truncated_pull_completes_it(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 3)
    dest = tmp_path / "dest"
    dst_path = tmp_path / "dst.db"
    with ResearchArchive(dst_path) as dst:
        pull_source(dst, dest, open_stream=_Recorder(_export(src)[:-1]))
    with ResearchArchive(dst_path) as dst:
        result = pull_source(dst, dest, open_stream=_Recorder(_export(src)))
        assert result.complete is True
        assert _rows(dst, "SELECT COUNT(*) FROM observations")[0][0] == 3
    assert SyncState.load(dest).source("vps").last_pull_status == "ok"


# --- backfill passthrough ---------------------------------------------------


def test_from_file_does_not_touch_the_vps_watermark(tmp_path: Path) -> None:
    backfill = tmp_path / "bf.db"
    with ResearchArchive(backfill) as archive:
        _seed(archive, 3)
    dest = tmp_path / "dest"
    state = SyncState()
    vps = state.source("vps")
    vps.archive_uuid = "the-real-vps"
    vps.last_id = 51681
    vps.last_max_id = 51681
    vps.last_pull_at = "2026-09-19T14:03:11+00:00"
    state.save(dest)

    with ResearchArchive(tmp_path / "dst.db") as dst:
        merge_archive_file(dst, backfill)

    after = SyncState.load(dest).source("vps")
    assert after.archive_uuid == "the-real-vps"
    assert after.last_id == 51681
    assert after.last_pull_at == "2026-09-19T14:03:11+00:00"


def test_from_file_merges_every_row(tmp_path: Path) -> None:
    backfill = tmp_path / "bf.db"
    with ResearchArchive(backfill) as archive:
        _seed(archive, 4)
    with ResearchArchive(tmp_path / "dst.db") as dst:
        merge_archive_file(dst, backfill)
        assert _rows(dst, "SELECT COUNT(*) FROM observations")[0][0] == 4


def test_importing_the_same_backfill_twice_is_a_no_op(tmp_path: Path) -> None:
    backfill = tmp_path / "bf.db"
    with ResearchArchive(backfill) as archive:
        _seed(archive, 3)
        archive.append("backfill_started", {"run": 1})
    dst_path = tmp_path / "dst.db"
    with ResearchArchive(dst_path) as dst:
        merge_archive_file(dst, backfill)
    first = _snapshot(dst_path)
    with ResearchArchive(dst_path) as dst:
        merge_archive_file(dst, backfill)
    assert _snapshot(dst_path) == first


def test_from_file_rejects_a_stream_mixing_two_archives(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    with ResearchArchive(src) as archive:
        _seed(archive, 2)
    stream = _export(src)
    stream[1]["dedupe_key"] = "other"
    stream.insert(1, {"type": "header", "protocol": PROTOCOL,
                      "archive_uuid": "a-second-archive",
                      "remote_now": "2026-01-01T00:00:00+00:00"})
    with ResearchArchive(tmp_path / "dst.db") as dst:
        with pytest.raises(SyncProtocolError):
            merge_stream(dst, stream)
