import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from invo_agent.allowlist import (
    PortfolioAllowlist,
    extract_portfolio_ids,
    extract_portfolios,
)
from invo_agent.api.models import Notification
from invo_agent.config import Settings
from invo_agent.db import AgentDB, FollowedPortfolio
from invo_agent.engine import CopyEngine

ENV_A = "00000000-0000-4000-8000-000000000007"
ENV_B = "00000000-0000-4000-8000-000000000003"
FOLLOWED_A = "00000000-0000-4000-8000-000000000009"
FOLLOWED_B = "00000000-0000-4000-8000-000000000011"


class FakeClient:
    """Stands in for InvolioClient: fixed pages, or an exception."""

    def __init__(self, pages: list[dict] | None = None, error: Exception | None = None):
        self.pages = pages or []
        self.error = error
        self.calls: list[tuple[str, int, int]] = []

    def get_followed_portfolios(self, user_id, *, page: int = 1, size: int = 50) -> dict:
        self.calls.append((str(user_id), page, size))
        if self.error is not None:
            raise self.error
        if page > len(self.pages):
            return {"items": []}
        return self.pages[page - 1]


def _settings(tmp_path, **overrides) -> Settings:
    base = {
        "db_path": tmp_path / "agent.db",
        "portfolio_allowlist": f"{ENV_A},{ENV_B}",
    }
    base.update(overrides)
    return Settings(**base)


def _allowlist(settings, client=None, db=None, now=None) -> PortfolioAllowlist:
    return PortfolioAllowlist(
        settings, client, db=db, now=now, user_id=lambda: "11111111-1111-1111-1111-111111111111"
    )


def _notification(portfolio_id: str | None) -> Notification:
    payload = {
        "id": str(uuid4()),
        "content": "x",
        "notificationType": "user_added_investment",
        "createdAt": datetime.now(UTC).isoformat(),
    }
    if portfolio_id:
        payload["portfolioId"] = portfolio_id
    return Notification.model_validate(payload)


# --- payload parsing ---


def test_extract_reads_common_shapes() -> None:
    assert extract_portfolio_ids({"items": [{"id": FOLLOWED_A}]}) == {FOLLOWED_A}
    assert extract_portfolio_ids({"portfolios": [{"portfolioId": FOLLOWED_A}]}) == {FOLLOWED_A}
    assert extract_portfolio_ids({"data": {"items": [{"id": FOLLOWED_A}]}}) == {FOLLOWED_A}
    assert extract_portfolio_ids([{"id": FOLLOWED_A}, {"id": FOLLOWED_B}]) == {
        FOLLOWED_A,
        FOLLOWED_B,
    }
    assert extract_portfolio_ids({"items": [{"portfolio": {"id": FOLLOWED_A}}]}) == {FOLLOWED_A}


def test_extract_reads_involio_saved_portfolios_shape() -> None:
    """The real response: savedPortfolios → [{portfolio: {id}}], owner id alongside.

    Every other shape here is defensive guesswork; this is the one the live
    endpoint returns, so a rename that drops it silently empties the allowlist.
    """
    owner = str(uuid4())
    payload = {
        "savedPortfolios": [
            {
                "portfolio": {"id": FOLLOWED_A, "ownerId": owner, "isFollowing": True},
                "createdAt": "2026-01-02T12:00:00.000Z",
            },
            {
                "portfolio": {"id": FOLLOWED_B, "ownerId": owner, "isFollowing": True},
                "createdAt": "2026-01-01T12:00:00.000Z",
            },
        ]
    }
    assert extract_portfolio_ids(payload) == {FOLLOWED_A, FOLLOWED_B}


def test_extract_ignores_owner_ids() -> None:
    owner = str(uuid4())
    payload = {
        "items": [
            {"id": FOLLOWED_A, "user": {"id": owner}, "userId": owner},
        ]
    }
    assert extract_portfolio_ids(payload) == {FOLLOWED_A}


