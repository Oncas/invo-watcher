from invo_agent.dex.pricing import (
    extract_avg_px,
    fill_gaps,
    ioc_unfilled_message,
    price_is_at_or_better,
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
