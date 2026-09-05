import json
import sqlite3
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from invo_agent.api.client import InvolioAPIError, InvolioClient
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings
from invo_agent.db import AgentDB
from invo_agent.research import BufferedResearch, ResearchArchive, validate_archive_path
from invo_agent.research_backfill import HistoryBackfill, HistoryError
from invo_agent.research_cli import research_app

PID = str(uuid4())
BASE = str(uuid4())


def trade(**fields):
    return {"id": "trade1", "baseId": BASE, "ticker": "BTC", "entryPrice": 100,
            "createdAt": "2025-01-01T00:00:00Z", "portfolio": {"id": PID}, **fields}


class FakeHistory:
    def __init__(self):
        self.calls = []
        self.record = trade()

    def get_portfolio_investments(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "investmentsTicker": (
            [self.record] if kwargs["page"] == 1 else []
        )}

    def get_investment_history(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "page": kwargs["page"], "items": [
            {"id": "post1", "createdAt": "2025-01-02T00:00:00Z", "update": self.record}
        ] if kwargs["page"] == 1 else []}


def test_append_preserves_revisions_origin_and_first_observation(tmp_path):
    with ResearchArchive(tmp_path / "research.db") as archive:
        kwargs = {"entity_id": BASE, "portfolio_id": PID, "deduplicate": True}
        assert archive.append("investment", trade(), observed_at="2026-01-01", **kwargs)
        assert not archive.append("investment", trade(), observed_at="2026-01-02", **kwargs)
        assert archive.append("investment", trade(stopLoss=95), **kwargs)
        assert archive.append("investment", trade(), origin="backfill", **kwargs)
        rows = archive.conn.execute(
            "SELECT entry_price, observed_at, stop_loss "
            "FROM trade_observations ORDER BY observation_id"
        ).fetchall()
        assert rows[0] == (100, "2026-01-01", None)
        assert rows[1][2] == 95
        assert len(rows) == 3


def test_recursive_redaction_preserves_unknown_trading_fields(tmp_path):
    with ResearchArchive(tmp_path / "r.db") as archive:
        archive.append("investment", trade(accessToken="DO_NOT_KEEP", nested=[{
            "private_key": "DO_NOT_KEEP", "newMetric": 123, "Authorization": "DO_NOT_KEEP",
        }]))
        payload = archive.conn.execute("SELECT payload FROM observations").fetchone()[0]
        assert "DO_NOT_KEEP" not in payload
        assert json.loads(payload)["nested"][0]["newMetric"] == 123


def test_backfill_both_statuses_history_and_safe_rerun(tmp_path):
    client = FakeHistory()
    waits = []
    with ResearchArchive(tmp_path / "r.db") as archive:
        importer = HistoryBackfill(client, archive, sleep=waits.append)
        first = importer.run([PID, PID])
        assert first["status"] == "complete"
        assert first["saved"] == 2
        assert {c["is_open"] for c in client.calls if "is_open" in c} == {False, True}
        # A short page is followed by another request; don't assume server page size.
        assert any(c["page"] == 2 for c in client.calls)
        second = HistoryBackfill(client, archive, sleep=waits.append).run([PID])
        assert second["saved"] == 0
        client.record = trade(closingPrice=110, isOpen=False)
        assert HistoryBackfill(client, archive, sleep=waits.append).run([PID])["saved"] == 2
        rows = archive.conn.execute(
            "SELECT observed_at, source_at, origin FROM observations WHERE kind='investment'"
        ).fetchall()
        assert all(row[0] != row[1] and row[2] == "backfill" for row in rows)
        assert all(w >= 1 for w in waits)


