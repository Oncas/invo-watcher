from __future__ import annotations

import json
from typing import Any

import httpx
from eth_account.messages import SignableMessage, encode_typed_data
from turnkey_api_key_stamper import ApiKeyStamper, ApiKeyStamperConfig


class TurnkeyError(RuntimeError):
    pass


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
            raise TurnkeyError(f"Turnkey {path} failed ({response.status_code}): {data}")
        return data

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
            "timestampMs": str(int(__import__("time").time() * 1000)),
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
