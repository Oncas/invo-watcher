"""A rejected Involio call must say *why* it was rejected, not just that it was.

``InvolioAPIError`` already carries the server's response body, but ``str(exc)``
renders only the status line. Every close we record has failed with a bare
``(400)`` for that reason, so the server's own explanation has never reached the
log. ``api_error_detail`` is what the log sites call instead.
"""

from __future__ import annotations

from invo_agent.api.client import InvolioAPIError, api_error_detail


def test_detail_carries_the_server_explanation() -> None:
    exc = InvolioAPIError(
        "DEX POST /dex/position/close failed (400)",
        status_code=400,
        body={"error": "position already closed"},
    )
    detail = api_error_detail(exc)
    assert "DEX POST /dex/position/close failed (400)" in detail
    assert "position already closed" in detail


def test_detail_of_a_bodyless_error_is_just_the_message() -> None:
    exc = InvolioAPIError("DEX POST /dex/mimic/copy failed (500)", status_code=500)
    assert api_error_detail(exc) == "DEX POST /dex/mimic/copy failed (500)"


def test_plain_exceptions_still_render() -> None:
    assert api_error_detail(ValueError("boom")) == "boom"


def test_a_huge_body_is_truncated() -> None:
    exc = InvolioAPIError("failed (400)", status_code=400, body={"error": "x" * 5000})
    detail = api_error_detail(exc)
    assert len(detail) < 1200
    assert detail.endswith("…")


def test_a_text_body_survives_verbatim() -> None:
    exc = InvolioAPIError("failed (400)", status_code=400, body="Bad Request")
    assert "Bad Request" in api_error_detail(exc)