@pytest.mark.parametrize("body", [
    {"success": False, "investmentsTicker": []},
    {"success": True}, {"success": True, "investmentsTicker": [42]},
    {"success": True, "investmentsTicker": [{}]},
])
def test_malformed_pages_never_report_complete(tmp_path, body):
    client = FakeHistory()
    client.get_portfolio_investments = lambda **kw: body
    with ResearchArchive(tmp_path / "r.db") as archive:
        result = HistoryBackfill(client, archive, sleep=lambda _: None).run([PID])
        assert result["status"] == "incomplete"
        assert len(result["failures"]) == 2


@pytest.mark.parametrize("repeat", [True, False])
def test_repeated_pages_and_page_cap_are_incomplete(tmp_path, repeat):
    client = FakeHistory()
    if repeat:
        client.get_portfolio_investments = lambda **kw: {
            "success": True, "investmentsTicker": [trade()]
        }
    with ResearchArchive(tmp_path / "r.db") as archive:
        result = HistoryBackfill(client, archive, max_pages=2 if repeat else 1,
                                 sleep=lambda _: None).run([PID])
        assert result["status"] == "incomplete"


def test_rate_limit_honors_retry_after_and_stops_after_budget(tmp_path):
    calls, waits = [], []
    client = FakeHistory()

    def limited(**kwargs):
        calls.append(kwargs)
        raise InvolioAPIError("limited", status_code=429, retry_after="30")

    client.get_portfolio_investments = limited
    with ResearchArchive(tmp_path / "r.db") as archive:
        with pytest.raises(InvolioAPIError):
            HistoryBackfill(client, archive, sleep=waits.append).run([PID])
        assert len(calls) == 4
        assert waits == [1, 30, 30, 30]
        assert archive.conn.execute(
            "SELECT kind FROM observations ORDER BY id DESC LIMIT 1"
        ).fetchone()[0] == "backfill_interrupted"


def test_interruption_keeps_committed_records_and_rerun_recovers(tmp_path):
    client = FakeHistory()
    original = client.get_portfolio_investments

    def interrupted(**kw):
        if kw["page"] == 2:
            raise KeyboardInterrupt
        return original(**kw)

    client.get_portfolio_investments = interrupted
    with ResearchArchive(tmp_path / "r.db") as archive:
        with pytest.raises(KeyboardInterrupt):
            HistoryBackfill(client, archive, sleep=lambda _: None).run([PID])
        assert archive.conn.execute("SELECT COUNT(*) FROM trade_observations").fetchone()[0] == 1
        client.get_portfolio_investments = original
        result = HistoryBackfill(client, archive, sleep=lambda _: None).run([PID])
        assert result["status"] == "complete"


def test_followed_selection_is_archived_and_does_not_mutate_allowlist(tmp_path):
    client = FakeHistory()
    client.get_followed_portfolios = lambda **kw: {
        "savedPortfolios": [{"portfolio": {"id": PID}}] if kw["page"] == 1 else []
    }
    with ResearchArchive(tmp_path / "r.db") as archive:
        importer = HistoryBackfill(client, archive, sleep=lambda _: None)
        assert importer.followed_portfolios("user") == [PID]
        client.get_followed_portfolios = lambda **kw: {"success": False}
        with pytest.raises(HistoryError):
            importer.followed_portfolios("user")


def test_buffer_detaches_inputs_and_flushes(tmp_path):
    path = tmp_path / "r.db"
    buffer = BufferedResearch(path)
    payload = trade()
    buffer.record("investment", payload)
    payload["entryPrice"] = 999
    buffer.close()
    with ResearchArchive(path) as archive:
        price = archive.conn.execute("SELECT entry_price FROM trade_observations").fetchone()[0]
        assert price == 100


