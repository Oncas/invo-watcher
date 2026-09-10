"""A rejected HL order must leave enough behind to diagnose it later.

HL reports a rejected order *inside* a ``status: ok`` envelope, so nothing
raises at the HTTP layer and the caller only ever sees the parsed message.
When the message contradicts what we believe we sent, the parsed string alone
cannot settle it — the ticket we signed has to be on record too.
"""

from __future__ import annotations

from invo_agent.dex.hl_trader import first_order_error


def _resp(statuses: list) -> dict:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}


def test_error_is_found_inside_an_ok_envelope():
    resp = _resp([{"error": "Order must have minimum value of $10. asset=0"}])
    assert first_order_error(resp) == "Order must have minimum value of $10. asset=0"


def test_filled_order_reports_no_error():
    resp = _resp([{"filled": {"totalSz": "1.0", "avgPx": "100.0", "oid": 1}}])
    assert first_order_error(resp) is None


def test_first_error_wins_across_legs():
    resp = _resp(
        [
            {"filled": {"totalSz": "1.0", "avgPx": "100.0", "oid": 1}},
            {"error": "second leg rejected"},
        ]
    )
    assert first_order_error(resp) == "second leg rejected"


def test_malformed_shapes_do_not_raise():
    assert first_order_error(None) is None
    assert first_order_error({}) is None
    assert first_order_error({"status": "ok"}) is None
    assert first_order_error(_resp([])) is None
    assert first_order_error(_resp(["not-a-dict"])) is None
