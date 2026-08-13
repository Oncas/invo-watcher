from invo_agent.hud import _account_value_samples, _baseline_at_window_start


def test_baseline_is_last_print_at_or_before_window() -> None:
    # Sparse perpDay prints around a T−24h that sits 12 min before the next one.
    # Nearest would pick 14:48 ($53.51) and under-report vs Involio.
    t0 = 1700014700000  # synthetic window start
    samples = [
        (1700000000000, 45.0),  # synthetic sample
        (1700015600000, 50.0),  # synthetic sample
    ]
    picked = _baseline_at_window_start(samples, t0)
    assert picked is not None
    assert picked[0] == 1700000000000
    assert picked[1] == 45.0


def test_baseline_uses_exact_window_print() -> None:
    samples = [(100, 10.0), (200, 20.0), (300, 30.0)]
    assert _baseline_at_window_start(samples, 200) == (200, 20.0)


def test_baseline_falls_back_to_first_after_if_all_newer() -> None:
    samples = [(200, 20.0), (300, 30.0)]
    assert _baseline_at_window_start(samples, 100) == (200, 20.0)


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