def test_buffer_disk_failure_does_not_break_trading_db(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    buffer = BufferedResearch(blocked / "r.db")
    db = AgentDB(tmp_path / "agent.db")
    db.research = buffer
    db.mark_processed("n1", "IGNORE")
    db.log_action(action="SKIP", summary="cap", notification_id="n1", dry_run=True)
    assert db.processed_action("n1") == "IGNORE"
    db.close()
    buffer.close()
    assert buffer.failed == 2


def test_archive_path_cannot_be_trading_db_or_pruned_logs(tmp_path):
    db, logs = tmp_path / "agent.db", tmp_path / "logs"
    for path in (db, logs / "research.db"):
        with pytest.raises(ValueError):
            validate_archive_path(path, db, logs)


def test_api_contracts_and_raw_capture_without_auth_capture(tmp_path):
    settings = Settings(_env_file=None)
    client = InvolioClient(settings, SessionStore(tmp_path / "absent-session.json"))
    client._auth_headers = lambda **kw: {"Authorization": "Bearer test-only"}
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("get_notifications"):
            return httpx.Response(200, json={"success": True, "items": [{
                "id": str(uuid4()), "createdAt": "2025-01-01T00:00:00Z", "content": "signal",
                "notificationType": "portfolio_added_investment", "unknownFutureField": 5,
            }]})
        return httpx.Response(200, json={"success": True, "investmentsTicker": [], "items": []})

    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    buffer = BufferedResearch(tmp_path / "r.db")
    client.research = buffer
    client.get_portfolio_investments(PID, is_open=False, page=2)
    assert json.loads(requests[-1].content) == {
        "portfolioId": PID, "isOpen": False, "params": {"page": 2, "size": 10},
    }
    client.get_investment_history(BASE, page=3)
    assert json.loads(requests[-1].content) == {
        "investmentBaseId": BASE,
        "params": {"page": 3, "size": 20, "shadowInvestmentsOnly": False},
    }
    client.get_notifications()
    client._request("POST", client._social("/auth/test"))
    client.close()
    buffer.close()
    with ResearchArchive(tmp_path / "r.db") as archive:
        rows = archive.conn.execute("SELECT kind, payload FROM observations").fetchall()
        assert len(rows) == 1 and rows[0][0] == "notification"
        assert json.loads(rows[0][1])["unknownFutureField"] == 5
        assert "Bearer" not in rows[0][1]


def test_status_uses_read_only_archive(tmp_path):
    path = tmp_path / "r.db"
    with ResearchArchive(path) as archive:
        archive.append("investment", trade())
    result = CliRunner().invoke(research_app, ["status", "--database", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["datasets"][0]["records"] == 1
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_cli_requires_selection_without_reading_settings(monkeypatch):
    import invo_agent.research_cli as module
    monkeypatch.setattr(module, "get_settings", lambda: pytest.fail("unexpected settings access"))
    result = CliRunner().invoke(research_app, ["backfill"])
    assert result.exit_code == 2


def test_queue_saturation_is_nonblocking_and_records_gap(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = ResearchArchive.append

    def paused(self, kind, payload, **metadata):
        if kind == "first":
            entered.set()
            assert release.wait(3)
        return original(self, kind, payload, **metadata)

    monkeypatch.setattr(ResearchArchive, "append", paused)
    buffer = BufferedResearch(tmp_path / "r.db", capacity=1)
    try:
        buffer.record("first", {})
        assert entered.wait(3)
        buffer.record("second", {})
        buffer.record("third", {})
        assert buffer.dropped == 1
    finally:
        release.set()
        buffer.close()
    with ResearchArchive(tmp_path / "r.db") as archive:
        gap = archive.conn.execute(
            "SELECT payload FROM observations WHERE kind='capture_gap'"
        ).fetchone()
        assert json.loads(gap[0])["dropped_total"] == 1


def test_engine_capture_links_signal_and_risk_settings(tmp_path):
    from invo_agent.api.models import Notification, PostResponse
    from invo_agent.engine import CopyEngine

    settings = Settings(_env_file=None, max_position_pct=7)
    notification = Notification.model_validate({
        "id": str(uuid4()), "createdAt": "2025-01-01T00:00:00Z", "content": "signal",
        "notificationType": "portfolio_added_investment", "postId": str(uuid4()),
        "portfolioId": PID,
    })
    client = SimpleNamespace(get_post_by_id=lambda _: PostResponse.model_validate({
        "success": True, "post": {"update": trade(id=str(uuid4()))},
    }))
    buffer = BufferedResearch(tmp_path / "r.db")
    db = AgentDB(tmp_path / "agent.db")
    db.research = buffer
    action = CopyEngine(client, db, settings).enrich(notification)
    assert action.investment.entry_price == 100
    db.close()
    buffer.close()
    with ResearchArchive(tmp_path / "r.db") as archive:
        entity, payload = archive.conn.execute(
            "SELECT entity_id, payload FROM observations WHERE kind='decision_inputs'"
        ).fetchone()
        assert entity == str(notification.id)
        assert json.loads(payload)["risk"]["max_position_pct"] == 7
        assert json.loads(payload)["risk"]["dry_run"] is True


def test_cli_backfill_failure_does_not_print_account_data(tmp_path, monkeypatch):
    import invo_agent.research_cli as module

    settings = Settings(_env_file=None, research_db_path=tmp_path / "r.db")
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "SessionStore", lambda _: SimpleNamespace(
        load=lambda: SimpleNamespace(user_id="user", access_token="DO_NOT_PRINT")
    ))

    class FailingClient:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_portfolio_investments(self, **kwargs):
            raise InvolioAPIError("DO_NOT_PRINT", status_code=403,
                                 body={"accessToken": "DO_NOT_PRINT"})

    monkeypatch.setattr(module, "InvolioClient", FailingClient)
    monkeypatch.setattr("invo_agent.research_backfill.time.sleep", lambda _: None)
    result = CliRunner().invoke(research_app, ["backfill", "--portfolio", PID])
    assert result.exit_code == 1
    assert "HTTP 403" in result.output
    assert "DO_NOT_PRINT" not in result.output
    status = CliRunner().invoke(research_app, ["status", "--database", str(tmp_path / "r.db")])
    assert json.loads(status.output)["latest_backfill"]["kind"] == "backfill_interrupted"


def test_instrument_fields_survive_redaction_and_removals_are_recorded(tmp_path):
    with ResearchArchive(tmp_path / "r.db") as archive:
        archive.append("investment", trade(
            tokenAddress="0xInstrument", tokenSymbol="WBTC", sessionHigh=101.5,
            seedRound="series-a", accessToken="DO_NOT_KEEP",
            nested=[{"x-api-key": "DO_NOT_KEEP", "walletPrivateKey": "DO_NOT_KEEP"}],
        ))
        payload, redacted = archive.conn.execute(
            "SELECT payload, redacted_keys FROM observations"
        ).fetchone()
        stored = json.loads(payload)
        # Market/instrument names that merely contain a credential word must stay.
        assert stored["tokenAddress"] == "0xInstrument"
        assert stored["tokenSymbol"] == "WBTC"
        assert stored["sessionHigh"] == 101.5
        assert stored["seedRound"] == "series-a"
        assert "DO_NOT_KEEP" not in payload
        # Provenance: what we deleted is distinguishable from what was absent.
        assert json.loads(redacted) == [
            "accessToken", "nested[].walletPrivateKey", "nested[].x-api-key",
        ]


def test_unchanged_repeat_advances_cadence_instead_of_disappearing(tmp_path):
    with ResearchArchive(tmp_path / "r.db") as archive:
        kwargs = {"entity_id": BASE, "portfolio_id": PID, "deduplicate": True}
        assert archive.append("investment", trade(), observed_at="2026-01-01", **kwargs)
        assert not archive.append("investment", trade(), observed_at="2026-01-02", **kwargs)
        assert not archive.append("investment", trade(), observed_at="2026-01-03", **kwargs)
        rows = archive.conn.execute(
            "SELECT observed_at, last_seen_at, seen_count FROM trade_observations"
        ).fetchall()
        # One row, but it still evidences that the value held through 01-03.
        assert rows == [("2026-01-01", "2026-01-03", 3)]


def test_live_post_capture_keys_on_investment_base_id(tmp_path):
    settings = Settings(_env_file=None)
    client = InvolioClient(settings, SessionStore(tmp_path / "absent-session.json"))
    client._auth_headers = lambda **kw: {}
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "success": True, "post": {"id": str(uuid4()), "update": trade(id=str(uuid4()))},
        })
    ))
    buffer = BufferedResearch(tmp_path / "r.db")
    client.research = buffer
    client.get_post_by_id(uuid4())
    client.get_current_shadow_post_by_investment_base_id(BASE)
    client.close()
    buffer.close()
    with ResearchArchive(tmp_path / "r.db") as archive:
        keys = dict(archive.conn.execute(
            "SELECT kind, entity_id FROM observations WHERE kind IN ('post', 'latest_post')"
        ).fetchall())
        # Both live post kinds join to the backfill's investment/history rows.
        assert keys == {"post": BASE, "latest_post": BASE}


