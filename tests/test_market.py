import json
import sqlite3

import pytest

from invo_agent.market import MarketStore, interval_ms, validate_market_path
from invo_agent.market_fetch import (
    MAINNET_INFO_URL,
    HyperliquidCandles,
    MarketBackfill,
    MarketFetchError,
    coins_from_archive,
    tradable_coins,
)
from invo_agent.market_join import coverage_gaps, market_context, signal_rows, to_ms
from invo_agent.research import ResearchArchive

MINUTE = 60_000
T0 = 1_800_000_000_000  # fixed synthetic epoch; every test clock derives from it


def bar(open_ms, price, *, interval="1m", high=None, low=None):
    step = interval_ms(interval)
    return {"t": open_ms, "T": open_ms + step - 1, "s": "BTC", "i": interval,
            "o": str(price), "c": str(price + 1), "h": str(high if high else price + 2),
            "l": str(low if low else price - 2), "v": "10.5", "n": 42}


def series(start_ms, count, *, interval="1m", first=100):
    step = interval_ms(interval)
    return [bar(start_ms + i * step, first + i, interval=interval) for i in range(count)]


class FakeCandles:
    """Mimics the real endpoint: honours startTime, caps the tail, empty past retention."""

    name = "fake"

    def __init__(self, bars, *, cap=50):
        self.bars, self.cap, self.calls = bars, cap, []

    def candles(self, coin, interval, start_ms, end_ms):
        self.calls.append((coin, interval, start_ms, end_ms))
        window = [b for b in self.bars
                  if b["i"] == interval and start_ms <= b["t"] <= end_ms]
        return window[: self.cap]


# --- storage refuses to record what is not yet final ------------------------


