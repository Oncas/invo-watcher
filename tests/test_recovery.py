from invo_agent.dex.recovery import (
    adverse_sl_price,
    decide_missed_close,
    looks_like_unlevered_park_sl,
)


def test_adverse_sl_scales_with_leverage() -> None:
    mid = 0.33
    unlev = adverse_sl_price(mid, 3.0, True, leverage=1)
    lev3 = adverse_sl_price(mid, 3.0, True, leverage=3)
    assert abs(unlev / mid - 0.97) < 1e-9
    assert abs(lev3 / mid - 0.99) < 1e-9
    short = adverse_sl_price(100.0, 3.0, False, leverage=10)
    assert abs(short / 100.0 - 1.003) < 1e-9


def test_looks_like_unlevered_park_sl() -> None:
    mid = 0.33
    old = adverse_sl_price(mid, 3.0, True, leverage=1)
    new = adverse_sl_price(mid, 3.0, True, leverage=3)
    assert looks_like_unlevered_park_sl(old, mid, sl_pct=3.0, leverage=3)
    assert not looks_like_unlevered_park_sl(new, mid, sl_pct=3.0, leverage=3)
    assert not looks_like_unlevered_park_sl(old, mid, sl_pct=3.0, leverage=1)


def test_park_sl_is_roe_not_price() -> None:
    d = decide_missed_close(
        our_entry=0.34,
        mid=0.33,
        is_long=True,
        trader_entry=0.32,
        trader_close=0.329,
        recovery_sl_pct=3.0,
        leverage=3,
    )
    assert d.action == "park"
    assert d.tp == 0.34
    assert abs(d.sl / 0.33 - 0.99) < 1e-9


def test_scale_in_does_not_make_us_look_redder_than_the_trader() -> None:
    """A trader who averages down must not strand us in a park.

    Both sides entered at the same price. The trader then scaled in, so their
    average walked toward the price and their loss at exit looks small. Measured
    from that moved average we are far redder than them and the park branch
    fires; measured from the entry we actually copied, we are level with them
    and the smaller loss gets taken now.
    """
    copied_entry = 100.0
    our_entry = 100.0
    trader_close = 98.0
    mid = 98.0

    moved_average = decide_missed_close(
        our_entry=our_entry,
        mid=mid,
        is_long=True,
        trader_entry=99.5,  # after scaling into the loser
        trader_close=trader_close,
        recovery_sl_pct=3.0,
        leverage=10,
    )
    assert moved_average.action == "park"
    assert moved_average.reason == "losing_worse_than_trader"

    as_copied = decide_missed_close(
        our_entry=our_entry,
        mid=mid,
        is_long=True,
        trader_entry=copied_entry,
        trader_close=trader_close,
        recovery_sl_pct=3.0,
        leverage=10,
    )
    assert as_copied.action == "close"
    assert as_copied.reason == "less_red_than_trader"


def test_genuinely_worse_entry_still_parks() -> None:
    """The park branch must survive the fix: a worse entry is still a park."""
    d = decide_missed_close(
        our_entry=100.0,
        mid=97.0,
        is_long=True,
        trader_entry=96.0,  # trader got in far better and left green-ish
        trader_close=95.5,
        recovery_sl_pct=3.0,
        leverage=10,
    )
    assert d.action == "park"
    assert d.reason == "losing_worse_than_trader"