def test_investment_without_base_id_is_never_reported_complete(tmp_path):
    client = FakeHistory()
    client.record = {"id": "trade1", "ticker": "BTC", "entryPrice": 100}
    with ResearchArchive(tmp_path / "r.db") as archive:
        for revisions in (True, False):
            result = HistoryBackfill(client, archive, revisions=revisions,
                                     sleep=lambda _: None).run([PID])
            assert result["status"] == "incomplete"
            assert {f["error_type"] for f in result["failures"]} == {"missing_base_id"}
        # The view says the fallback happened rather than implying a real base id.
        assert archive.conn.execute(
            "SELECT base_id, source_base_id FROM trade_observations"
        ).fetchone() == ("trade1", None)


def test_shutdown_persists_the_closing_loss_tally(tmp_path):
    buffer = BufferedResearch(tmp_path / "r.db")
    buffer.dropped = 3  # records lost with nothing following them to trigger a gap
    buffer.close()
    with ResearchArchive(tmp_path / "r.db") as archive:
        gaps = [json.loads(row[0]) for row in archive.conn.execute(
            "SELECT payload FROM observations WHERE kind='capture_gap'"
        ).fetchall()]
    assert [g for g in gaps if g.get("final")] == [
        {"dropped_total": 3, "failed_total": 0, "final": True}
    ]