def test_extract_survives_junk() -> None:
    assert extract_portfolio_ids({"success": True}) == set()
    assert extract_portfolio_ids({"items": [{"id": "not-a-uuid"}, None, 7]}) == set()
    assert extract_portfolio_ids(None) == set()


def test_extract_reads_title_and_manager() -> None:
    payload = {
        "items": [
            {
                "id": FOLLOWED_A,
                "title": "Majors swing",
                "user": {"username": "epsilon_trader", "name": "Epsilon", "id": str(uuid4())},
            }
        ]
    }
    (record,) = extract_portfolios(payload)
    assert record.portfolio_id == FOLLOWED_A
    assert record.title == "Majors swing"
    assert record.manager == "epsilon_trader"
    assert record.manager_name == "Epsilon"
    assert record.label == "@epsilon_trader · Majors swing"


def test_extract_reads_manager_off_the_follow_row() -> None:
    """Some shapes wrap the portfolio and hang the owner off the wrapper."""
    payload = {
        "items": [
            {
                "portfolio": {"id": FOLLOWED_A, "name": "Alts"},
                "owner": {"username": "betatrader"},
            }
        ]
    }
    (record,) = extract_portfolios(payload)
    assert record.portfolio_id == FOLLOWED_A
    assert record.title == "Alts"
    assert record.manager == "betatrader"


def test_manager_id_is_not_an_allowlist_entry() -> None:
    owner = str(uuid4())
    payload = {"items": [{"id": FOLLOWED_A, "user": {"id": owner, "username": "x"}}]}
    (record,) = extract_portfolios(payload)
    assert record.manager_id == owner
    assert extract_portfolio_ids(payload) == {FOLLOWED_A}


def test_label_falls_back_to_id(tmp_path) -> None:
    allowlist = _allowlist(_settings(tmp_path), FakeClient([{"items": [{"id": FOLLOWED_A}]}]))
    assert allowlist.label(FOLLOWED_A) == FOLLOWED_A
    allowlist.refresh()
    assert allowlist.label(FOLLOWED_A) == FOLLOWED_A
    assert allowlist.label(ENV_A) == ENV_A
    assert allowlist.label(None) == "?"


# --- refresh behaviour ---


