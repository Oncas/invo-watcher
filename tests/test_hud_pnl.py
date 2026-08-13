from invo_agent.db import AgentDB
from invo_agent.hud import (
    _PNL_BASELINE_MAX_LAG_MS,
    _PNL_ROLLOVER_SECONDS,
    _account_value_samples,
    _baseline_at_window_start,
    _resolve_pinned_baseline,
)


def test_baseline_is_last_print_at_least_6h_before_window() -> None:
    # Synthetic timestamps exercise choosing the correct baseline sample.
    t0 = 1700022200000  # synthetic window start
    samples = [
        (1700000000000, 45.0),  # synthetic sample
        (1700015600000, 50.0),  # synthetic sample
        (1700024000000, 55.0),  # synthetic sample
    ]
    picked = _baseline_at_window_start(samples, t0)
    assert picked == (1700000000000, 45.0)


def test_baseline_falls_back_to_last_before_if_none_old_enough() -> None:
    samples = [(100, 10.0), (200, 20.0)]
    assert _baseline_at_window_start(samples, 250) == (200, 20.0)


def test_baseline_falls_back_to_first_after_if_all_newer() -> None:
    samples = [(200, 20.0), (300, 30.0)]
    assert _baseline_at_window_start(samples, 100) == (200, 20.0)


def test_pinned_baseline_holds_until_rollover(tmp_path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    lag = _PNL_BASELINE_MAX_LAG_MS
    samples = [(0, 10.0), (lag, 20.0), (2 * lag, 40.0)]
    first = _resolve_pinned_baseline(samples, lag + 100, db=db, now=1_000.0)
    assert first == (0, 10.0)
    held = _resolve_pinned_baseline(
        samples, 2 * lag + 100, db=db, now=1_000.0 + _PNL_ROLLOVER_SECONDS - 1
    )
    assert held == (0, 10.0)
    rolled = _resolve_pinned_baseline(
        samples, 2 * lag + 100, db=db, now=1_000.0 + _PNL_ROLLOVER_SECONDS
    )
    assert rolled == (lag, 20.0)
    db.close()


def test_account_value_samples_ignore_spot_day_week() -> None:
    portfolio = [
        ("day", {"accountValueHistory": [[1, 999.0]]}),
        ("perpDay", {"accountValueHistory": [[2, 50.0]]}),
        ("perpWeek", {"accountValueHistory": [[3, 51.0]]}),
    ]
    assert _account_value_samples(portfolio) == [(2, 50.0), (3, 51.0)]


def test_involio_pct_matches_header_formula() -> None:
    equity = 120.0
    baseline = 60.0
    net_flow = gross_in = 50.0
    pnl = equity - baseline - net_flow
    pct = pnl / (baseline + gross_in) * 100.0
    assert pnl == 10.0
    assert round(pct, 2) == 9.09