def test_version_1_archive_migrates_without_losing_rows(tmp_path):
    path = tmp_path / "r.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL, kind TEXT NOT NULL,
            origin TEXT NOT NULL, entity_id TEXT, portfolio_id TEXT,
            observed_at TEXT NOT NULL, source_at TEXT, run_id TEXT,
            dedupe_key TEXT UNIQUE, payload TEXT NOT NULL
        );
        CREATE VIEW trade_observations AS SELECT id AS observation_id FROM observations;
    """)
    old.execute(
        "INSERT INTO observations (schema_version, kind, origin, observed_at, payload)"
        " VALUES (1, 'investment', 'backfill', '2026-01-01', ?)", (json.dumps(trade()),)
    )
    old.commit()
    old.close()

    with ResearchArchive(path) as archive:
        assert archive.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        row = archive.conn.execute(
            "SELECT observations.schema_version, observations.seen_count,"
            " observations.last_seen_at, entry_price, owner_username"
            " FROM observations JOIN trade_observations"
            " ON id = observation_id"
        ).fetchone()
        # Pre-existing row is intact, backfilled to seen_count 1, and readable
        # through the rebuilt view including columns it predates.
        assert row == (1, 1, None, 100, None)
        assert archive.append("investment", trade(owner={"username": "synthetic_trader"}))
        assert archive.conn.execute(
            "SELECT owner_username FROM trade_observations WHERE observation_id = 2"
        ).fetchone()[0] == "synthetic_trader"
