from datetime import UTC, datetime, timedelta

from invo_agent.dex.session import (
    TURNKEY_DEFAULT_EXPIRATION_SECONDS,
    DexSessionData,
    infer_dex_expires_at_ms,
    session_should_proactive_renew,
    session_ttl_remaining_seconds,
)


def _session(*, remaining_s: float, issued_s: float) -> DexSessionData:
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=remaining_s)
    updated = expires - timedelta(seconds=issued_s)
    return DexSessionData(
        organization_id="org",
        master_address="0xabc",
        api_public_key="pub",
        api_private_key="priv",
        expires_at_ms=int(expires.timestamp() * 1000),
        updated_at=updated,
    )


def test_short_key_renews_near_end_not_immediately() -> None:
    # 15-minute issued TTL, 11 minutes left → 25% lead is 3.75 min → do not renew yet
    data = _session(remaining_s=11 * 60, issued_s=15 * 60)
    assert not session_should_proactive_renew(data, renew_before_seconds=6 * 3600)
    data_late = _session(remaining_s=2 * 60, issued_s=15 * 60)
    assert session_should_proactive_renew(data_late, renew_before_seconds=6 * 3600)


def test_long_key_uses_six_hour_lead() -> None:
    data = _session(remaining_s=7 * 3600, issued_s=30 * 24 * 3600)
    assert not session_should_proactive_renew(data, renew_before_seconds=6 * 3600)
    data_near = _session(remaining_s=5 * 3600, issued_s=30 * 24 * 3600)
    assert session_should_proactive_renew(data_near, renew_before_seconds=6 * 3600)


def test_missing_expires_at_uses_turnkey_15m_not_requested_30d() -> None:
    now_ms = 1_700_000_000_000
    expires, source = infer_dex_expires_at_ms({}, now_ms=now_ms)
    assert source == "turnkey_default_15m"
    assert expires == now_ms + TURNKEY_DEFAULT_EXPIRATION_SECONDS * 1000
    expires_api, source_api = infer_dex_expires_at_ms(
        {"expiresAtMs": now_ms + 3600_000}, now_ms=now_ms
    )
    assert source_api == "involio"
    assert expires_api == now_ms + 3600_000


def test_remaining_none_without_expiry() -> None:
    data = DexSessionData(
        organization_id="org",
        master_address="0xabc",
        api_public_key="pub",
        api_private_key="priv",
    )
    assert session_ttl_remaining_seconds(data) is None
    assert not session_should_proactive_renew(data)
