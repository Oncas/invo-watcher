from invo_agent.hud import (
    PNL_RULE,
    RollingPnl,
    _PNL_BASELINE_MAX_LAG_MS,
    _account_value_samples,
    _baseline_at_window_start,
    _usable_baseline,
    pnl_served_matches_fresh,
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


def test_served_pnl_allows_mark_move_on_same_baseline() -> None:
    fresh = RollingPnl(
        pnl=10.0,
        pct=9.09,
        baseline_ms=1700075600000,
        baseline_equity=60.0,
        net_flow=50.0,
        gross_in=50.0,
    )
    # Equity ticked +$1.50; 24h $ must move by the same amount.
    err = pnl_served_matches_fresh(
        served_equity=121.5,
        served_pnl=11.5,
        served_baseline_ms=fresh.baseline_ms,
        served_rule=PNL_RULE,
        fresh=fresh,
    )
    assert err is None


def test_served_pnl_rejects_old_6h_baseline() -> None:
    fresh = RollingPnl(
        pnl=10.0,
        pct=9.09,
        baseline_ms=1700075600000,
        baseline_equity=60.0,
        net_flow=50.0,
        gross_in=50.0,
    )
    err = pnl_served_matches_fresh(
        served_equity=120.0,
        served_pnl=20.0,
        served_baseline_ms=1700046500000,
        served_rule=PNL_RULE,
        fresh=fresh,
    )
    assert err is not None
    assert "baseline" in err


def test_served_pnl_rejects_missing_rule() -> None:
    fresh = RollingPnl(
        pnl=5.0,
        pct=4.2,
        baseline_ms=1,
        baseline_equity=60.0,
        net_flow=0.0,
        gross_in=0.0,
    )
    err = pnl_served_matches_fresh(
        served_equity=65.0,
        served_pnl=5.0,
        served_baseline_ms=1,
        served_rule=None,
        fresh=fresh,
    )
    assert err is not None
    assert "pnl_rule" in err
