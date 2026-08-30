from invo_agent.dex.pricing import (
    extract_avg_px,
    fill_gaps,
    ioc_unfilled_message,
    min_notional_target,
    plan_resting_adopt,
    price_is_at_or_better,
    size_clearing_min_notional,
)


def test_long_at_or_better() -> None:
    assert price_is_at_or_better(mid=76.11, trader_entry=76.12, is_long=True)
    assert price_is_at_or_better(mid=76.12, trader_entry=76.12, is_long=True)
    assert not price_is_at_or_better(mid=76.13, trader_entry=76.12, is_long=True)


def test_short_at_or_better() -> None:
    assert price_is_at_or_better(mid=100.1, trader_entry=100.0, is_long=False)
    assert not price_is_at_or_better(mid=99.9, trader_entry=100.0, is_long=False)


def test_missing_entry_is_ok() -> None:
    assert price_is_at_or_better(mid=1.0, trader_entry=None, is_long=True)


def test_fill_gaps_long() -> None:
    price, roe = fill_gaps(our_px=63807, trader_entry=63786, is_long=True, leverage=30)
    assert price is not None and roe is not None
    assert 0.03 < price < 0.04
    assert 0.9 < roe < 1.1


def test_extract_avg_px() -> None:
    resp = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": "3.22", "avgPx": "76.127"}}]},
        },
    }
    assert extract_avg_px(resp) == 76.127


def test_ioc_unfilled_message() -> None:
    assert ioc_unfilled_message(
        "Order could not immediately match against any resting orders."
    )
    assert not ioc_unfilled_message("Insufficient margin")


def test_plan_resting_adopt_keeps_link_partial() -> None:
    held, keep = plan_resting_adopt(
        posted_sz=1.1,
        filled_sz=0.6,
        booked_sz=0.0,
        hl_sz=0.6,
        only_copy=True,
    )
    assert held == 0.6
    assert keep is True


def test_plan_resting_adopt_grows_after_leftover_fill() -> None:
    held, keep = plan_resting_adopt(
        posted_sz=1.1,
        filled_sz=0.6,
        booked_sz=0.6,
        hl_sz=1.1,
        only_copy=True,
    )
    assert held == 1.1
    assert keep is False


def test_plan_resting_adopt_ignores_hl_when_shared() -> None:
    held, keep = plan_resting_adopt(
        posted_sz=1.1,
        filled_sz=0.6,
        booked_sz=0.6,
        hl_sz=2.0,
        only_copy=False,
    )
    assert held == 0.6
    assert keep is True


def test_plan_resting_adopt_does_not_trust_shrunk_posted() -> None:
    """Old bug: resting_sz was rewritten to leftover 0.5, then 0.6 fills looked done."""
    held, keep = plan_resting_adopt(
        posted_sz=1.1,
        filled_sz=0.6,
        booked_sz=0.6,
        hl_sz=0.6,
        only_copy=True,
    )
    assert held == 0.6
    assert keep is True


def test_min_notional_target_buffers_over_the_floor() -> None:
    """Open and close paths must aim over HL's floor, never at it."""
    assert min_notional_target(10.0) == 12.0
    assert min_notional_target(20.0) == 24.0
    # A floor below $10 still gets the $12 aim the close path always used.
    assert min_notional_target(5.0) == 12.0


def test_btc_40x_clip_survives_lot_rounding() -> None:
    """0.00013 BTC at mid 78188.5 is $10.16 — over the floor, still rejected.

    Flooring the lot at the buffered target has to leave real headroom, not
    land a few cents over the floor for HL to disagree with.
    """
    mid = 78188.5
    sz = size_clearing_min_notional(
        mid=mid,
        notional=min_notional_target(10.0),
        min_notional=10.0,
        sz_decimals=5,
    )
    assert sz * mid > 11.0
