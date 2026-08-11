from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field
from rich.console import Console

from invo_agent.api.client import InvolioClient
from invo_agent.auth.inbox import InboxError, wait_for_otp
from invo_agent.config import Settings
from invo_agent.dex.turnkey_client import TurnkeySigner
from invo_agent.dex.turnkey_crypto import (
    decrypt_credential_bundle,
    generate_p256_keypair,
    public_key_from_private,
)

console = Console()


class DexSessionData(BaseModel):
    organization_id: str
    wallet_id: str | None = None
    trading_account_id: str | None = None
    master_address: str
    api_public_key: str
    api_private_key: str
    api_key_id: str | None = None
    credential_type: str | None = None
    expires_at_ms: int | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DexSessionStore:
    def __init__(self, path: Path):
        self.path = path.expanduser()

    def load(self) -> DexSessionData | None:
        if not self.path.exists():
            return None
        try:
            return DexSessionData.model_validate_json(self.path.read_text())
        except Exception:
            return None

    def save(self, data: DexSessionData) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = data.model_copy(update={"updated_at": datetime.now(UTC)})
        self.path.write_text(payload.model_dump_json(indent=2))
        self.path.chmod(0o600)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class DexSessionError(RuntimeError):
    pass


def enable_trading_account(client: InvolioClient) -> dict:
    return client.dex_post("/dex/auth/enable", {})


def start_dex_otp(client: InvolioClient) -> tuple[str, int]:
    data = client.dex_post("/dex/auth/otp", {})
    payload = data.get("data") or data
    otp_id = payload.get("otpId")
    ttl = int(payload.get("ttlMs") or 300000)
    if not otp_id:
        raise DexSessionError(f"No otpId in response: {data}")
    return str(otp_id), ttl


def complete_dex_otp_login(
    client: InvolioClient,
    *,
    otp_id: str,
    code: str,
    public_key: str,
    expiration_seconds: int = 60 * 60 * 24 * 7,
    device_label: str = "invo-agent",
) -> dict:
    body = {
        "otpId": otp_id,
        "code": code.strip(),
        "publicKey": public_key,
        "deviceLabel": device_label,
        "expirationSeconds": expiration_seconds,
    }
    data = client.dex_post("/dex/auth/otp/login", body)
    return data.get("data") or data


def establish_dex_session(
    client: InvolioClient,
    settings: Settings,
    store: DexSessionStore,
    *,
    code: str | None = None,
    code_prompt=None,
) -> DexSessionData:
    """Create/refresh a Turnkey API-key session via Involio DEX email OTP."""
    console.print("[dim]Enabling DEX trading account...[/dim]")
    enabled = enable_trading_account(client)
    en = enabled.get("data") or enabled
    org_id = en.get("organizationId")
    master = en.get("masterAddress") or (en.get("wallet") or {}).get("evmAddress")
    if not org_id or not master:
        raise DexSessionError(f"dex/auth/enable missing org/address: {enabled}")

    keypair = generate_p256_keypair()
    console.print("[dim]Requesting DEX trading OTP...[/dim]")
    started = datetime.now(UTC)
    otp_id, _ttl = start_dex_otp(client)
    console.print("[green]DEX OTP sent to your email.[/green]")

    if not code:
        if settings.imap_configured:
            console.print(
                "[dim]Waiting for Invo Wallet OTP "
                "(from no-reply@mail.turnkey.com)...[/dim]"
            )
            try:
                code = wait_for_otp(
                    settings,
                    kind="wallet",
                    not_before=started,
                    timeout_seconds=90,
                )
                console.print(f"[green]Wallet OTP found:[/green] {code}")
            except InboxError as exc:
                console.print(f"[yellow]IMAP miss:[/yellow] {exc}")
                if code_prompt is None:
                    raise DexSessionError(str(exc)) from exc
                code = code_prompt()
        elif code_prompt:
            code = code_prompt()
        else:
            raise DexSessionError("No DEX OTP code and IMAP not configured")

    if not code:
        raise DexSessionError("Empty DEX OTP code")

    login = complete_dex_otp_login(
        client,
        otp_id=otp_id,
        code=code,
        public_key=keypair.public_key_otp_hex,
    )
    bundle = login.get("credentialBundle")
    if not bundle:
        raise DexSessionError(f"No credentialBundle in otp/login: {login}")

    api_private = decrypt_credential_bundle(bundle, keypair.private_key_hex)
    api_public = public_key_from_private(api_private)

    session = DexSessionData(
        organization_id=str(org_id),
        wallet_id=en.get("walletId"),
        trading_account_id=en.get("tradingAccountId"),
        master_address=str(master),
        api_public_key=api_public,
        api_private_key=api_private,
        api_key_id=login.get("apiKeyId"),
        credential_type=login.get("credentialType"),
        expires_at_ms=login.get("expiresAtMs"),
    )
    store.save(session)
    # Keep a redacted mirror under captures for debugging (optional)
    debug_path = Path("captures/dex-session.meta.json")
    debug_path.parent.mkdir(exist_ok=True)
    debug_path.write_text(
        json.dumps(
            {
                "organization_id": session.organization_id,
                "master_address": session.master_address,
                "api_key_id": session.api_key_id,
                "expires_at_ms": session.expires_at_ms,
            },
            indent=2,
        )
    )
    return session


def load_signer(store: DexSessionStore) -> tuple[DexSessionData, TurnkeySigner]:
    data = store.load()
    if data is None:
        raise DexSessionError("No DEX session. Run: invo-agent dex-login")
    signer = TurnkeySigner(
        organization_id=data.organization_id,
        sign_with_address=data.master_address,
        api_public_key=data.api_public_key,
        api_private_key=data.api_private_key,
    )
    return data, signer
