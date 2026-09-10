"""An untracked HL position must stay visible without drowning the log.

A position HL holds with no ``open_copy`` sits there until a human closes it,
so every poll rediscovers it. The old code logged and notified on each pass,
which turned the one sighting that mattered into thousands of identical lines
and buried it.

Reporting is now edge-triggered plus a slow heartbeat: once on the first
sighting, then once per ``UNTRACKED_REPORT_INTERVAL_SECONDS``. What must not
change is the sighting itself — throttling the report may never suppress the
guard, reset the recorded age, or let a genuinely new stranger wait.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from invo_agent.config import Settings
from invo_agent.db import (
    GHOST_SEEN_META_PREFIX,
    UNTRACKED_REPORTED_META_PREFIX,
    AgentDB,
)
from invo_agent.poller import UNTRACKED_REPORT_INTERVAL_SECONDS, NotificationPoller


class _FakePos:
    def __init__(self, *, sz: float, is_long: bool) -> None:
        self.sz = sz
        self.is_long = is_long
        self.entry_px = 100.0
        self.leverage = 10


class _FakeBook:
    def __init__(self, positions: dict[str, _FakePos]) -> None:
        self.positions = positions

    def position(self, coin: str) -> _FakePos | None:
        return self.positions.get(coin.upper())


class _FakeExecutor:
    def __init__(self, book: _FakeBook, *, untracked: list[str]) -> None:
        self._trader = SimpleNamespace(_book=book)
        self._untracked = untracked
        self.adopted: list[dict] = []

    def untracked_hl_coins(self) -> list[str]:
        return list(self._untracked)

    def adopt_open_fill(self, **kwargs) -> None:
        self.adopted.append(kwargs)


class _FakeClient:
    def __init__(self) -> None:
        self.session = None

    def get_current_shadow_post_by_investment_base_id(self, base_id: str):
        return SimpleNamespace(post=None)


def _poller(tmp_path, *, untracked=("AAA",)):
    settings = Settings(
        db_path=tmp_path / "agent.db",
        session_path=tmp_path / "session.json",
        live=False,
        dry_run=True,
        mac_notify=False,
    )
    db = AgentDB(settings.db_path)
    poller = NotificationPoller(
        client=_FakeClient(),
        db=db,
        settings=settings,
        session_store=SimpleNamespace(),
    )  # type: ignore[arg-type]
    book = _FakeBook({"AAA": _FakePos(sz=1.0, is_long=True)})
    poller.executor = _FakeExecutor(book, untracked=list(untracked))  # type: ignore[assignment]
    return poller, db


def _events(monkeypatch) -> list[dict]:
    """Capture ``log_event`` calls the way the poller emits them."""
    seen: list[dict] = []

    def _capture(kind: str, **fields) -> None:
        seen.append({"kind": kind, **fields})

    monkeypatch.setattr("invo_agent.poller.log_event", _capture)
    return seen


def _inverse_ghost_events(seen: list[dict]) -> list[dict]:
    return [e for e in seen if e["kind"] == "inverse_ghost"]


def _age_the_report(db: AgentDB, coin: str, seconds: float) -> None:
    """Backdate the throttle stamp — the honest way to advance the interval."""
    key = f"{UNTRACKED_REPORTED_META_PREFIX}{coin.upper()}"
    stamp = datetime.fromisoformat(str(db.get_meta(key)))
    db.set_meta(key, (stamp - timedelta(seconds=seconds)).isoformat())


# --- the first sighting always speaks --------------------------------------


def test_first_sighting_reports(tmp_path, monkeypatch) -> None:
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)

    poller._reconcile_inverse_ghosts()

    assert len(_inverse_ghost_events(seen)) == 1
    db.close()


def test_first_sighting_still_records_the_ghost(tmp_path, monkeypatch) -> None:
    """Throttling is about the report, never about the guard's bookkeeping."""
    _events(monkeypatch)
    poller, db = _poller(tmp_path)

    poller._reconcile_inverse_ghosts()

    assert db.get_meta(f"{GHOST_SEEN_META_PREFIX}AAA") is not None
    assert poller._known_ghosts() == {"AAA"}
    db.close()


# --- repeats inside the window stay quiet ----------------------------------


def test_repeat_polls_do_not_report(tmp_path, monkeypatch) -> None:
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)

    for _ in range(50):
        poller._reconcile_inverse_ghosts()

    assert len(_inverse_ghost_events(seen)) == 1
    db.close()


def test_the_guard_still_runs_on_every_quiet_poll(tmp_path, monkeypatch) -> None:
    """Silence must not mean the position stopped being watched."""
    _events(monkeypatch)
    poller, db = _poller(tmp_path)

    for _ in range(50):
        poller._reconcile_inverse_ghosts()

    assert poller._known_ghosts() == {"AAA"}  # still flagged
    assert poller.executor.adopted == []  # and still never adopted
    assert db.list_open_copies() == []
    db.close()


# --- the heartbeat comes back ----------------------------------------------


def test_report_returns_after_the_interval(tmp_path, monkeypatch) -> None:
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)

    poller._reconcile_inverse_ghosts()
    poller._reconcile_inverse_ghosts()
    assert len(_inverse_ghost_events(seen)) == 1

    _age_the_report(db, "AAA", UNTRACKED_REPORT_INTERVAL_SECONDS + 1)
    poller._reconcile_inverse_ghosts()

    assert len(_inverse_ghost_events(seen)) == 2
    db.close()


