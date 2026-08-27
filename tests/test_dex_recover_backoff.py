"""The 26 Aug live run: a ~5 min OTP key kept dying inside a flat 10 min backoff.

Recovery has to retry faster than the key it is trying to save expires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from invo_agent.dex.session import DexSessionData, session_issued_ttl_seconds
from invo_agent.poller import (
    DEX_RECOVER_COOLDOWN_MIN_SECONDS,
    DEX_RECOVER_COOLDOWN_SECONDS,
    NotificationPoller,
)


def _session(*, remaining_s: float, issued_s: float) -> DexSessionData:
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=remaining_s)
    return DexSessionData(
        organization_id="org",
        master_address="0xabc",
        api_public_key="pub",
        api_private_key="priv",
        expires_at_ms=int(expires.timestamp() * 1000),
        updated_at=expires - timedelta(seconds=issued_s),
    )


def _poller(data: DexSessionData | None) -> SimpleNamespace:
    return SimpleNamespace(
        executor=SimpleNamespace(dex_store=SimpleNamespace(load=lambda: data))
    )


def test_issued_ttl_reads_granted_lifetime_not_remaining() -> None:
    data = _session(remaining_s=60, issued_s=298)
    assert session_issued_ttl_seconds(data) == pytest.approx(298, abs=0.01)


def test_issued_ttl_none_without_timestamps() -> None:
    data = _session(remaining_s=60, issued_s=298)
    assert session_issued_ttl_seconds(data.model_copy(update={"updated_at": None})) is None


def test_five_minute_key_retries_well_inside_its_own_life() -> None:
    # The 26 Aug failure: 298s key, flat 600s cooldown → dead before the retry.
    data = _session(remaining_s=86, issued_s=298)
    cooldown = NotificationPoller._dex_cooldown_seconds(_poller(data))
    assert cooldown < 298, "cooldown must not outlast the key it is recovering"
    assert cooldown == pytest.approx(29.8, abs=0.01)


def test_tiny_key_still_respects_the_floor() -> None:
    # Don't turn a 30s key into a Turnkey OTP hammer.
    data = _session(remaining_s=5, issued_s=30)
    cooldown = NotificationPoller._dex_cooldown_seconds(_poller(data))
    assert cooldown == DEX_RECOVER_COOLDOWN_MIN_SECONDS


def test_thirty_day_key_keeps_the_full_ten_minutes() -> None:
    data = _session(remaining_s=3600, issued_s=30 * 24 * 3600)
    cooldown = NotificationPoller._dex_cooldown_seconds(_poller(data))
    assert cooldown == DEX_RECOVER_COOLDOWN_SECONDS


def test_unknown_ttl_falls_back_to_flat_cooldown() -> None:
    assert (
        NotificationPoller._dex_cooldown_seconds(_poller(None))
        == DEX_RECOVER_COOLDOWN_SECONDS
    )