def test_forming_bar_is_not_stored_as_final(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        bars = series(T0, 3)
        # "now" lands inside the third bar, so only the first two have closed.
        written = store.upsert_bars("BTC", "1m", bars, source="fake",
                                    fetched_at="2026-01-01T00:00:00+00:00",
                                    now_ms=T0 + 2 * MINUTE + 30_000)
        assert written == 2
        assert store.conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 2


def test_refetch_is_idempotent_and_later_fetch_wins(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        kwargs = {"source": "fake", "now_ms": T0 + 10 * MINUTE}
        store.upsert_bars("BTC", "1m", series(T0, 3), fetched_at="2026-01-01T00:00:00+00:00",
                          **kwargs)
        store.upsert_bars("BTC", "1m", series(T0, 3), fetched_at="2026-01-01T00:00:00+00:00",
                          **kwargs)
        assert store.conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 3
        # A corrected bar from a later fetch replaces the earlier value.
        store.upsert_bars("BTC", "1m", [bar(T0, 999)],
                          fetched_at="2026-02-01T00:00:00+00:00", **kwargs)
        assert store.conn.execute(
            "SELECT open FROM bars WHERE open_time = ?", (T0,)
        ).fetchone()[0] == 999


# --- reading refuses to leak the future -------------------------------------


def test_executable_price_is_the_next_bar_open_never_the_containing_bar(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        store.upsert_bars("BTC", "1m", series(T0, 5), source="fake",
                          fetched_at="2026-01-01T00:00:00+00:00", now_ms=T0 + 10 * MINUTE)
        # Decision lands 30s into the bar that opened at T0+1m (open price 101).
        hit = store.executable_price_at("BTC", T0 + MINUTE + 30_000)
        # That bar's own open is already in the past and its close is in the
        # future, so the honest fill is the next bar to start: 102.
        assert hit["price"] == 102
        assert hit["bar_open_time"] == T0 + 2 * MINUTE
        assert hit["delay_ms"] == 30_000
        assert hit["basis"] == "next_bar_open"


def test_unpriced_moment_stays_unknown(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        store.upsert_bars("BTC", "1m", series(T0, 3), source="fake",
                          fetched_at="2026-01-01T00:00:00+00:00", now_ms=T0 + 10 * MINUTE)
        # Well past the last stored bar: no interpolation, no nearest-neighbour.
        assert store.executable_price_at("BTC", T0 + 500 * MINUTE) is None
        assert store.executable_price_at("SOL", T0) is None


def test_volatility_uses_only_bars_closed_before_the_moment(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        store.upsert_bars("BTC", "1h", series(T0, 40, interval="1h"), source="fake",
                          fetched_at="2026-01-01T00:00:00+00:00",
                          now_ms=T0 + 100 * interval_ms("1h"))
        at = T0 + 30 * interval_ms("1h")
        vol = store.realized_vol_asof("BTC", at, interval="1h", lookback=24)
        assert vol["bars"] == 24
        assert vol["stdev_log_return"] > 0
        # Every bar it consumed had already closed at that moment.
        assert store.conn.execute(
            "SELECT MAX(close_time) FROM bars WHERE close_time <= ?", (at,)
        ).fetchone()[0] <= at


def test_thin_history_reports_no_volatility_rather_than_a_partial_one(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        store.upsert_bars("BTC", "1h", series(T0, 5, interval="1h"), source="fake",
                          fetched_at="2026-01-01T00:00:00+00:00",
                          now_ms=T0 + 100 * interval_ms("1h"))
        # 5 bars cannot fill a 24-bar window; a calm-looking number here would
        # be an artefact of missing data.
        assert store.realized_vol_asof("BTC", T0 + 4 * interval_ms("1h"), lookback=24) is None


# --- fetching ---------------------------------------------------------------


def test_backfill_walks_forward_and_records_asked_versus_got(tmp_path):
    available = series(T0, 120)
    source = FakeCandles(available, cap=50)
    with MarketStore(tmp_path / "m.db") as store:
        importer = MarketBackfill(source, store, delay=0.1, sleep=lambda _: None,
                                  clock=lambda: T0 + 200 * MINUTE)
        result = importer.backfill("BTC", "1m", since_ms=T0)
        assert result["status"] == "complete"
        assert result["first_open"] == T0
        assert result["last_open"] == T0 + 119 * MINUTE
        assert store.conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 120
        # The cap forced several passes, and each is auditable.
        assert result["requests"] >= 3
        asked, got = store.conn.execute(
            "SELECT SUM(requested_end - requested_start), SUM(returned_count) FROM fetch_runs"
        ).fetchone()
        assert got == 120 and asked > 0


def test_backfill_past_retention_records_empty_windows_not_success(tmp_path):
    source = FakeCandles([], cap=50)
    with MarketStore(tmp_path / "m.db") as store:
        importer = MarketBackfill(source, store, delay=0.1, sleep=lambda _: None,
                                  clock=lambda: T0 + 200 * MINUTE)
        result = importer.backfill("BTC", "1m", since_ms=T0)
        assert result["empty_windows"] >= 1
        assert result["first_open"] is None
        outcomes = {r[0] for r in store.conn.execute("SELECT outcome FROM fetch_runs")}
        assert outcomes == {"empty"}


def test_backfill_refuses_a_source_that_never_advances(tmp_path):
    class Stuck:
        name = "stuck"

        def candles(self, coin, interval, start_ms, end_ms):
            return series(T0 - 10 * MINUTE, 3)  # always the same, always behind

    with MarketStore(tmp_path / "m.db") as store:
        importer = MarketBackfill(Stuck(), store, delay=0.1, sleep=lambda _: None,
                                  clock=lambda: T0 + 200 * MINUTE)
        with pytest.raises(MarketFetchError, match="no progress"):
            importer.backfill("BTC", "1m", since_ms=T0)


def test_update_only_asks_for_what_is_missing(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        store.upsert_bars("BTC", "1m", series(T0, 10), source="fake",
                          fetched_at="2026-01-01T00:00:00+00:00", now_ms=T0 + 200 * MINUTE)
        source = FakeCandles(series(T0, 30), cap=50)
        importer = MarketBackfill(source, store, delay=0.1, sleep=lambda _: None,
                                  clock=lambda: T0 + 200 * MINUTE)
        importer.update("BTC", "1m")
        assert source.calls[0][2] == T0 + 10 * MINUTE  # resumes after the newest bar
        assert store.conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 30


def test_retention_edge_tracks_the_interval(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        importer = MarketBackfill(FakeCandles([]), store, delay=0.1,
                                  sleep=lambda _: None, clock=lambda: T0)
        # Minutes reach back days; hours reach back months. Same bar budget.
        minute_days = (T0 - importer.retention_start("1m")) / 86_400_000
        hour_days = (T0 - importer.retention_start("1h")) / 86_400_000
        assert round(minute_days) == 3
        assert round(hour_days) == 208


# --- joining ----------------------------------------------------------------


def test_naive_or_unparseable_timestamps_stay_unknown():
    assert to_ms("2026-01-01T00:00:00+00:00") is not None
    assert to_ms("2026-01-01T00:00:00Z") == to_ms("2026-01-01T00:00:00+00:00")
    assert to_ms("2026-01-01T00:00:00") is None  # no zone: cannot place on the market clock
    assert to_ms("not a date") is None
    assert to_ms(None) is None


def _archive_with_signal(path, *, ticker="BTC", origin="live", source_at, observed_at):
    with ResearchArchive(path) as archive:
        archive.append("investment", {
            "id": "inv-1", "baseId": "base-1", "ticker": ticker,
            "directionLong": True, "entryPrice": 123.0, "leverage": 20,
            "createdAt": source_at,
        }, origin=origin, entity_id="base-1", portfolio_id="pf-1",
            source_at=source_at, observed_at=observed_at)


def test_signal_row_prices_publication_and_each_delay(tmp_path):
    research, market = tmp_path / "r.db", tmp_path / "m.db"
    published = "2027-01-15T12:00:00+00:00"
    received = "2027-01-15T12:00:30+00:00"
    _archive_with_signal(research, source_at=published, observed_at=received)
    published_ms = to_ms(published)
    with MarketStore(market) as store:
        store.upsert_bars("BTC", "1m", series(published_ms, 120), source="fake",
                          fetched_at="2027-02-01T00:00:00+00:00",
                          now_ms=published_ms + 500 * MINUTE)
        store.upsert_bars("BTC", "1h", series(published_ms - 40 * interval_ms("1h"), 40,
                                              interval="1h"), source="fake",
                          fetched_at="2027-02-01T00:00:00+00:00",
                          now_ms=published_ms + 500 * MINUTE)
        rows = list(signal_rows(research, store))
    assert len(rows) == 1
    row = rows[0]
    assert row["coin"] == "BTC"
    assert row["publication_delay_ms"] == 30_000
    assert row["receipt_is_evidence_of_latency"] is True
    assert row["price_at_publication"] == 100
    assert row["realized_vol_at_publication"] > 0
    # Priced at each delay past receipt, so the cost of waiting is measurable.
    assert row["price_after_0s"] is not None
    assert row["price_after_3600s"] is not None
    assert row["price_after_3600s"] > row["price_after_0s"]
    # The trader's own claim is carried, never substituted for a measured price.
    assert row["reported_entry_price"] == 123.0


def test_backfilled_signal_is_not_treated_as_latency_evidence(tmp_path):
    research, market = tmp_path / "r.db", tmp_path / "m.db"
    _archive_with_signal(research, origin="backfill",
                         source_at="2027-01-15T12:00:00+00:00",
                         observed_at="2027-06-01T00:00:00+00:00")
    with MarketStore(market) as store:
        rows = list(signal_rows(research, store))
    # Months of "delay" here is just when we imported it, not when we could act.
    assert rows[0]["receipt_is_evidence_of_latency"] is False
    assert rows[0]["price_at_publication"] is None  # no bars: stays unknown


def test_coverage_gaps_make_a_thin_join_visible(tmp_path):
    research, market = tmp_path / "r.db", tmp_path / "m.db"
    _archive_with_signal(research, source_at="2027-01-15T12:00:00+00:00",
                         observed_at="2027-01-15T12:00:30+00:00")
    with MarketStore(market) as store:
        rows = list(signal_rows(research, store))
        gaps = coverage_gaps(rows)
    assert gaps == {"signals": 1, "with_publication_time": 1, "with_executable_price": 0,
                    "with_volatility": 0, "unpriced": 1, "priced_ratio": 0.0}


def test_market_context_reports_unknowns_without_raising(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        ctx = market_context(store, "BTC", T0)
    assert ctx["executable_price"] is None and ctx["realized_vol"] is None


def test_coins_come_from_what_the_archive_observed(tmp_path):
    research = tmp_path / "r.db"
    with ResearchArchive(research) as archive:
        for ticker in ("btc", "SOL", "BTC"):
            archive.append("investment", {"id": f"i-{ticker}", "ticker": ticker},
                           entity_id=f"b-{ticker}")
        archive.append("notification", {"id": "n1", "ticker": "IGNORED"})
    assert coins_from_archive(str(research)) == ["BTC", "SOL"]


def test_market_store_cannot_be_the_trading_db_or_pruned_logs(tmp_path):
    db, logs = tmp_path / "agent.db", tmp_path / "logs"
    for path in (db, logs / "market.db"):
        with pytest.raises(ValueError):
            validate_market_path(path, db, logs)


def test_status_and_join_survive_an_empty_store(tmp_path):
    with MarketStore(tmp_path / "m.db") as store:
        assert store.coverage() == []
    assert sqlite3.connect(tmp_path / "m.db").execute(
        "PRAGMA user_version").fetchone()[0] == 1
    assert json.dumps(coverage_gaps([])) is not None


# --- the archive names coins the venue never listed --------------------------


class ListedCandles(FakeCandles):
    """A source that knows its own venue's universe, as the real endpoint does."""

    def __init__(self, bars, listed, *, cap=50):
        super().__init__(bars, cap=cap)
        self.listed = set(listed)

    def universe(self) -> set[str]:
        return set(self.listed)


def test_coins_the_venue_does_not_list_are_skipped_not_requested(tmp_path):
    # Involio observes Binance-style and exchange tickers that Hyperliquid has
    # no perp for; asking for one answers HTTP 500 and used to kill the run.
    source = ListedCandles(series(T0, 10), listed={"BTC", "SOL"})
    wanted, skipped = tradable_coins(["1000BONK", "BTC", "KCS", "SOL"], source)
    assert wanted == ["BTC", "SOL"]
    assert skipped == ["1000BONK", "KCS"]

    with MarketStore(tmp_path / "m.db") as store:
        importer = MarketBackfill(source, store, delay=0.1, sleep=lambda _: None,
                                  clock=lambda: T0 + 200 * MINUTE)
        for symbol in wanted:
            importer.backfill(symbol, "1m", since_ms=T0)
    assert {c[0] for c in source.calls} == {"BTC", "SOL"}


def test_a_source_that_declares_no_universe_keeps_every_coin(tmp_path):
    source = FakeCandles(series(T0, 10))
    assert tradable_coins(["BTC", "WHATEVER"], source) == (["BTC", "WHATEVER"], [])


def test_universe_is_read_from_the_meta_endpoint():
    class Reply:
        status_code = 200

        @staticmethod
        def json():
            return {"universe": [{"name": "BTC"}, {"name": "SOL"}, {"name": "0G"}]}

    class Client:
        def __init__(self):
            self.sent = []

        def post(self, url, json):
            self.sent.append(json)
            return Reply()

    source = HyperliquidCandles.__new__(HyperliquidCandles)
    source.url, source._client = MAINNET_INFO_URL, Client()
    assert source.universe() == {"BTC", "SOL", "0G"}
    assert source._client.sent == [{"type": "meta"}]


def test_k_markets_are_listed_upper_cased_and_fetched_by_venue_spelling():
    # Hyperliquid spells its 1000x perps kPEPE; the archive's tickers arrive
    # upper-cased, so a case-sensitive check called them unlisted.
    class Client:
        def __init__(self):
            self.sent = []

        def post(self, url, json):
            self.sent.append(json)
            reply = type("Reply", (), {"status_code": 200})()
            body = ({"universe": [{"name": "BTC"}, {"name": "kPEPE"}]}
                    if json["type"] == "meta" else [])
            reply.json = lambda: body
            return reply

    source = HyperliquidCandles.__new__(HyperliquidCandles)
    source.url, source._client, source._venue_names = MAINNET_INFO_URL, Client(), None
    assert tradable_coins(["BTC", "KPEPE"], source) == (["BTC", "KPEPE"], [])

    source.candles("KPEPE", "4h", T0, T0 + 1)
    source.candles("BTC", "4h", T0, T0 + 1)
    assert [s["req"]["coin"] for s in source._client.sent[1:]] == ["kPEPE", "BTC"]


def test_an_unlisted_coin_that_slips_through_is_still_fatal(tmp_path):
    # Filtering is not a licence to swallow a real outage: a 500 still stops.
    class Broken:
        name = "broken"

        def candles(self, coin, interval, start_ms, end_ms):
            raise MarketFetchError("candleSnapshot failed (HTTP 500)")

    with MarketStore(tmp_path / "m.db") as store:
        importer = MarketBackfill(Broken(), store, delay=0.1, sleep=lambda _: None,
                                  clock=lambda: T0 + 200 * MINUTE)
        with pytest.raises(MarketFetchError, match="HTTP 500"):
            importer.backfill("BTC", "1m", since_ms=T0)