def test_report_does_not_return_one_second_early(tmp_path, monkeypatch) -> None:
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)

    poller._reconcile_inverse_ghosts()
    _age_the_report(db, "AAA", UNTRACKED_REPORT_INTERVAL_SECONDS - 1)
    poller._reconcile_inverse_ghosts()

    assert len(_inverse_ghost_events(seen)) == 1
    db.close()


# --- the age we report is the real one -------------------------------------


def test_reported_age_grows_from_first_sighting(tmp_path, monkeypatch) -> None:
    """The throttle stamp moves; the first-seen stamp must not."""
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)

    poller._reconcile_inverse_ghosts()
    first = _inverse_ghost_events(seen)[0]["untracked_for_seconds"]

    # Backdate the sighting an hour, then let the heartbeat fire again.
    seen_key = f"{GHOST_SEEN_META_PREFIX}AAA"
    aged = datetime.fromisoformat(str(db.get_meta(seen_key))) - timedelta(hours=1)
    db.set_meta(seen_key, aged.isoformat())
    _age_the_report(db, "AAA", UNTRACKED_REPORT_INTERVAL_SECONDS + 1)
    poller._reconcile_inverse_ghosts()

    second = _inverse_ghost_events(seen)[1]["untracked_for_seconds"]
    assert first < 60  # just spotted
    assert second >= 3600  # and an hour later it says so
    db.close()


# --- a new stranger never waits out someone else's window ------------------


def test_a_coin_that_returns_reports_immediately(tmp_path, monkeypatch) -> None:
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)

    poller._reconcile_inverse_ghosts()
    assert len(_inverse_ghost_events(seen)) == 1

    poller.executor._untracked = []  # closed by hand
    poller._reconcile_inverse_ghosts()
    assert poller._known_ghosts() == set()

    poller.executor._untracked = ["AAA"]  # a different one turns up
    poller._reconcile_inverse_ghosts()

    assert len(_inverse_ghost_events(seen)) == 2
    db.close()


def test_leaving_the_book_clears_the_throttle_stamp(tmp_path, monkeypatch) -> None:
    _events(monkeypatch)
    poller, db = _poller(tmp_path)
    poller._reconcile_inverse_ghosts()
    assert db.get_meta(f"{UNTRACKED_REPORTED_META_PREFIX}AAA") is not None

    poller.executor._untracked = []
    poller._reconcile_inverse_ghosts()

    assert db.get_meta(f"{UNTRACKED_REPORTED_META_PREFIX}AAA") is None
    db.close()


def test_each_coin_is_throttled_on_its_own(tmp_path, monkeypatch) -> None:
    """One noisy stranger must not silence the next one."""
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)
    poller.executor._trader._book.positions["BBB"] = _FakePos(sz=2.0, is_long=False)

    poller._reconcile_inverse_ghosts()  # AAA reports
    poller.executor._untracked = ["AAA", "BBB"]
    poller._reconcile_inverse_ghosts()  # AAA quiet, BBB reports

    coins = [e["coin"] for e in _inverse_ghost_events(seen)]
    assert coins == ["AAA", "BBB"]
    db.close()


# --- a throttled report is still never a flatten ---------------------------


def test_a_quiet_untracked_position_is_never_flattened(tmp_path, monkeypatch) -> None:
    _events(monkeypatch)
    poller, db = _poller(tmp_path)

    for _ in range(20):
        poller._reconcile_inverse_ghosts()

    assert poller.executor.adopted == []
    assert db.list_open_copies() == []
    db.close()


def test_a_bad_throttle_stamp_does_not_silence_the_report(tmp_path, monkeypatch) -> None:
    """An unparseable stamp must fail towards reporting, never towards silence."""
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)
    db.set_meta(f"{UNTRACKED_REPORTED_META_PREFIX}AAA", "not-a-timestamp")

    poller._reconcile_inverse_ghosts()

    assert len(_inverse_ghost_events(seen)) == 1
    db.close()


def test_unique_ids_are_not_needed_to_flag_a_stranger(tmp_path, monkeypatch) -> None:
    """The guard keys on the coin, so an unknown base id changes nothing."""
    _events(monkeypatch)
    poller, db = _poller(tmp_path)
    db.set_meta("unrelated", str(uuid4()))

    poller._reconcile_inverse_ghosts()

    assert poller._known_ghosts() == {"AAA"}
    db.close()


def test_a_naive_first_seen_stamp_does_not_crash_the_pass(tmp_path, monkeypatch) -> None:
    """Stamps predating the tz-aware writes must not take the reconcile down."""
    seen = _events(monkeypatch)
    poller, db = _poller(tmp_path)
    naive = datetime.now().replace(microsecond=0) - timedelta(hours=2)  # noqa: DTZ005
    db.set_meta(f"{GHOST_SEEN_META_PREFIX}AAA", naive.isoformat())

    # A naive stamp carries no offset, so its true age is unknowable — the only
    # thing that matters is that the pass still runs and still reports.
    poller._reconcile_inverse_ghosts()

    assert len(_inverse_ghost_events(seen)) == 1
    assert poller._known_ghosts() == {"AAA"}
    assert poller.executor.adopted == []
    db.close()
