"""A live agent with a dead signing key polls happily but cannot close.

Before this, /v1/health reported ok:true throughout exactly that state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from invo_agent.config import Settings
from invo_agent.db import AgentDB
from invo_agent.dex.session import DexSessionData, DexSessionStore
from invo_agent.server import _dex_key_state, start_hud_server


def _write_session(path, *, remaining_s: float) -> None:
    expires = datetime.now(UTC) + timedelta(seconds=remaining_s)
    DexSessionStore(path).save(
        DexSessionData(
            organization_id="org",
            master_address="0xabc",
            api_public_key="pub",
            api_private_key="priv",
            expires_at_ms=int(expires.timestamp() * 1000),
            updated_at=expires - timedelta(seconds=298),
        )
    )


def _settings(tmp_path, *, live: bool, remaining_s: float | None) -> Settings:
    session_path = tmp_path / "dex-session.json"
    if remaining_s is not None:
        _write_session(session_path, remaining_s=remaining_s)
    return Settings(
        live=live,
        dry_run=not live,
        dex_session_path=session_path,
        db_path=tmp_path / "agent.db",
        hud_token="test-token",
        hud_bind="127.0.0.1",
    )


def test_live_expired_key_is_not_ok(tmp_path) -> None:
    ttl, ok = _dex_key_state(_settings(tmp_path, live=True, remaining_s=-115))
    assert ok is False
    assert ttl == pytest.approx(-115, abs=1)


def test_live_valid_key_is_ok(tmp_path) -> None:
    ttl, ok = _dex_key_state(_settings(tmp_path, live=True, remaining_s=200))
    assert ok is True
    assert ttl == pytest.approx(200, abs=1)


def test_live_missing_session_is_not_ok(tmp_path) -> None:
    assert _dex_key_state(_settings(tmp_path, live=True, remaining_s=None)) == (None, False)


def test_dry_run_ignores_dead_key(tmp_path) -> None:
    # Must not cry wolf in dry-run, or the signal gets trained away.
    assert _dex_key_state(_settings(tmp_path, live=False, remaining_s=-999)) == (None, True)


def _get(port: int) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/health",
        headers={"Authorization": "Bearer test-token"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_endpoint_returns_503_on_dead_key(tmp_path, free_tcp_port) -> None:
    """End to end: the probe a watcher actually calls must fail."""
    settings = _settings(tmp_path, live=True, remaining_s=-115)
    settings = settings.model_copy(update={"hud_port": free_tcp_port})
    # Poll loop is healthy — the key is the only thing wrong, so the probe
    # must fail for that reason and not hide behind a stale poll.
    db = AgentDB(settings.db_path)
    db.set_meta("last_poll_at", datetime.now(UTC).isoformat())
    db.close()
    httpd, stop = start_hud_server(settings)
    try:
        status, body = _get(free_tcp_port)
    finally:
        stop.set()
        httpd.shutdown()
    assert status == 503
    assert body["ok"] is False
    assert body["dex_key_ok"] is False
    assert body["reason"] == "dex_key_expired"


def test_endpoint_returns_200_on_live_key(tmp_path, free_tcp_port) -> None:
    """The other half: a healthy live agent must still pass, or the probe is useless."""
    settings = _settings(tmp_path, live=True, remaining_s=200)
    settings = settings.model_copy(update={"hud_port": free_tcp_port})
    db = AgentDB(settings.db_path)
    db.set_meta("last_poll_at", datetime.now(UTC).isoformat())
    db.close()
    httpd, stop = start_hud_server(settings)
    try:
        status, body = _get(free_tcp_port)
    finally:
        stop.set()
        httpd.shutdown()
    assert status == 200
    assert body["ok"] is True
    assert body["dex_key_ok"] is True
    assert body["reason"] is None
