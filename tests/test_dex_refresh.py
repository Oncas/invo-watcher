from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from invo_agent.dex.session import DexSessionData, DexSessionStore, refresh_dex_session
from invo_agent.dex.turnkey_client import (
    TurnkeySigner,
    parse_read_write_session_result,
)
from invo_agent.dex.turnkey_crypto import generate_p256_keypair


def test_parse_prefers_v2_result() -> None:
    parsed = parse_read_write_session_result(
        {
            "createReadWriteSessionResultV2": {
                "apiKeyId": "key-v2",
                "credentialBundle": "bundle-v2",
            },
            "createReadWriteSessionResult": {
                "apiKeyId": "key-v1",
                "credentialBundle": "bundle-v1",
            },
        }
    )
    assert parsed == {"apiKeyId": "key-v2", "credentialBundle": "bundle-v2"}


def test_parse_falls_back_to_v1() -> None:
    parsed = parse_read_write_session_result(
        {"createReadWriteSessionResult": {"apiKeyId": "key-v1", "credentialBundle": "b"}}
    )
    assert parsed == {"apiKeyId": "key-v1", "credentialBundle": "b"}


def test_create_read_write_session_stamps_expected_body(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_submit(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["path"] = path
        captured["body"] = body
        return {
            "activity": {
                "status": "ACTIVITY_STATUS_COMPLETED",
                "result": {
                    "createReadWriteSessionResultV2": {
                        "apiKeyId": "new-key",
                        "credentialBundle": "new-bundle",
                    }
                },
            }
        }

    monkeypatch.setattr(TurnkeySigner, "_submit", fake_submit)
    pair = generate_p256_keypair()
    target = pair.public_key_otp_hex.removeprefix("0x")
    signer = TurnkeySigner(
        organization_id="org-1",
        sign_with_address="0xabc",
        api_public_key="aa" * 33,
        api_private_key="bb" * 32,
    )
    try:
        out = signer.create_read_write_session(
            target_public_key="0x" + target,
            api_key_name="invo-watcher · testhash",
            expiration_seconds=2592000,
            invalidate_existing=False,
        )
    finally:
        signer.close()

    assert out == {"apiKeyId": "new-key", "credentialBundle": "new-bundle"}
    assert captured["path"] == "/public/v1/submit/create_read_write_session"
    body = captured["body"]
    assert body["type"] == "ACTIVITY_TYPE_CREATE_READ_WRITE_SESSION_V2"
    assert body["organizationId"] == "org-1"
    assert body["parameters"]["targetPublicKey"] == target
    assert body["parameters"]["expirationSeconds"] == "2592000"
    assert body["parameters"]["invalidateExisting"] is False
    assert body["parameters"]["apiKeyName"].startswith("invo-watcher")


def test_refresh_dex_session_saves_new_key(tmp_path: Path, monkeypatch) -> None:
    store = DexSessionStore(tmp_path / "dex-session.json")
    store.save(
        DexSessionData(
            organization_id="org-1",
            wallet_id="w1",
            trading_account_id="t1",
            master_address="0xabc",
            api_public_key="old-pub",
            api_private_key="old-priv",
            api_key_id="old-id",
            expires_at_ms=int(datetime.now(UTC).timestamp() * 1000) + 120_000,
        )
    )

    class FakeSigner:
        organization_id = "org-1"

        def create_read_write_session(self, **kwargs):
            assert kwargs["invalidate_existing"] is False
            assert kwargs["expiration_seconds"] == 2592000
            assert kwargs["target_public_key"].startswith("04")
            assert len(kwargs["target_public_key"]) == 130
            return {"apiKeyId": "new-id", "credentialBundle": "bundle"}

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "invo_agent.dex.session.load_signer",
        lambda store, require_fresh=False: (store.load(), FakeSigner()),
    )
    monkeypatch.setattr(
        "invo_agent.dex.session.decrypt_credential_bundle",
        lambda bundle, priv: "new-priv-hex",
    )
    monkeypatch.setattr(
        "invo_agent.dex.session.public_key_from_private",
        lambda priv: "new-pub-hex",
    )
    monkeypatch.setattr(
        "invo_agent.dex.session.refine_expiry_from_turnkey",
        lambda data: data,
    )

    session = refresh_dex_session(store)
    assert session.api_key_id == "new-id"
    assert session.api_public_key == "new-pub-hex"
    assert session.api_private_key == "new-priv-hex"
    assert session.master_address == "0xabc"
    saved = store.load()
    assert saved is not None
    assert saved.api_key_id == "new-id"
    # Old key stays until save; after refresh the file is the new key.
    dumped = json.loads((tmp_path / "dex-session.json").read_text())
    assert dumped["api_private_key"] == "new-priv-hex"
    assert dumped["api_private_key"] != "old-priv"
