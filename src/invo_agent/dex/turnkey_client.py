from __future__ import annotations

import json
import time
from typing import Any

import httpx
from eth_account.messages import SignableMessage, encode_typed_data
from turnkey_api_key_stamper import ApiKeyStamper, ApiKeyStamperConfig


class TurnkeyError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def parse_read_write_session_result(result: dict[str, Any]) -> dict[str, Any]:
    """Pull apiKeyId + credentialBundle from V2 or V1 Turnkey result."""
    v2 = result.get("createReadWriteSessionResultV2") or {}
    v1 = result.get("createReadWriteSessionResult") or {}
    if not isinstance(v2, dict):
        v2 = {}
    if not isinstance(v1, dict):
        v1 = {}
    return {
        "apiKeyId": v2.get("apiKeyId") or v1.get("apiKeyId"),
        "credentialBundle": v2.get("credentialBundle") or v1.get("credentialBundle"),
    }


class TurnkeySigner:
    """Stamp Turnkey activities and sign EIP-712 digests for Hyperliquid."""

    def __init__(
        self,
        *,
        organization_id: str,
        sign_with_address: str,
        api_public_key: str,
        api_private_key: str,
        base_url: str = "https://api.turnkey.com",
    ):
        self.organization_id = organization_id
        self.sign_with = sign_with_address
        self.base_url = base_url.rstrip("/")
        self.stamper = ApiKeyStamper(
            ApiKeyStamperConfig(
                api_public_key=api_public_key,
                api_private_key=api_private_key,
            )
        )
        self._http = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def _submit(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        # Stamp and send identical JSON bytes.
        payload = json.dumps(body, separators=(",", ":"))
        stamp = self.stamper.stamp(payload)
        response = self._http.post(
            f"{self.base_url}{path}",
            content=payload,
            headers={
                "Content-Type": "application/json",
                stamp.stamp_header_name: stamp.stamp_header_value,
            },
        )
        data = response.json()
        if response.status_code >= 400:
            raise TurnkeyError(
                f"Turnkey {path} failed ({response.status_code}): {data}",
                status_code=response.status_code,
            )
        return data

    def whoami(self) -> dict[str, Any]:
        return self._submit(
            "/public/v1/query/whoami",
            {"organizationId": self.organization_id},
        )

    def get_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        data = self._submit(
            "/public/v1/query/get_api_keys",
            {"organizationId": self.organization_id, "userId": user_id},
        )
        keys = data.get("apiKeys")
        return keys if isinstance(keys, list) else []

    def create_read_write_session(
        self,
        *,
        target_public_key: str,
        api_key_name: str,
        expiration_seconds: int,
        invalidate_existing: bool = False,
    ) -> dict[str, Any]:
        """Stamp a new API-key session with the current still-valid key.

        Same as the Involio web renew path: ACTIVITY_TYPE_CREATE_READ_WRITE_SESSION_V2
        with invalidateExisting=false. No email OTP.
        """
        pub = target_public_key.removeprefix("0x").removeprefix("0X")
        if not pub.startswith("04") or len(pub) != 130:
            raise TurnkeyError(
                "targetPublicKey must be uncompressed P-256 hex "
                f"(04…, 130 chars), got len={len(pub)} prefix={pub[:2]!r}"
            )
        body = {
            "type": "ACTIVITY_TYPE_CREATE_READ_WRITE_SESSION_V2",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.organization_id,
            "parameters": {
                "targetPublicKey": pub,
                "apiKeyName": api_key_name,
                "expirationSeconds": str(int(expiration_seconds)),
                "invalidateExisting": bool(invalidate_existing),
            },
        }
        data = self._submit("/public/v1/submit/create_read_write_session", body)
        activity = data.get("activity") or data
        status = activity.get("status")
        if status and status != "ACTIVITY_STATUS_COMPLETED":
            raise TurnkeyError(
                f"create_read_write_session incomplete (status={status}): {data}"
            )
        parsed = parse_read_write_session_result(activity.get("result") or {})
        if not parsed.get("credentialBundle"):
            raise TurnkeyError(
                f"Missing credentialBundle in create_read_write_session: {data}"
            )
        return parsed

    def sign_eip712(self, typed_data: dict[str, Any]) -> dict[str, Any]:
        """Sign EIP-712 typed data; returns {r,s,v} hex fields for Hyperliquid.

        eth_account's encode_typed_data splits domain/struct hashes into
        SignableMessage header/body. LocalAccount signs keccak(\\x19||version||
        header||body). We must sign that same final digest (not body alone).
        """
        from eth_account.messages import _hash_eip191_message

        signable: SignableMessage = encode_typed_data(full_message=typed_data)
        digest_hex = _hash_eip191_message(signable).hex()

        body = {
            "type": "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.organization_id,
            "parameters": {
                "signWith": self.sign_with,
                "payload": digest_hex,
                "encoding": "PAYLOAD_ENCODING_HEXADECIMAL",
                "hashFunction": "HASH_FUNCTION_NO_OP",
            },
        }
        data = self._submit("/public/v1/submit/sign_raw_payload", body)
        activity = data.get("activity") or data
        result = ((activity.get("result") or {}).get("signRawPayloadResult")) or {}
        r = (result.get("r") or "").lower().removeprefix("0x")
        s = (result.get("s") or "").lower().removeprefix("0x")
        v_raw = result.get("v")
        if not r or not s or v_raw is None:
            # Activity may still be pending
            status = activity.get("status")
            raise TurnkeyError(f"Missing signature in Turnkey response (status={status}): {data}")
        v = int(v_raw, 16) if isinstance(v_raw, str) and v_raw.startswith("0x") else int(v_raw)
        # Hyperliquid expects 27/28 style v sometimes; normalize
        if v in (0, 1):
            v += 27
        return {"r": "0x" + r, "s": "0x" + s, "v": v}
