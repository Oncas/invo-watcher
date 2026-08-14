from invo_agent.hud import (
    _PNL_BASELINE_MAX_LAG_MS,
    _account_value_samples,
    _baseline_at_window_start,
    _usable_baseline,
)


def test_baseline_is_first_print_at_or_after_window() -> None:
    # Synthetic timestamps exercise choosing the correct baseline sample.
    t0 = 1700069900000  # synthetic window start
    samples = [
        (1700046500000, 40.0),  # synthetic sample
        (1700068400000, 50.0),  # synthetic sample
        (1700075600000, 60.0),  # synthetic sample
    ]
    picked = _baseline_at_window_start(samples, t0)
    assert picked == (1700075600000, 60.0)


def test_baseline_uses_exact_window_print() -> None:
    samples = [(100, 10.0), (200, 20.0), (300, 30.0)]
    assert _baseline_at_window_start(samples, 200) == (200, 20.0)


def test_baseline_falls_back_to_last_before_if_all_older() -> None:
    samples = [(100, 10.0), (200, 20.0)]
    assert _baseline_at_window_start(samples, 250) == (200, 20.0)


def test_usable_baseline_hides_fallback_older_than_6h() -> None:
    lag = _PNL_BASELINE_MAX_LAG_MS
    window = 2 * lag
    samples = [(0, 10.0)]
    assert _baseline_at_window_start(samples, window) == (0, 10.0)
    assert _usable_baseline(samples, window) is None


def test_usable_baseline_keeps_recent_fallback() -> None:
    window = _PNL_BASELINE_MAX_LAG_MS
    samples = [(window - 60_000, 50.0)]
    assert _usable_baseline(samples, window) == (window - 60_000, 50.0)


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


