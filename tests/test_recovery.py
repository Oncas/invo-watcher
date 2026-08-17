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