def test_refresh_replaces_env_list(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = FakeClient([{"items": [{"id": FOLLOWED_A}, {"id": FOLLOWED_B}]}])
    allowlist = _allowlist(settings, client)

    assert allowlist.ids == {ENV_A, ENV_B}
    assert allowlist.source == "env"
    assert allowlist.refresh() is True
    assert allowlist.ids == {FOLLOWED_A, FOLLOWED_B}
    assert allowlist.source == "followed"


def test_refresh_pages_until_short_page(tmp_path) -> None:
    first = {"items": [{"id": str(uuid4())} for _ in range(50)]}
    second = {"items": [{"id": FOLLOWED_A}]}
    client = FakeClient([first, second])
    allowlist = _allowlist(_settings(tmp_path), client)

    allowlist.refresh()
    assert [call[1] for call in client.calls] == [1, 2]
    assert FOLLOWED_A in allowlist.ids
    assert len(allowlist.ids) == 51


def test_api_failure_keeps_env_list(tmp_path) -> None:
    allowlist = _allowlist(_settings(tmp_path), FakeClient(error=RuntimeError("503")))
    assert allowlist.refresh() is False
    assert allowlist.ids == {ENV_A, ENV_B}
    assert allowlist.source == "env"


def test_empty_response_never_widens_to_all(tmp_path) -> None:
    """An empty allowlist means 'copy everyone' — a bad sync must not cause it."""
    allowlist = _allowlist(_settings(tmp_path), FakeClient([{"items": []}]))
    assert allowlist.refresh() is False
    assert allowlist.ids == {ENV_A, ENV_B}


def test_empty_response_keeps_previous_sync(tmp_path) -> None:
    client = FakeClient([{"items": [{"id": FOLLOWED_A}]}])
    allowlist = _allowlist(_settings(tmp_path), client)
    allowlist.refresh()

    client.pages = [{"items": []}]
    assert allowlist.refresh() is False
    assert allowlist.ids == {FOLLOWED_A}


def test_refresh_reports_no_change_when_same(tmp_path) -> None:
    client = FakeClient([{"items": [{"id": FOLLOWED_A}]}])
    allowlist = _allowlist(_settings(tmp_path), client)
    assert allowlist.refresh() is True
    assert allowlist.refresh() is False


def test_missing_user_id_keeps_env_list(tmp_path) -> None:
    client = FakeClient([{"items": [{"id": FOLLOWED_A}]}])
    allowlist = PortfolioAllowlist(_settings(tmp_path), client, user_id=lambda: None)
    assert allowlist.refresh() is False
    assert allowlist.ids == {ENV_A, ENV_B}
    assert client.calls == []


# --- scheduling ---


def test_maybe_refresh_respects_interval(tmp_path) -> None:
    clock = [1000.0]
    settings = _settings(tmp_path, portfolio_sync_interval_seconds=900)
    client = FakeClient([{"items": [{"id": FOLLOWED_A}]}])
    allowlist = _allowlist(settings, client, now=lambda: clock[0])

    assert allowlist.maybe_refresh() is True
    clock[0] += 899
    allowlist.maybe_refresh()
    assert len(client.calls) == 1

    clock[0] += 2
    client.pages = [{"items": [{"id": FOLLOWED_A}, {"id": FOLLOWED_B}]}]
    assert allowlist.maybe_refresh() is True
    assert allowlist.ids == {FOLLOWED_A, FOLLOWED_B}


def test_sync_off_keeps_env_list(tmp_path) -> None:
    settings = _settings(tmp_path, portfolio_sync=False)
    client = FakeClient([{"items": [{"id": FOLLOWED_A}]}])
    allowlist = _allowlist(settings, client)

    assert allowlist.maybe_refresh() is False
    assert client.calls == []
    assert allowlist.ids == {ENV_A, ENV_B}


# --- persistence ---


def test_last_good_sync_survives_restart(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    client = FakeClient(
        [{"items": [{"id": FOLLOWED_A, "title": "Majors", "user": {"username": "epsilon_short"}}]}]
    )
    _allowlist(settings, client, db=db).refresh()

    # New process, API now down: hold the last known follows, not the env list.
    restarted = _allowlist(settings, FakeClient(error=RuntimeError("boom")), db=db)
    assert restarted.ids == {FOLLOWED_A}
    assert restarted.source == "followed"
    assert restarted.refresh() is False
    assert restarted.ids == {FOLLOWED_A}
    assert restarted.label(FOLLOWED_A) == "@epsilon_short · Majors"
    db.close()


def test_unfollowed_portfolios_leave_the_cache(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    client = FakeClient([{"items": [{"id": FOLLOWED_A}, {"id": FOLLOWED_B}]}])
    allowlist = _allowlist(settings, client, db=db)
    allowlist.refresh()

    client.pages = [{"items": [{"id": FOLLOWED_A}]}]
    assert allowlist.refresh() is True
    assert {p.portfolio_id for p in db.list_followed_portfolios()} == {FOLLOWED_A}
    db.close()


def test_rename_counts_as_a_change(tmp_path) -> None:
    client = FakeClient([{"items": [{"id": FOLLOWED_A, "title": "Majors"}]}])
    allowlist = _allowlist(_settings(tmp_path), client)
    assert allowlist.refresh() is True

    client.pages = [{"items": [{"id": FOLLOWED_A, "title": "Majors v2"}]}]
    assert allowlist.refresh() is True
    assert allowlist.label(FOLLOWED_A) == "Majors v2"


def test_cache_ignored_when_sync_disabled(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    _allowlist(settings, FakeClient([{"items": [{"id": FOLLOWED_A}]}]), db=db).refresh()

    off = _allowlist(_settings(tmp_path, portfolio_sync=False), db=db)
    assert off.ids == {ENV_A, ENV_B}
    db.close()


# --- engine wiring ---


def test_engine_follows_synced_list(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    client = FakeClient([{"items": [{"id": FOLLOWED_A}]}])
    allowlist = _allowlist(settings, client, db=db)
    engine = CopyEngine(  # type: ignore[arg-type]
        client=None, db=db, settings=settings, allowlist=allowlist
    )

    assert engine.should_consider(_notification(ENV_A)) is True
    assert engine.should_consider(_notification(FOLLOWED_A)) is False

    allowlist.refresh()
    assert engine.should_consider(_notification(FOLLOWED_A)) is True
    assert engine.should_consider(_notification(ENV_A)) is False
    assert engine.should_consider(_notification(None)) is False
    db.close()


def test_engine_without_allowlist_uses_settings(tmp_path) -> None:
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    engine = CopyEngine(client=None, db=db, settings=settings)  # type: ignore[arg-type]
    assert engine.should_consider(_notification(ENV_A)) is True
    assert engine.should_consider(_notification(FOLLOWED_A)) is False
    db.close()


# --- a missing allowlist must pause copying, never widen it ---


def test_failed_sync_pauses_copying_instead_of_copying_everyone(tmp_path) -> None:
    """No list must never mean "copy every trader on Involio".

    With PORTFOLIO_ALLOWLIST empty, ``ids`` is empty, and empty is the flag
    should_consider reads as "copy all". A sync we attempted and could not
    complete lands in exactly that state, so it has to stop copying instead.
    """
    settings = _settings(tmp_path, portfolio_allowlist="")
    db = AgentDB(settings.db_path)
    allowlist = _allowlist(settings, FakeClient(error=RuntimeError("503")), db=db)
    engine = CopyEngine(  # type: ignore[arg-type]
        client=None, db=db, settings=settings, allowlist=allowlist
    )

    # Never attempted: the old documented "no allowlist = copy all" still holds.
    assert allowlist.usable is True
    assert engine.should_consider(_notification(str(uuid4()))) is True

    allowlist.maybe_refresh()  # what run_forever does before catch-up
    assert allowlist.ids == set()
    assert allowlist.usable is False
    assert engine.should_consider(_notification(str(uuid4()))) is False
    db.close()


def test_copying_resumes_once_a_sync_lands(tmp_path) -> None:
    settings = _settings(tmp_path, portfolio_allowlist="")
    db = AgentDB(settings.db_path)
    client = FakeClient(error=RuntimeError("503"))
    allowlist = _allowlist(settings, client, db=db)
    engine = CopyEngine(  # type: ignore[arg-type]
        client=None, db=db, settings=settings, allowlist=allowlist
    )
    allowlist.refresh()
    assert engine.should_consider(_notification(FOLLOWED_A)) is False

    client.error = None
    client.pages = [{"items": [{"id": FOLLOWED_A}]}]
    assert allowlist.refresh() is True
    assert allowlist.usable is True
    assert engine.should_consider(_notification(FOLLOWED_A)) is True
    assert engine.should_consider(_notification(str(uuid4()))) is False
    db.close()


def test_failed_sync_keeps_copying_the_env_list(tmp_path) -> None:
    """A fallback list is still a list — only *no* list pauses copying."""
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    allowlist = _allowlist(settings, FakeClient(error=RuntimeError("503")), db=db)
    engine = CopyEngine(  # type: ignore[arg-type]
        client=None, db=db, settings=settings, allowlist=allowlist
    )
    allowlist.refresh()
    assert allowlist.usable is True
    assert engine.should_consider(_notification(ENV_A)) is True
    assert engine.should_consider(_notification(FOLLOWED_A)) is False
    db.close()


def test_sync_off_with_no_env_list_still_copies_all(tmp_path) -> None:
    """PORTFOLIO_SYNC=false + empty .env is a deliberate "copy all", not a failure."""
    settings = _settings(tmp_path, portfolio_allowlist="", portfolio_sync=False)
    db = AgentDB(settings.db_path)
    allowlist = _allowlist(settings, FakeClient(error=RuntimeError("503")), db=db)
    engine = CopyEngine(  # type: ignore[arg-type]
        client=None, db=db, settings=settings, allowlist=allowlist
    )
    allowlist.maybe_refresh()
    assert allowlist.usable is True
    assert engine.should_consider(_notification(str(uuid4()))) is True
    db.close()


# --- widening a hand-picked .env list is loud ---


def test_sync_widening_the_env_list_is_logged(tmp_path, caplog) -> None:
    settings = _settings(tmp_path)  # two pinned ids
    client = FakeClient(
        [{"items": [{"id": ENV_A}, {"id": FOLLOWED_A}, {"id": FOLLOWED_B}]}]
    )
    allowlist = _allowlist(settings, client)
    with caplog.at_level(logging.WARNING, logger="invo_agent.allowlist"):
        assert allowlist.refresh() is True
    assert "widened" in caplog.text
    assert "PORTFOLIO_SYNC=false" in caplog.text


def test_sync_inside_the_env_list_is_not_flagged(tmp_path, caplog) -> None:
    allowlist = _allowlist(_settings(tmp_path), FakeClient([{"items": [{"id": ENV_A}]}]))
    with caplog.at_level(logging.WARNING, logger="invo_agent.allowlist"):
        allowlist.refresh()
    assert "widened" not in caplog.text


# --- failure reporting ---


def test_refresh_records_why_it_failed(tmp_path) -> None:
    """A cached list already reads as "followed", so source cannot signal failure."""
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    _allowlist(settings, FakeClient([{"items": [{"id": FOLLOWED_A}]}]), db=db).refresh()

    down = _allowlist(settings, FakeClient(error=RuntimeError("503 upstream")), db=db)
    assert down.source == "followed"
    assert down.refresh() is False
    assert down.last_error == "503 upstream"

    empty = _allowlist(settings, FakeClient([{"items": []}]), db=db)
    empty.refresh()
    assert empty.last_error == "API returned no portfolios"

    ok = _allowlist(settings, FakeClient([{"items": [{"id": FOLLOWED_B}]}]), db=db)
    ok.refresh()
    assert ok.last_error is None
    db.close()


def test_failed_cache_write_keeps_the_previous_follows(tmp_path) -> None:
    """A half-done write must not empty the cache — that falls back to .env."""
    settings = _settings(tmp_path)
    db = AgentDB(settings.db_path)
    db.replace_followed_portfolios([FollowedPortfolio(portfolio_id=FOLLOWED_A)])

    with pytest.raises(Exception):
        db.replace_followed_portfolios(
            [FollowedPortfolio(portfolio_id=FOLLOWED_B, title={"unbindable": 1})]  # type: ignore[arg-type]
        )
    assert [p.portfolio_id for p in db.list_followed_portfolios()] == [FOLLOWED_A]

    db.set_meta("unrelated", "x")  # a later commit must not flush the DELETE
    assert [p.portfolio_id for p in db.list_followed_portfolios()] == [FOLLOWED_A]
    db.close()


# --- manager parsing ---


def test_manager_id_survives_the_wrapper_fallback() -> None:
    """The live savedPortfolios shape hangs ownerId on the nested portfolio."""
    owner = str(uuid4())
    payload = {
        "savedPortfolios": [
            {
                "portfolio": {"id": FOLLOWED_A, "ownerId": owner, "isFollowing": True},
                "createdAt": "2026-01-02T12:00:00.000Z",
            }
        ]
    }
    (record,) = extract_portfolios(payload)
    assert record.portfolio_id == FOLLOWED_A
    assert record.manager_id == owner


def test_wrapper_fills_gaps_without_clobbering_the_portfolio() -> None:
    owner = str(uuid4())
    payload = {
        "items": [
            {
                "portfolio": {"id": FOLLOWED_A, "name": "Alts", "ownerId": owner},
                "owner": {"username": "betatrader"},
            }
        ]
    }
    (record,) = extract_portfolios(payload)
    assert record.manager == "betatrader"
    assert record.manager_id == owner
    assert record.title == "Alts"
